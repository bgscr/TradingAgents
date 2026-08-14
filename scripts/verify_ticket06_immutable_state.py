from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any

EXPECTED_SEQUENCE_COUNT = 1024
EXPECTED_ATTEMPT_COUNT = 1036
RUN_LIVE_ENVIRONMENT = (
    "RUN_LIVE_DEEPSEEK_TESTS",
    "RUN_LIVE_PROVIDER_TESTS",
    "RUN_LIVE_TUSHARE_TESTS",
)
INCIDENT_SEQUENCE_IDS = (
    "provider-request-sequence=sha256:52708e996eea0b08eef8207023dab4928e3d0f1aeea568f5025dc589b2fd63c9",
    "provider-request-sequence=sha256:887ebcfd38215c9f2db1f63ca9644853dc98d49f7774e2f7d4495881077f9db7",
    "provider-request-sequence=sha256:59c031e2a78af025fac9aa9933e905ce1b0fbfeb5f7eb8c90258616462725611",
    "provider-request-sequence=sha256:49d24acaf2cea5b03f2e72930101ce815f0e2ac0b993144a25524100fea686a0",
)
INCIDENT_IDENTITIES = (
    (
        INCIDENT_SEQUENCE_IDS[0],
        "provider-physical-attempt=sha256:"
        "4a18d589b32f8f7a1c96a7feb5dfea0c61ec4cb40c4e36d1491cf9afa566655c",
    ),
    (
        INCIDENT_SEQUENCE_IDS[1],
        "provider-physical-attempt=sha256:"
        "d4f8f231e159c0b5daa55a05d18d7ccb016ed52211e2987ad987372ebe2daac8",
    ),
    (
        INCIDENT_SEQUENCE_IDS[2],
        "provider-physical-attempt=sha256:"
        "1889b3fd3b09ae18b511fff36ff1e319ce8b0c9c4e8697b67fd4ac9fe1820f82",
    ),
    (
        INCIDENT_SEQUENCE_IDS[3],
        "provider-physical-attempt=sha256:"
        "49eb75b10fdb4ed11b0f5c8d7ab04e4ac9b13d5f7f13cfaa0f958998684a0f3d",
    ),
)
LEGACY_AC7_INCIDENT_SEQUENCE_IDS = INCIDENT_SEQUENCE_IDS[:3]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _typed_value(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bytes):
        return {
            "base64": base64.b64encode(value).decode("ascii"),
            "type": "blob",
        }
    if isinstance(value, bool):
        raise TypeError("SQLite row unexpectedly returned bool")
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "real", "value": value.hex()}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    raise TypeError(f"unsupported SQLite value type: {type(value).__name__}")


def _open_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
    )
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_inventory(
    connection: sqlite3.Connection,
    table: str,
    *,
    identity_columns: tuple[str, ...],
) -> dict[str, object]:
    columns = tuple(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    )
    if not columns:
        raise RuntimeError(f"missing protected table: {table}")
    missing = set(identity_columns).difference(columns)
    if missing:
        raise RuntimeError(
            f"{table} is missing identity columns: {sorted(missing)}"
        )
    rows = connection.execute(
        f"SELECT {', '.join(columns)} FROM {table} "
        f"ORDER BY {', '.join(identity_columns)}"
    ).fetchall()
    entries: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        values = dict(zip(columns, row, strict=True))
        identity = tuple(values[column] for column in identity_columns)
        if identity in seen:
            raise RuntimeError(f"duplicate protected identity in {table}: {identity}")
        seen.add(identity)
        typed_row = {
            column: _typed_value(values[column])
            for column in columns
        }
        entries.append(
            {
                "identity": {
                    column: values[column]
                    for column in identity_columns
                },
                "row_sha256": _digest(typed_row),
            }
        )
    return {
        "columns": list(columns),
        "count": len(entries),
        "inventory_sha256": _digest(entries),
        "rows": entries,
    }


def _provider_ledger_inventory(
    path: Path,
    *,
    enforce_expected_counts: bool,
) -> dict[str, object]:
    with _open_read_only(path) as connection:
        sequences = _table_inventory(
            connection,
            "provider_request_sequences",
            identity_columns=("sequence_id",),
        )
        attempts = _table_inventory(
            connection,
            "provider_request_attempts",
            identity_columns=("sequence_id", "attempt_index"),
        )
        if enforce_expected_counts and sequences["count"] != EXPECTED_SEQUENCE_COUNT:
            raise RuntimeError(
                "protected provider sequence count is not the immutable "
                f"baseline: {sequences['count']} != {EXPECTED_SEQUENCE_COUNT}"
            )
        if enforce_expected_counts and attempts["count"] != EXPECTED_ATTEMPT_COUNT:
            raise RuntimeError(
                "protected provider attempt count is not the immutable "
                f"baseline: {attempts['count']} != {EXPECTED_ATTEMPT_COUNT}"
            )
        placeholders = ", ".join("?" for _ in INCIDENT_SEQUENCE_IDS)
        incident_sequences = connection.execute(
            "SELECT sequence_id FROM provider_request_sequences "
            f"WHERE sequence_id IN ({placeholders}) ORDER BY sequence_id",
            INCIDENT_SEQUENCE_IDS,
        ).fetchall()
        incident_attempts = connection.execute(
            "SELECT sequence_id, attempt_event_id, operation, "
            "upstream_service_name FROM provider_request_attempts "
            f"WHERE sequence_id IN ({placeholders}) ORDER BY sequence_id, attempt_index",
            INCIDENT_SEQUENCE_IDS,
        ).fetchall()
    if tuple(row[0] for row in incident_sequences) != tuple(
        sorted(INCIDENT_SEQUENCE_IDS)
    ):
        raise RuntimeError("one or more historical pytest incident sequences is missing")
    if tuple((row[0], row[1]) for row in incident_attempts) != tuple(
        sorted(INCIDENT_IDENTITIES)
    ) or any(
        row[2:] != ("market-snapshot", "Yahoo Finance")
        for row in incident_attempts
    ):
        raise RuntimeError("historical pytest incident attempt identity changed")
    sequence_digests = {
        str(entry["identity"]["sequence_id"]): entry["row_sha256"]
        for entry in sequences["rows"]
    }
    attempt_digests = {
        (
            str(entry["identity"]["sequence_id"]),
            int(entry["identity"]["attempt_index"]),
        ): entry["row_sha256"]
        for entry in attempts["rows"]
    }
    return {
        "attempts": attempts,
        "incident_rows": [
            {
                "attempt_event_id": attempt_event_id,
                "attempt_row_sha256": attempt_digests[(sequence_id, 1)],
                "sequence_id": sequence_id,
                "sequence_row_sha256": sequence_digests[sequence_id],
            }
            for sequence_id, attempt_event_id, _operation, _service
            in incident_attempts
        ],
        "sequences": sequences,
    }


def _calendar_inventory(path: Path) -> dict[str, object]:
    with _open_read_only(path) as connection:
        calendars = _table_inventory(
            connection,
            "market_session_calendars",
            identity_columns=("calendar_revision_id",),
        )
        sessions = _table_inventory(
            connection,
            "market_sessions",
            identity_columns=("calendar_revision_id", "session_date"),
        )
        revisions = [
            {
                "calendar_revision_id": row[0],
                "payload_digest": row[1],
                "session_count": int(row[2]),
                "open_session_count": int(row[3]),
                "closed_session_count": int(row[4]),
                "first_session": row[5],
                "last_session": row[6],
            }
            for row in connection.execute(
                "SELECT calendar.calendar_revision_id, calendar.payload_digest, "
                "COUNT(session.session_date), "
                "SUM(CASE WHEN session.session_status = 'open' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN session.session_status = 'closed' THEN 1 ELSE 0 END), "
                "MIN(session.session_date), MAX(session.session_date) "
                "FROM market_session_calendars AS calendar "
                "JOIN market_sessions AS session "
                "ON session.calendar_revision_id = calendar.calendar_revision_id "
                "GROUP BY calendar.calendar_revision_id "
                "ORDER BY calendar.calendar_revision_id"
            )
        ]
    if not revisions:
        raise RuntimeError("production Market Session Calendar revision is missing")
    return {
        "calendar_rows": calendars,
        "revisions": revisions,
        "session_rows": sessions,
    }


def _file_inventory(root: Path, *, include_rows: bool) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    rows = [
        {
            "byte_length": path.stat().st_size,
            "relative_path": path.relative_to(resolved).as_posix(),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(
            (item for item in resolved.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(resolved).as_posix(),
        )
    ]
    result: dict[str, object] = {
        "byte_length": sum(int(row["byte_length"]) for row in rows),
        "file_count": len(rows),
        "inventory_sha256": _digest(rows),
    }
    if include_rows:
        result["files"] = rows
    return result


def _capture(
    args: argparse.Namespace,
    *,
    enforce_expected_counts: bool,
) -> dict[str, object]:
    environment = {name: os.environ.get(name) for name in RUN_LIVE_ENVIRONMENT}
    if set(environment.values()) != {"0"}:
        raise RuntimeError(
            "every RUN_LIVE_* opt-in must be explicitly disabled: "
            f"{environment}"
        )
    return {
        "calendar": _calendar_inventory(args.calendar_database),
        "checkpoints": _file_inventory(args.checkpoints_root, include_rows=False),
        "provider_ledger": _provider_ledger_inventory(
            args.provider_ledger,
            enforce_expected_counts=enforce_expected_counts,
        ),
        "reports": _file_inventory(args.reports_root, include_rows=False),
        "run_live_environment": environment,
        "schema_version": "ticket06-post-incident-immutable-inventory-v1",
        "ticket07_evidence": _file_inventory(args.ticket07_root, include_rows=True),
    }


def capture_immutable_state(
    *,
    provider_ledger: Path,
    calendar_database: Path,
    ticket07_root: Path,
    reports_root: Path,
    checkpoints_root: Path,
    enforce_expected_counts: bool = True,
) -> dict[str, object]:
    """Public read-only semantic capture seam used by the ISO-03 host harness."""

    return _capture(
        argparse.Namespace(
            provider_ledger=provider_ledger,
            calendar_database=calendar_database,
            ticket07_root=ticket07_root,
            reports_root=reports_root,
            checkpoints_root=checkpoints_root,
        ),
        enforce_expected_counts=enforce_expected_counts,
    )


def _provider_delta(
    before: dict[str, Any],
    after: dict[str, Any],
    table: str,
) -> dict[str, int]:
    def by_identity(inventory: dict[str, Any]) -> dict[str, str]:
        return {
            _canonical_json(row["identity"]).decode("utf-8"): row["row_sha256"]
            for row in inventory["provider_ledger"][table]["rows"]
        }

    old = by_identity(before)
    new = by_identity(after)
    return {
        "appended": len(new.keys() - old.keys()),
        "deleted": len(old.keys() - new.keys()),
        "rewritten": sum(
            old[key] != new[key]
            for key in old.keys() & new.keys()
        ),
    }


def _comparison_inventory(
    expected: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Preserve the frozen three-row named projection without hiding row deltas."""

    expected_incidents = expected.get("provider_ledger", {}).get("incident_rows")
    if not isinstance(expected_incidents, list):
        return current
    expected_ids = {row.get("sequence_id") for row in expected_incidents}
    if expected_ids != set(LEGACY_AC7_INCIDENT_SEQUENCE_IDS):
        return current
    projected = json.loads(json.dumps(current))
    projected["provider_ledger"]["incident_rows"] = [
        row
        for row in projected["provider_ledger"]["incident_rows"]
        if row["sequence_id"] in LEGACY_AC7_INCIDENT_SEQUENCE_IDS
    ]
    return projected


def _summary(inventory: dict[str, Any]) -> str:
    provider = inventory["provider_ledger"]
    calendar = inventory["calendar"]["revisions"]
    ticket07 = inventory["ticket07_evidence"]
    return (
        f"provider={provider['sequences']['count']}/"
        f"{provider['attempts']['count']} "
        f"sequence_sha256={provider['sequences']['inventory_sha256']} "
        f"attempt_sha256={provider['attempts']['inventory_sha256']} "
        f"calendar={calendar} "
        f"ticket07={ticket07['file_count']}/"
        f"{ticket07['byte_length']}/"
        f"{ticket07['inventory_sha256']}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-ledger", required=True, type=Path)
    parser.add_argument("--calendar-database", required=True, type=Path)
    parser.add_argument("--ticket07-root", required=True, type=Path)
    parser.add_argument("--reports-root", required=True, type=Path)
    parser.add_argument("--checkpoints-root", required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write-baseline", type=Path)
    action.add_argument("--write-audit-snapshot", type=Path)
    action.add_argument("--compare", type=Path)
    return parser.parse_args()


def _write_inventory(path: Path, inventory: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            inventory,
            handle,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


def main() -> int:
    args = _parse_args()
    current = _capture(
        args,
        enforce_expected_counts=args.write_baseline is not None,
    )
    if args.write_baseline is not None:
        _write_inventory(args.write_baseline, current)
        print(f"BASELINE RECORDED: {_summary(current)}")
        return 0
    if args.write_audit_snapshot is not None:
        _write_inventory(args.write_audit_snapshot, current)
        print(f"AUDIT SNAPSHOT RECORDED: {_summary(current)}")
        return 0

    assert args.compare is not None
    expected = json.loads(args.compare.read_text(encoding="utf-8"))
    sequence_delta = _provider_delta(expected, current, "sequences")
    attempt_delta = _provider_delta(expected, current, "attempts")
    comparison_current = _comparison_inventory(expected, current)
    if comparison_current != expected:
        print(f"IMMUTABILITY FAILURE: {_summary(current)}")
        print(f"sequence_delta={sequence_delta}")
        print(f"attempt_delta={attempt_delta}")
        for section in (
            "calendar",
            "checkpoints",
            "reports",
            "ticket07_evidence",
        ):
            print(f"{section}_unchanged={current[section] == expected[section]}")
        print(
            "incident_rows_unchanged="
            f"{current['provider_ledger']['incident_rows'] == expected['provider_ledger']['incident_rows']}"
        )
        return 1
    print(f"IMMUTABILITY PASS: {_summary(current)}")
    print(f"sequence_delta={sequence_delta}")
    print(f"attempt_delta={attempt_delta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

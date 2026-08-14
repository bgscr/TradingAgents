from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import verify_ticket06_immutable_state as verifier

_EXTRA_SEQUENCE_ID = "provider-request-sequence=sha256:" + ("e" * 64)
_APPENDED_SEQUENCE_ID = "provider-request-sequence=sha256:" + ("f" * 64)


@dataclass(frozen=True)
class _VerifierPaths:
    provider_ledger: Path
    calendar_database: Path
    ticket07_root: Path
    reports_root: Path
    checkpoints_root: Path


@pytest.fixture(autouse=True)
def _disable_live_opt_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in verifier.RUN_LIVE_ENVIRONMENT:
        monkeypatch.setenv(name, "0")


@pytest.fixture()
def verifier_paths(tmp_path: Path) -> _VerifierPaths:
    paths = _VerifierPaths(
        provider_ledger=tmp_path / "provider.sqlite3",
        calendar_database=tmp_path / "calendar.sqlite3",
        ticket07_root=tmp_path / "ticket07-evidence",
        reports_root=tmp_path / "reports",
        checkpoints_root=tmp_path / "checkpoints",
    )
    _create_provider_ledger(paths.provider_ledger)
    _create_calendar_database(paths.calendar_database)
    for root, name in (
        (paths.ticket07_root, "evidence.json"),
        (paths.reports_root, "report.md"),
        (paths.checkpoints_root, "checkpoint.db"),
    ):
        root.mkdir(parents=True)
        (root / name).write_text(f"immutable-{name}", encoding="utf-8")
    return paths


def _create_provider_ledger(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE provider_request_sequences (
                sequence_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE provider_request_attempts (
                sequence_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL,
                attempt_event_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                upstream_service_name TEXT NOT NULL,
                outcome TEXT NOT NULL,
                PRIMARY KEY (sequence_id, attempt_index)
            );
            """
        )
        for sequence_id, attempt_event_id in verifier.INCIDENT_IDENTITIES:
            _insert_provider_row(
                connection,
                sequence_id=sequence_id,
                attempt_event_id=attempt_event_id,
            )
        _insert_provider_row(
            connection,
            sequence_id=_EXTRA_SEQUENCE_ID,
            attempt_event_id="extra-attempt",
        )


def _insert_provider_row(
    connection: sqlite3.Connection,
    *,
    sequence_id: str,
    attempt_event_id: str,
) -> None:
    connection.execute(
        "INSERT INTO provider_request_sequences (sequence_id, status) VALUES (?, ?)",
        (sequence_id, "completed"),
    )
    connection.execute(
        "INSERT INTO provider_request_attempts "
        "(sequence_id, attempt_index, attempt_event_id, operation, "
        "upstream_service_name, outcome) VALUES (?, 1, ?, ?, ?, ?)",
        (
            sequence_id,
            attempt_event_id,
            "market-snapshot",
            "Yahoo Finance",
            "available",
        ),
    )


def _create_calendar_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE market_session_calendars (
                calendar_revision_id TEXT PRIMARY KEY,
                payload_digest TEXT NOT NULL
            );
            CREATE TABLE market_sessions (
                calendar_revision_id TEXT NOT NULL,
                session_date TEXT NOT NULL,
                session_status TEXT NOT NULL,
                PRIMARY KEY (calendar_revision_id, session_date)
            );
            INSERT INTO market_session_calendars VALUES ('calendar-1', 'digest-1');
            INSERT INTO market_sessions VALUES ('calendar-1', '2026-08-13', 'open');
            """
        )


def _arguments(paths: _VerifierPaths) -> argparse.Namespace:
    return argparse.Namespace(
        provider_ledger=paths.provider_ledger,
        calendar_database=paths.calendar_database,
        ticket07_root=paths.ticket07_root,
        reports_root=paths.reports_root,
        checkpoints_root=paths.checkpoints_root,
        write_baseline=None,
        write_audit_snapshot=None,
        compare=None,
    )


def _cli_arguments(paths: _VerifierPaths) -> list[str]:
    return [
        "verify_ticket06_immutable_state.py",
        "--provider-ledger",
        str(paths.provider_ledger),
        "--calendar-database",
        str(paths.calendar_database),
        "--ticket07-root",
        str(paths.ticket07_root),
        "--reports-root",
        str(paths.reports_root),
        "--checkpoints-root",
        str(paths.checkpoints_root),
    ]


def _write_comparison_snapshot(paths: _VerifierPaths, destination: Path) -> None:
    inventory = verifier._capture(_arguments(paths), enforce_expected_counts=False)
    destination.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_provider_rows(paths: _VerifierPaths) -> None:
    with sqlite3.connect(paths.provider_ledger) as connection:
        _insert_provider_row(
            connection,
            sequence_id=_APPENDED_SEQUENCE_ID,
            attempt_event_id="appended-attempt",
        )


def _delete_provider_rows(paths: _VerifierPaths) -> None:
    with sqlite3.connect(paths.provider_ledger) as connection:
        connection.execute(
            "DELETE FROM provider_request_attempts WHERE sequence_id = ?",
            (_EXTRA_SEQUENCE_ID,),
        )
        connection.execute(
            "DELETE FROM provider_request_sequences WHERE sequence_id = ?",
            (_EXTRA_SEQUENCE_ID,),
        )


def _rewrite_provider_rows(paths: _VerifierPaths) -> None:
    with sqlite3.connect(paths.provider_ledger) as connection:
        connection.execute(
            "UPDATE provider_request_sequences SET status = 'failed' "
            "WHERE sequence_id = ?",
            (_EXTRA_SEQUENCE_ID,),
        )
        connection.execute(
            "UPDATE provider_request_attempts SET outcome = 'provider_error' "
            "WHERE sequence_id = ?",
            (_EXTRA_SEQUENCE_ID,),
        )


@pytest.mark.parametrize(
    ("mutation", "delta_name"),
    (
        (_append_provider_rows, "appended"),
        (_delete_provider_rows, "deleted"),
        (_rewrite_provider_rows, "rewritten"),
    ),
)
def test_compare_detects_every_provider_row_mutation_kind(
    verifier_paths: _VerifierPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutation: Callable[[_VerifierPaths], None],
    delta_name: str,
) -> None:
    comparison = tmp_path / "comparison.json"
    _write_comparison_snapshot(verifier_paths, comparison)
    mutation(verifier_paths)
    monkeypatch.setattr(
        sys,
        "argv",
        [*_cli_arguments(verifier_paths), "--compare", str(comparison)],
    )

    assert verifier.main() == 1

    output = capsys.readouterr().out
    assert "IMMUTABILITY FAILURE" in output
    assert f"'{delta_name}': 1" in output
    for other in {"appended", "deleted", "rewritten"} - {delta_name}:
        assert f"'{other}': 0" in output


@pytest.mark.parametrize(
    ("section", "relative_path"),
    (
        ("checkpoints", "checkpoint.db"),
        ("reports", "report.md"),
        ("ticket07_evidence", "evidence.json"),
    ),
)
def test_compare_detects_protected_file_rewrites(
    verifier_paths: _VerifierPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    section: str,
    relative_path: str,
) -> None:
    comparison = tmp_path / "comparison.json"
    _write_comparison_snapshot(verifier_paths, comparison)
    root = {
        "checkpoints": verifier_paths.checkpoints_root,
        "reports": verifier_paths.reports_root,
        "ticket07_evidence": verifier_paths.ticket07_root,
    }[section]
    (root / relative_path).write_text("rewritten", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [*_cli_arguments(verifier_paths), "--compare", str(comparison)],
    )

    assert verifier.main() == 1
    assert f"{section}_unchanged=False" in capsys.readouterr().out


def test_compare_detects_calendar_row_rewrite(
    verifier_paths: _VerifierPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    comparison = tmp_path / "comparison.json"
    _write_comparison_snapshot(verifier_paths, comparison)
    with sqlite3.connect(verifier_paths.calendar_database) as connection:
        connection.execute(
            "UPDATE market_sessions SET session_status = 'closed' "
            "WHERE calendar_revision_id = 'calendar-1'"
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [*_cli_arguments(verifier_paths), "--compare", str(comparison)],
    )

    assert verifier.main() == 1
    assert "calendar_unchanged=False" in capsys.readouterr().out


def test_audit_snapshot_records_without_acceptance_count_enforcement(
    verifier_paths: _VerifierPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "audit-snapshot.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *_cli_arguments(verifier_paths),
            "--write-audit-snapshot",
            str(destination),
        ],
    )

    assert verifier.main() == 0

    recorded = json.loads(destination.read_text(encoding="utf-8"))
    assert recorded["provider_ledger"]["sequences"]["count"] == 5
    assert recorded["provider_ledger"]["attempts"]["count"] == 5
    assert "AUDIT SNAPSHOT RECORDED" in capsys.readouterr().out

    with pytest.raises(FileExistsError):
        verifier.main()


def test_acceptance_baseline_still_enforces_declared_counts(
    verifier_paths: _VerifierPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "accepted-baseline.json"
    monkeypatch.setattr(verifier, "EXPECTED_SEQUENCE_COUNT", 5)
    monkeypatch.setattr(verifier, "EXPECTED_ATTEMPT_COUNT", 5)
    monkeypatch.setattr(
        sys,
        "argv",
        [*_cli_arguments(verifier_paths), "--write-baseline", str(destination)],
    )

    assert verifier.main() == 0
    assert destination.is_file()


def test_declared_post_incident_acceptance_counts_are_frozen() -> None:
    assert (
        verifier.EXPECTED_SEQUENCE_COUNT,
        verifier.EXPECTED_ATTEMPT_COUNT,
    ) == (1024, 1036)


def test_all_four_historical_incident_identities_are_named(
    verifier_paths: _VerifierPaths,
) -> None:
    inventory = verifier._capture(
        _arguments(verifier_paths),
        enforce_expected_counts=False,
    )

    incident_rows = inventory["provider_ledger"]["incident_rows"]
    assert len(incident_rows) == 4
    assert {
        (row["sequence_id"], row["attempt_event_id"])
        for row in incident_rows
    } == set(verifier.INCIDENT_IDENTITIES)


def test_compare_accepts_frozen_three_row_named_inventory_without_rebaseline(
    verifier_paths: _VerifierPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    comparison = tmp_path / "legacy-named-incident-comparison.json"
    _write_comparison_snapshot(verifier_paths, comparison)
    frozen = json.loads(comparison.read_text(encoding="utf-8"))
    frozen["provider_ledger"]["incident_rows"] = [
        row
        for row in frozen["provider_ledger"]["incident_rows"]
        if row["sequence_id"] in verifier.INCIDENT_SEQUENCE_IDS[:3]
    ]
    comparison.write_text(
        json.dumps(frozen, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [*_cli_arguments(verifier_paths), "--compare", str(comparison)],
    )

    assert verifier.main() == 0
    assert "IMMUTABILITY PASS" in capsys.readouterr().out


def test_acceptance_baseline_rejects_incorrect_declared_counts(
    verifier_paths: _VerifierPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "rejected-baseline.json"
    monkeypatch.setattr(verifier, "EXPECTED_SEQUENCE_COUNT", 4)
    monkeypatch.setattr(verifier, "EXPECTED_ATTEMPT_COUNT", 5)
    monkeypatch.setattr(
        sys,
        "argv",
        [*_cli_arguments(verifier_paths), "--write-baseline", str(destination)],
    )

    with pytest.raises(RuntimeError, match="sequence count"):
        verifier.main()

    assert not destination.exists()


def test_read_only_connection_cannot_rewrite_protected_rows(
    verifier_paths: _VerifierPaths,
) -> None:
    with (
        verifier._open_read_only(verifier_paths.provider_ledger) as connection,
        pytest.raises(sqlite3.OperationalError),
    ):
        connection.execute("UPDATE provider_request_sequences SET status = 'failed'")

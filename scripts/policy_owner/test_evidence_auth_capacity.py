"""Real native cumulative pre-challenge/envelope and publication-time boundary fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-fixture", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("new external TEST fixture required")
    output.mkdir()
    (output / "UNAUTHENTICATED-TEST-STORE.txt").write_bytes(
        b"NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n"
    )
    instance = "test-20300101-905"

    def canonical(value):
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")

    cases = []

    def check(name, operation, payload, rejection=None):
        result = subprocess.run(
            [str(args.native_fixture), str(output), instance],
            input=canonical({"operation": operation, "payload": payload}),
            capture_output=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=output,
        )
        item = {
            "case": name,
            "exit": result.returncode,
            "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace"),
            "expected_rejection": rejection,
        }
        cases.append(item)
        (output / "cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
        assert (
            (result.returncode != 0 and rejection in item["stderr"])
            if rejection
            else result.returncode == 0
        ), item
        return json.loads(result.stdout) if not result.returncode else None

    document = {"label": "EXPLICIT CAPACITY FIXTURE ONLY", "execution_authorized": False}
    envelope = 256 * 1024
    at_limit = 32 * 1024**2 - (len(canonical(document)) + envelope + 1)
    for delta in (-1, 0, 1):
        value = check(
            "current-journal-plus-next-record-" + str(delta),
            "fixture-prechallenge-capacity",
            {"journal_bytes": at_limit + delta, "documents": [document]},
            "before human challenge" if delta == 1 else None,
        )
        if value:
            assert (
                value["projected_journal_bytes"] == 32 * 1024**2 + delta
                and value["challenge_created"] is False
            )
    for delta in (-1, 0, 1):
        check(
            "actual-envelope-" + str(delta),
            "fixture-envelope-boundary",
            {"bytes": envelope + delta},
            "reserved bound" if delta == 1 else None,
        )
    route = check(
        "cumulative64proposed-documents",
        "fixture-prechallenge-capacity",
        {"journal_bytes": 103223, "documents": [dict(document, ordinal=i) for i in range(64)]},
    )
    assert (
        route["proposed_documents"] == 64
        and route["ledger_contents_read"] is False
        and route["ledger_state_queried"] is False
    )
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    digest = hashlib.sha256(canonical(document)).hexdigest()
    validation = {
        "contract_version": "policy-owner-authentication-validation-v2",
        "validated": True,
        "instance": instance,
        "purpose": "REGISTER_EVIDENCE",
        "document_sha256": digest,
        "valid_from_utc": start.isoformat(),
        "valid_until_utc": end.isoformat(),
        "execution_authorized": False,
    }

    def window(at, **changes):
        return {
            "validation": dict(validation, **changes),
            "purpose": "REGISTER_EVIDENCE",
            "document_sha256": digest,
            "at_utc": at.isoformat(),
        }

    check(
        "before-validity",
        "fixture-validation-window",
        window(start - timedelta(microseconds=1)),
        "not yet effective",
    )
    check("exact-lower-bound", "fixture-validation-window", window(start))
    check(
        "just-before-exclusive-expiry",
        "fixture-validation-window",
        window(end - timedelta(microseconds=1)),
    )
    check(
        "expired-during-expensive-validation", "fixture-validation-window", window(end), "expired"
    )
    check(
        "expired-during-capacity-serialization",
        "fixture-validation-window",
        window(end + timedelta(microseconds=1)),
        "expired",
    )
    check(
        "private-result-wrong-body",
        "fixture-validation-window",
        window(start, document_sha256="0" * 64),
        "binding changed",
    )
    check(
        "private-result-wrong-instance",
        "fixture-validation-window",
        window(start, instance="test-20300101-906"),
        "binding changed",
    )
    check(
        "private-result-wrong-purpose",
        "fixture-validation-window",
        window(start, purpose="EXACT_POLICY_DECISION"),
        "binding changed",
    )
    report = {
        "status": "PASS_NATIVE_CAPACITY_AND_PUBLICATION_WINDOW_FIXTURES",
        "cases": cases,
        "record_created": False,
        "human_authenticated": False,
        "clock_changed": False,
        "explicit_fixture_instants_only": True,
        "complete_route_projection": route,
        "execution_authorized": False,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "cases": len(cases)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

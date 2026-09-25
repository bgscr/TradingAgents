"""Exercise native pre-provision rejection with fresh, synthetic test DB copies.

No setup UI, service, enrollment or owner RPC is available from this harness.
Mutated SQLite rows are deliberately invalid fixtures, never issued claims.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def run(proposal_path: Path, executable: Path, output: Path) -> dict:
    from build_policy_owner import canonical, pin, write_new

    proposal = json.loads(proposal_path.read_bytes())
    if not proposal["instance"].startswith("test-"):
        raise ValueError("only explicitly isolated test setup inputs are allowed")
    if executable.name != "PolicyAuthenticationContracts.exe":
        raise ValueError("the inert fixture executable is required")
    output = output.resolve(strict=False)
    source = Path(__file__).resolve().parents[2]
    if output.exists() or output == source or output.is_relative_to(source):
        raise ValueError("fresh external output directory required")
    output.mkdir()
    original = proposal_path.parent / "depot"
    cases = {
        "pristine": None,
        "started-clock": ("owner", "UPDATE metadata SET last_clock='2001-01-01T00:00:00Z'"),
        "imported-approval": (
            "owner",
            "INSERT INTO approvals VALUES ('INVALID-TEST','INVALID-TEST',x'7b7d','INVALID-TEST')",
        ),
        "applied-authentication": (
            "owner",
            "INSERT INTO applied_records VALUES ('INVALID-TEST',x'7b7d')",
        ),
        "consumed-claim": (
            "lineage",
            "INSERT INTO claims VALUES ('INVALID-TEST','INVALID-TEST','INVALID-TEST',x'7b7d')",
        ),
        "changed-schema": ("owner", "DROP TRIGGER artifacts_no_update"),
        "wrong-ledger": ("lineage", "UPDATE metadata SET ledger_id='INVALID-TEST'"),
        "invalid-artifact-shape": (
            "owner",
            "INSERT INTO artifacts VALUES ('INVALID-TEST',x'7b7d0a')",
        ),
        "authentication-file": "authentication/00000000.json",
        "temporary-file": "state/temp/INVALID-TEST.tmp",
        "unexpected-state": "state/INVALID-TEST.json",
    }
    results = []
    for name, mutation in cases.items():
        root = output / name
        depot = root / "depot"
        depot.mkdir(parents=True)
        # The state validator only needs these files. The native interactive
        # setup separately requires and pins the complete executable/runtime set.
        for entry in proposal["files"]:
            relative = entry["relative_path"]
            if (
                relative.startswith(("state/", "artifacts/"))
                or relative == "original-acquisitions.json"
            ):
                destination = depot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                write_new(destination, (original / relative).read_bytes())
        if isinstance(mutation, tuple):
            database = sqlite3.connect(depot / "state" / (mutation[0] + ".sqlite3"))
            try:
                with database:
                    database.execute(mutation[1])
            finally:
                database.close()
        elif isinstance(mutation, str):
            destination = depot / mutation
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_new(destination, b"INVALID TEST FIXTURE; NOT AUTHENTICATED\n")
        value = {
            "instance": proposal["instance"],
            "ledger_identity": proposal["ledger_identity"],
            "files": [
                {"relative_path": path.relative_to(depot).as_posix(), **pin(path)}
                for path in sorted(depot.rglob("*"))
                if path.is_file()
            ],
        }
        fixture = root / "TEST-STATE.json"
        write_new(fixture, canonical(value))
        result = subprocess.run(
            [str(executable), "setup-state-check", str(fixture)],
            capture_output=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        write_new(root / "stdout.bin", result.stdout)
        write_new(root / "stderr.bin", result.stderr)
        expected = {
            "started-clock": "unset clock",
            "imported-approval": "cannot import an approval",
            "applied-authentication": "cannot import an approval",
            "consumed-claim": "cannot import consumed lineage",
            "changed-schema": "schema differs",
            "wrong-ledger": "lineage registration mismatch",
            "invalid-artifact-shape": "Evidence field set mismatch",
            "authentication-file": "cannot import authentication",
            "temporary-file": "cannot import authentication",
            "unexpected-state": "cannot import authentication",
        }.get(name, "")
        reason = result.stderr.decode("utf-8", errors="replace")
        passed = result.returncode == (0 if mutation is None else 2) and expected in reason
        results.append(
            {
                "case": name,
                "passed": passed,
                "exit_code": result.returncode,
                "expected_rejection": expected,
                "observed_rejection": reason.splitlines()[:1],
            }
        )
    report = {
        "contract_version": "inert-native-setup-state-tests-v1",
        "fixture_executable": pin(executable),
        "input_proposal": pin(proposal_path),
        "results": results,
        "passed": all(case["passed"] for case in results),
        "service_registered": False,
        "principal_enrolled": False,
        "qualification_claimed": False,
        "execution_authorized": False,
    }
    write_new(output / "result.json", canonical(report))
    return report


def main() -> int:
    if os.name != "nt" or not (
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    ):
        raise ValueError("use the existing Windows interpreter with -I -B -S")
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("proposal", "executable", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    report = run(args.proposal, args.executable, args.output)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

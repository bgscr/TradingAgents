"""Actual standalone CLI rehearsal on newly created disposable objects only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--test-dependencies", type=Path, required=True)
    p.add_argument("--delivered-dependencies", type=Path, required=True)
    p.add_argument("--delivered-depot", type=Path)
    args = p.parse_args()
    out = args.output.absolute()
    out.mkdir(exist_ok=False)
    source = Path(__file__).resolve().parents[2]
    if out.is_relative_to(source) or out.is_relative_to(Path(os.environ["PROGRAMDATA"])):
        raise ValueError("new external disposable CLI evidence required")
    sys.path[:0] = [str(source), str(args.test_dependencies)]
    from tests.unit.test_policy_coverage_native import native_fixture
    from tradingagents.policy_qualified_contract import canonical_bytes

    root, old, scope, measurement = native_fixture(out)
    for phase, raw in old.items():
        (out / ("original-" + phase + ".json")).write_bytes(raw)
    (out / "candidate.json").write_bytes(measurement.candidate.to_bytes())
    cli = source / "scripts/policy_owner/coverage_evidence.py"
    base = [
        sys.executable,
        "-I",
        "-B",
        "-S",
        str(cli),
        "--development-dependencies",
        str(args.delivered_dependencies),
        "--fixture-only",
    ]
    if args.delivered_depot:
        depot = args.delivered_depot.resolve(strict=True)
        base = [
            str(depot / "python/python.exe"),
            "-I",
            "-B",
            "-S",
            str(depot / "source/scripts/policy_owner/coverage_evidence.py"),
            "--fixture-only",
        ]
    results = []

    def run(name, *argv, expected=0):
        completed = subprocess.run(
            base + list(map(str, argv)),
            cwd=out,
            env=os.environ.copy(),
            capture_output=True,
            timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (out / (name + ".stdout")).write_bytes(completed.stdout)
        (out / (name + ".stderr")).write_bytes(completed.stderr)
        results.append(
            {
                "name": name,
                "arguments": base + list(map(str, argv)),
                "cwd": str(out),
                "exit_code": completed.returncode,
                "expected": expected,
            }
        )
        (out / "COMMANDS.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        if completed.returncode != expected:
            raise ValueError(name + ": " + completed.stderr.decode("utf-8", "replace"))

    run(
        "scope",
        "scope",
        "--before",
        out / "original-before.json",
        "--after",
        out / "original-after.json",
        "--output",
        out / "scope.json",
    )
    run(
        "measurement",
        "measurement",
        "--scope",
        out / "scope.json",
        "--candidate",
        out / "candidate.json",
        "--output",
        out / "measurement.json",
    )
    common = ["--scope", out / "scope.json", "--measurement", out / "measurement.json"]
    run(
        "before",
        "capture",
        *common,
        "--instance",
        "test-20990101-999",
        "--phase",
        "before",
        "--output",
        out / "before.json",
    )
    run(
        "assessment",
        "assess",
        *common,
        "--original-before",
        out / "original-before.json",
        "--original-after",
        out / "original-after.json",
        "--baseline",
        out / "before.json",
        "--output",
        out / "assessment.json",
    )
    run(
        "after",
        "capture",
        *common,
        "--instance",
        "test-20990101-999",
        "--phase",
        "after",
        "--output",
        out / "after.json",
    )
    run(
        "compare",
        "compare",
        *common,
        "--before",
        out / "before.json",
        "--after",
        out / "after.json",
        "--output",
        out / "comparison.json",
    )
    run(
        "preflight",
        "preflight",
        *common,
        "--original-before",
        out / "original-before.json",
        "--original-after",
        out / "original-after.json",
        "--baseline-fixture",
        out / "before.json",
        "--after-fixture",
        out / "after.json",
        "--output",
        out / "preflight.json",
    )
    prior = hashlib.sha256((out / "before.json").read_bytes()).hexdigest()
    run(
        "existing-output",
        "capture",
        *common,
        "--instance",
        "test-20990101-999",
        "--phase",
        "before",
        "--output",
        out / "before.json",
        expected=1,
    )
    assert hashlib.sha256((out / "before.json").read_bytes()).hexdigest() == prior
    (root / "known.txt").write_bytes(b"deliberate current change")
    run(
        "changed",
        "capture",
        *common,
        "--instance",
        "test-20990101-999",
        "--phase",
        "after",
        "--output",
        out / "changed.json",
    )
    run(
        "difference",
        "compare",
        *common,
        "--before",
        out / "before.json",
        "--after",
        out / "changed.json",
        "--output",
        out / "difference.json",
        expected=2,
    )
    result = {
        "status": "FIXTURE_FEASIBILITY_PASS",
        "real_environment": "NOT_MEASURED",
        "registered_owner": "NOT_CALLED",
        "human_authentication": "PENDING",
        "qualification": "NOT_AUTHORIZED",
        "commands": len(results),
        "complete_bytes": sum(x.stat().st_size for x in out.glob("*.json")),
        "execution_authorized": False,
    }
    (out / "RESULT.json").write_bytes(canonical_bytes(result) + b"\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()

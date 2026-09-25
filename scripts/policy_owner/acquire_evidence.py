"""Produce/independently verify current source or capture explicit roots additively.

No service request, authentication, allocation or claim is implemented here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-dependencies", type=Path)
    parser.add_argument("--fixture-only", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    produce = sub.add_parser("produce-source")
    produce.add_argument("--repository-root", type=Path, required=True)
    produce.add_argument("--candidate", type=Path, required=True)
    produce.add_argument("--output", type=Path, required=True)
    produce.add_argument("--git-executable", type=Path, required=True)
    produce.add_argument("--git-sha256", required=True)
    verify = sub.add_parser("verify-source")
    verify.add_argument("--inventory", type=Path, required=True)
    verify.add_argument("--candidate", type=Path, required=True)
    verify.add_argument("--repository-root", type=Path, required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--root-list", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--phase", choices=["before", "after"], required=True)
    capture.add_argument("--previous", type=Path)
    capture.add_argument("--maximum-seconds", type=int, default=600)
    capture.add_argument("--maximum-entries", type=int, default=2_000_000)
    capture.add_argument("--maximum-read-bytes", type=int, default=64 * 1024**3)
    compare = sub.add_parser("compare")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("use the delivered interpreter with -I -B -S")
    source = Path(__file__).resolve().parents[2]
    depot = source.parent
    if args.development_dependencies:
        if not args.fixture_only:
            raise ValueError("development dependency input cannot produce operational readiness")
        dependencies = args.development_dependencies.resolve(strict=True)
    else:
        if Path(sys.executable).resolve() != depot / "python/python.exe":
            raise ValueError("actual delivered interpreter/source ancestry is required")
        dependencies = depot / "dependencies"
    sys.path[:0] = [str(source), str(dependencies)]
    from tradingagents.policy_evidence_acquisition import (
        ProductionInventory,
        capture_roots,
        compare_captures,
        new_evidence_output,
        produce_production_inventory,
        validate_production_inventory,
    )
    from tradingagents.policy_evidence_contract import MAX_ARTIFACT_BYTES
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import Candidate, canonical_bytes, strict_json

    bound_evidence_process(
        seconds=min(1860, args.maximum_seconds + 60) if args.command == "capture" else 1860
    )

    def read(path):
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("input exceeds declared complete evidence capacity")
        return strict_json(path.read_bytes())

    if args.command == "produce-source":
        result = produce_production_inventory(
            args.repository_root,
            args.output,
            expected=Candidate.read(read(args.candidate)),
            fixture_only=args.fixture_only,
            git_executable=args.git_executable,
            git_sha256=args.git_sha256,
        )
        print(
            json.dumps(
                {
                    "status": "SOURCE_INVENTORY_CREATED_NOT_AUTHENTICATED",
                    "identity": result.identity,
                    "candidate": result.candidate.model_dump(mode="json"),
                    "fixture_only": result.fixture_only,
                    "execution_authorized": False,
                }
            )
        )
        return 0
    if args.command == "verify-source":
        inventory = ProductionInventory.read(read(args.inventory))
        if (
            inventory.candidate != Candidate.read(read(args.candidate))
            or inventory.fixture_only != args.fixture_only
        ):
            raise ValueError("independent source/candidate/fixture binding mismatch")
        validate_production_inventory(inventory, args.repository_root)
        print(
            json.dumps(
                {
                    "status": "SOURCE_BYTES_INDEPENDENTLY_VERIFIED_NOT_AUTHENTICATED",
                    "identity": inventory.identity,
                    "execution_authorized": False,
                }
            )
        )
        return 0
    if args.command == "capture":
        request = read(args.root_list)
        if request.get("fixture_only") != args.fixture_only:
            raise ValueError("root-list fixture scope mismatch")
        result = capture_roots(
            request,
            args.output,
            phase=args.phase,
            previous=read(args.previous) if args.previous else None,
            maximum_seconds=args.maximum_seconds,
            maximum_entries=args.maximum_entries,
            maximum_read_bytes=args.maximum_read_bytes,
        )
        print(
            json.dumps(
                {
                    "status": "NEW_OBSERVATION_NOT_AUTHENTICATED",
                    "identity": result["identity"],
                    "observation_identity": result["capture"]["observation_identity"],
                    "complete": result["capture"]["complete"],
                    "fixture_only": args.fixture_only,
                    "execution_authorized": False,
                }
            )
        )
        return 0 if result["capture"]["complete"] else 2
    result = compare_captures(read(args.before), read(args.after))
    with new_evidence_output(args.output) as stream:
        stream.write(canonical_bytes(result) + b"\n")
    print(
        json.dumps(
            {"status": "COMPARISON_ONLY_NOT_QUALIFICATION", **result, "execution_authorized": False}
        )
    )
    return 0 if result["equal"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

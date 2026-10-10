"""Versioned scope preparation, explicit capture and non-consuming comparisons.

This CLI never contacts an owner, authenticates, allocates, claims or runs stages.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-dependencies", type=Path)
    parser.add_argument("--fixture-only", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    scope_cmd = sub.add_parser("scope")
    scope_cmd.add_argument("--before", type=Path, required=True)
    scope_cmd.add_argument("--after", type=Path, required=True)
    scope_cmd.add_argument("--mixed-roots", action="store_true")
    measure_cmd = sub.add_parser("measurement")
    measure_cmd.add_argument("--scope", type=Path, required=True)
    measure_cmd.add_argument("--candidate", type=Path, required=True)
    measure_cmd.add_argument("--maximum-seconds", type=int, default=600)
    capture_cmd = sub.add_parser("capture")
    capture_cmd.add_argument("--instance", required=True)
    capture_cmd.add_argument("--phase", choices=["before", "after"], required=True)
    assess_cmd = sub.add_parser("assess")
    assess_cmd.add_argument("--original-before", type=Path, required=True)
    assess_cmd.add_argument("--original-after", type=Path, required=True)
    assess_cmd.add_argument("--baseline", type=Path, required=True)
    compare_cmd = sub.add_parser("compare")
    compare_cmd.add_argument("--before", type=Path, required=True)
    compare_cmd.add_argument("--after", type=Path, required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--original-before", type=Path, required=True)
    preflight.add_argument("--original-after", type=Path, required=True)
    preflight.add_argument("--baseline-fixture", type=Path)
    preflight.add_argument("--after-fixture", type=Path)
    preflight.add_argument("--policy", type=Path)
    for command in (capture_cmd, assess_cmd, compare_cmd, preflight):
        command.add_argument("--scope", type=Path, required=True)
        command.add_argument("--measurement", type=Path, required=True)
    for command in (scope_cmd, measure_cmd, capture_cmd, assess_cmd, compare_cmd, preflight):
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("delivered -I -B -S interpreter required")
    source = Path(__file__).resolve().parents[2]
    depot = source.parent
    if args.development_dependencies:
        if not args.fixture_only:
            raise ValueError("development dependencies are fixture-only")
        dependencies = args.development_dependencies.resolve(strict=True)
    else:
        if Path(sys.executable).resolve() != depot / "python/python.exe":
            raise ValueError("actual delivered interpreter/source ancestry required")
        dependencies = depot / "dependencies"
    sys.path[:0] = [str(source), str(dependencies)]
    from tradingagents.policy_coverage_contract import (
        MAX_INVENTORY_BYTES,
        CoverageMeasurement,
        RootTypedCoverageMeasurement,
        RootTypedCoverageScope,
        artifact_ref,
        build_scope,
        compare_current_inventories,
        compare_known_fields,
        read_coverage_context,
        read_scope,
        validate_scope,
    )
    from tradingagents.policy_evidence_acquisition import new_evidence_output
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import Candidate, canonical_bytes, strict_json

    bound_evidence_process(seconds=1860)

    def raw(path):
        info = path.lstat()
        if (
            info.st_nlink != 1
            or info.st_file_attributes & 0x400
            or info.st_size > MAX_INVENTORY_BYTES
        ):
            raise ValueError("input alias/reparse/capacity mismatch")
        value = path.read_bytes()
        after = path.lstat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("preparation input changed during read")
        return value

    def read(path):
        return strict_json(raw(path))

    if args.output.exists():
        raise FileExistsError("fresh additive output required")
    if args.command == "scope":
        result = build_scope(
            raw(args.before),
            raw(args.after),
            fixture_only=args.fixture_only,
            mixed_roots=args.mixed_roots,
        )
    else:
        scope = read_scope(read(args.scope))
        if scope.fixture_only != args.fixture_only:
            raise ValueError("explicit fixture scope mismatch")
        if args.command == "measurement":
            mixed = isinstance(scope, RootTypedCoverageScope)
            model = RootTypedCoverageMeasurement if mixed else CoverageMeasurement
            result = model.seal(
                contract_version="policy-coverage-measurement-v3"
                if mixed
                else "policy-coverage-measurement-v2",
                **({"root_kind_policy": "EXACT_SAVED_ROOT_KINDS"} if mixed else {}),
                scope=artifact_ref(scope),
                candidate=Candidate.read(read(args.candidate)),
                supported_reparse_tags=(0xA0000003,),
                security_required=True,
                maximum_entries=2_000_000,
                maximum_read_bytes=64 * 1024**3,
                maximum_seconds=args.maximum_seconds,
                maximum_depth=128,
                maximum_reparse_bytes=16384,
                maximum_indirect_link_depth=0,
                target_coverage="SEPARATE_ORDINARY_ROOT_WALK",
                known_fields_required=True,
            )
        else:
            scope, measurement = read_coverage_context(scope, read(args.measurement))
            if args.command == "capture":
                from tradingagents.policy_coverage_native import capture_coverage

                value = capture_coverage(
                    scope,
                    measurement,
                    args.output,
                    instance=args.instance,
                    phase=args.phase,
                    fixture_only=args.fixture_only,
                )
                print(
                    json.dumps(
                        {
                            "status": "NEW_COVERAGE_OBSERVATION_NOT_AUTHENTICATED",
                            "identity": value["identity"],
                            "complete": value["capture"]["complete"],
                            "execution_authorized": False,
                        }
                    )
                )
                return 0 if value["capture"]["complete"] else 2
            if args.command == "assess":
                result = compare_known_fields(
                    scope,
                    raw(args.original_before),
                    raw(args.original_after),
                    read(args.baseline),
                    measurement,
                )
            elif args.command == "compare":
                result = compare_current_inventories(
                    read(args.before), read(args.after), scope, measurement
                )
            else:
                before, after = raw(args.original_before), raw(args.original_after)
                validate_scope(scope, before, after)
                if args.policy:
                    from tradingagents.policy_coverage_models import CoveragePolicy

                    policy = CoveragePolicy.read(read(args.policy))
                    if (
                        policy.coverage_scope != artifact_ref(scope)
                        or policy.coverage_measurement != measurement
                    ):
                        raise ValueError("preflight policy/source/measurement/scope mismatch")
                witness = False
                if bool(args.baseline_fixture) != bool(args.after_fixture):
                    raise ValueError("a complete two-observation fixture witness is required")
                if args.baseline_fixture:
                    if not args.fixture_only:
                        raise ValueError("fixture witness cannot be relabelled as real environment")
                    baseline, later = read(args.baseline_fixture), read(args.after_fixture)
                    compare_known_fields(scope, before, after, baseline, measurement)
                    if not compare_current_inventories(baseline, later, scope, measurement)[
                        "equal"
                    ]:
                        raise ValueError("fixture witness has current-window drift")
                    witness = True
                result = {
                    "contract_version": "policy-coverage-preflight-v2",
                    "IMPLEMENTATION_PASS": "FIXED_CONTRACT_CHECKS_ONLY",
                    "FIXTURE_FEASIBILITY_PASS": witness,
                    "REAL_ENVIRONMENT_NOT_MEASURED": True,
                    "HUMAN_AUTHENTICATION_PENDING": True,
                    "QUALIFICATION_NOT_AUTHORIZED": True,
                    "scope": artifact_ref(scope).model_dump(mode="json"),
                    "measurement": artifact_ref(measurement).model_dump(mode="json"),
                    "candidate": measurement.candidate.model_dump(mode="json"),
                    "maximum_artifact_bytes": MAX_INVENTORY_BYTES,
                    "registered_capacity": "UNKNOWN_NOT_QUERIED",
                    "deployment_check_required": "preflight_evidence.py exact delivered manifest/native/dependency preflight",
                    "execution_authorized": False,
                }
    payload = result.to_bytes() if hasattr(result, "to_bytes") else canonical_bytes(result) + b"\n"
    if len(payload) > MAX_INVENTORY_BYTES:
        raise ValueError("complete output exceeds bounded artifact capacity")
    with new_evidence_output(args.output) as stream:
        stream.write(payload)
    print(
        json.dumps(
            {
                "status": "COVERAGE_PREPARATION_NOT_APPROVAL",
                "output": str(args.output),
                "identity": getattr(result, "identity", None),
                "execution_authorized": False,
            }
        )
    )
    return 0 if not isinstance(result, dict) or result.get("equal", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())

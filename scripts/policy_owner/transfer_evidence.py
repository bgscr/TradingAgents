"""Stage/verify/export complete evidence; never authenticate, allocate or claim."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--development-dependencies", type=Path)
    parser.add_argument("--fixture-native", type=Path)
    parser.add_argument("--fixture-store", type=Path)
    parser.add_argument("--coverage-scope", type=Path)
    parser.add_argument("--coverage-measurement", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--artifact", type=Path, required=True)
    stage.add_argument("--descriptor", type=Path, required=True)
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument(
        "--acquisition-registration",
        action="store_true",
        help="Explicit v3 fresh acquisition descriptor; requires a coverage-policy decision at later authentication",
    )
    export = sub.add_parser("export")
    export.add_argument("--reference", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact", type=Path, required=True)
    validate.add_argument("--descriptor", type=Path, required=True)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("use the delivered interpreter with -I -B -S")
    source = Path(__file__).resolve().parents[2]
    depot = source.parent
    fixture = args.fixture_native is not None
    if args.development_dependencies:
        if not fixture or not args.instance.startswith("test-"):
            raise ValueError(
                "external development dependencies are restricted to explicit headless TEST fixtures"
            )
        dependencies = args.development_dependencies.resolve(strict=True)
    else:
        if Path(sys.executable).resolve() != depot / "python/python.exe":
            raise ValueError("the actual delivered interpreter must belong to this source depot")
        dependencies = depot / "dependencies"
    sys.path[:0] = [str(source), str(dependencies)]
    from tradingagents.policy_evidence_client import export_artifact, stage_artifact
    from tradingagents.policy_evidence_contract import ArtifactDescriptor, validate_artifact
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import EvidenceRef, canonical_bytes, strict_json

    bound_evidence_process(seconds=3660)
    coverage_context = None
    if bool(args.coverage_scope) != bool(args.coverage_measurement):
        raise ValueError("complete coverage scope and measurement context required")
    if args.coverage_scope:
        from tradingagents.policy_coverage_contract import (
            CoverageMeasurement,
            HistoricalCoverageScope,
        )

        coverage_context = (
            HistoricalCoverageScope.read(args.coverage_scope.read_bytes()),
            CoverageMeasurement.read(args.coverage_measurement.read_bytes()),
        )

    if fixture:
        if (
            not args.instance.startswith("test-")
            or not args.fixture_store
            or args.fixture_native.name != "EvidenceTransferContracts.exe"
        ):
            raise ValueError("headless fixture cannot target an operational instance")

        def call(operation, payload):
            result = subprocess.run(
                [str(args.fixture_native), str(args.fixture_store), args.instance],
                input=canonical_bytes({"operation": operation, "payload": payload}),
                capture_output=True,
                timeout=35,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise ValueError(result.stderr.decode("utf-8", "replace"))
            return strict_json(result.stdout)
    else:
        from tradingagents.windows_policy_owner import RegisteredWindowsPolicyVerifier

        # Validation mode never constructs an owner or queries/changes a service.
        call = (
            None
            if args.command == "validate"
            else RegisteredWindowsPolicyVerifier(args.instance).call
        )
    if args.command in {"stage", "validate"}:
        descriptor = ArtifactDescriptor.read(args.descriptor.read_bytes())
        if descriptor.instance != args.instance:
            raise ValueError("descriptor instance mismatch")
        if args.command == "validate":
            validate_artifact(args.artifact, descriptor, coverage_context=coverage_context)
            print(
                json.dumps(
                    {
                        "status": "COMPLETE_BYTES_VALIDATED_NOT_AUTHENTICATED",
                        "descriptor": descriptor.identity,
                        "owner_called": False,
                        "execution_authorized": False,
                    }
                )
            )
            return 0
        progress_path = args.output.with_name(args.output.name + ".progress.jsonl")
        if args.output.exists() or progress_path.exists():
            raise FileExistsError(
                "intake output/progress already exists; no automatic staging retry"
            )
        with progress_path.open("xb") as log:

            def progress(record):
                log.write(canonical_bytes(record) + b"\n")
                log.flush()

            result = stage_artifact(
                call,
                args.artifact,
                descriptor,
                progress=progress,
                coverage_context=coverage_context,
                acquisition_registration=args.acquisition_registration,
            )
        with args.output.open("xb") as stream:
            stream.write(canonical_bytes(result))
        print(
            json.dumps(
                {
                    "status": "STAGED_ONLY_HUMAN_REGISTRATION_REQUIRED",
                    "intake_document": str(args.output),
                    "fixture_only": fixture,
                    "execution_authorized": False,
                }
            )
        )
    else:
        result = export_artifact(call, EvidenceRef.read(args.reference.read_bytes()), args.output)
        print(
            json.dumps({"status": "COMPLETE_ARTIFACT_EXPORTED", "fixture_only": fixture, **result})
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

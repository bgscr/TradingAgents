from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from tradingagents.windows_sandbox_qualification import (
    ISO03QualificationStatus,
    execute_iso03_boundary_qualification,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run only the ISO-03 Windows Sandbox boundary qualification."
    )
    parser.add_argument("--implementation-repository-root", required=True, type=Path)
    parser.add_argument("--reviewed-source-repository-root", required=True, type=Path)
    parser.add_argument("--protected-state-repository-root", required=True, type=Path)
    parser.add_argument("--historical-qualification-runs-root", required=True, type=Path)
    parser.add_argument("--replacement-qualification-run-root", required=True, type=Path)
    parser.add_argument(
        "--qualified-replacement-qualification-root",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--failed-iso04-ticket06-acceptance-root",
        required=True,
        type=Path,
    )
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--runtime-bundle-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--user-profile", required=True, type=Path)
    parser.add_argument("--host-temp", required=True, type=Path)
    parser.add_argument("--host-venv", required=True, type=Path)
    parser.add_argument(
        "--attest-protected-writers-quiesced",
        action="store_true",
        help=(
            "Attest that no process will write protected roots from the trusted "
            "sentinel inventory through the independent host AFTER inventory."
        ),
    )
    parser.add_argument("--minimal-timeout-seconds", type=int, default=180)
    parser.add_argument("--qualification-timeout-seconds", type=int, default=1800)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if not 30 <= arguments.minimal_timeout_seconds <= 600:
        raise SystemExit("minimal timeout must be between 30 and 600 seconds")
    if not 300 <= arguments.qualification_timeout_seconds <= 3600:
        raise SystemExit("qualification timeout must be between 300 and 3600 seconds")
    arguments.runs_root.mkdir(parents=True, exist_ok=True)
    report = execute_iso03_boundary_qualification(
        implementation_repository_root=arguments.implementation_repository_root,
        reviewed_source_repository_root=(
            arguments.reviewed_source_repository_root
        ),
        protected_state_repository_root=(
            arguments.protected_state_repository_root
        ),
        historical_qualification_runs_root=(
            arguments.historical_qualification_runs_root
        ),
        replacement_qualification_run_root=(
            arguments.replacement_qualification_run_root
        ),
        qualified_replacement_qualification_root=(
            arguments.qualified_replacement_qualification_root
        ),
        failed_iso04_ticket06_acceptance_root=(
            arguments.failed_iso04_ticket06_acceptance_root
        ),
        runs_root=arguments.runs_root,
        runtime_bundle_root=arguments.runtime_bundle_root,
        run_id=arguments.run_id,
        user_profile=arguments.user_profile,
        host_temp=arguments.host_temp,
        host_venv=arguments.host_venv,
        host_entrypoint_path=Path(__file__),
        minimal_timeout_seconds=arguments.minimal_timeout_seconds,
        qualification_timeout_seconds=arguments.qualification_timeout_seconds,
        protected_writers_quiesced=(
            arguments.attest_protected_writers_quiesced
        ),
    )
    print(
        json.dumps(
            report.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    if report.status is ISO03QualificationStatus.PASS:
        return os.EX_OK
    if report.status is ISO03QualificationStatus.NEEDS_INFO:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""ISO-01 no-launch Windows Sandbox runner construction command."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from tradingagents.windows_sandbox_runner import (
    ENVIRONMENT_POLICY_CONTRACT_VERSION,
    GUEST_LAYOUT_CONTRACT_VERSION,
    MAPPING_MANIFEST_CONTRACT_VERSION,
    PROTECTED_ROOT_MANIFEST_VERSION,
    RESULT_SCHEMA_CONTRACT_VERSION,
    RUNNER_CONTRACT_VERSION,
    SOURCE_INPUT_CONTRACT_VERSION,
    WSB_CONFIGURATION_CONTRACT_VERSION,
    RunnerConstructionRequest,
    SandboxContractError,
    SandboxExecutionMode,
    SandboxFailureReason,
    SandboxPlatform,
    SandboxQualificationOutcome,
    WindowsSandboxPlatform,
    WindowsSandboxRunnerService,
    default_protected_root_bindings,
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Construct and validate ISO-01 inputs without launching Sandbox.",
    )
    parser.add_argument("validate", choices=("validate",))
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-bundle-root", type=Path)
    parser.add_argument(
        "--execution-mode",
        required=True,
        choices=tuple(mode.value for mode in SandboxExecutionMode),
    )
    return parser


def _resolve_cli_path(path: Path, *, reason: SandboxFailureReason) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SandboxContractError(reason) from exc


def main(
    argv: list[str] | None = None,
    *,
    platform: SandboxPlatform | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    user_profile_raw = os.environ.get("USERPROFILE")
    if not user_profile_raw:
        payload = {
            "contract_version": "windows-sandbox-no-launch-output-v1",
            "status": SandboxQualificationOutcome.NEEDS_INFO.value,
            "reason": "host-user-profile-unavailable",
            "sandbox_launched": False,
        }
        print(_canonical_json(payload))
        return 2
    try:
        repository = _resolve_cli_path(
            arguments.repository_root,
            reason=SandboxFailureReason.SOURCE_REPOSITORY_INVALID,
        )
        user_profile = _resolve_cli_path(
            Path(user_profile_raw),
            reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
        )
        request = RunnerConstructionRequest(
            repository_root=repository,
            runs_root=arguments.runs_root,
            run_id=arguments.run_id,
            execution_mode=SandboxExecutionMode(arguments.execution_mode),
            protected_root_bindings=default_protected_root_bindings(
                user_profile=user_profile,
                repository_root=repository,
            ),
            user_profile=user_profile,
            host_temp=Path(tempfile.gettempdir()),
            host_venv=repository / ".venv",
            runtime_bundle_root=arguments.runtime_bundle_root,
        )
        service = WindowsSandboxRunnerService(platform=platform or WindowsSandboxPlatform())
        contract = service.validate_only(request)
    except SandboxContractError as exc:
        payload = {
            "contract_version": "windows-sandbox-no-launch-output-v1",
            "status": exc.qualification_outcome.value,
            "reason": exc.reason.value,
            "sandbox_launched": False,
            "ticket06_executed": False,
            "ticket07_executed": False,
            "provider_model_io": False,
        }
        print(_canonical_json(payload))
        return 2 if exc.qualification_outcome is SandboxQualificationOutcome.NEEDS_INFO else 1

    payload = {
        "contract_version": "windows-sandbox-no-launch-output-v1",
        "status": contract.qualification_outcome.value,
        "no_launch_output_identity": contract.no_launch_output_identity,
        "runner_identity": contract.runner_identity,
        "runner_contract_version": RUNNER_CONTRACT_VERSION,
        "protected_root_manifest_version": PROTECTED_ROOT_MANIFEST_VERSION,
        "source_input_contract_version": SOURCE_INPUT_CONTRACT_VERSION,
        "wsb_configuration_contract_version": WSB_CONFIGURATION_CONTRACT_VERSION,
        "mapping_manifest_contract_version": MAPPING_MANIFEST_CONTRACT_VERSION,
        "guest_layout_contract_version": GUEST_LAYOUT_CONTRACT_VERSION,
        "environment_policy_contract_version": ENVIRONMENT_POLICY_CONTRACT_VERSION,
        "result_schema_contract_version": RESULT_SCHEMA_CONTRACT_VERSION,
        "execution_mode": contract.result_template.execution_mode.value,
        "writable_host_mapping_count": sum(
            not mapping.read_only for mapping in contract.mapping_manifest.mappings
        ),
        "networking": contract.wsb_configuration.policy.networking,
        "runtime_bundle_state": contract.runtime_bundle.state,
        "result_export_file_count": 0,
        "sandbox_launched": False,
        "ticket06_executed": False,
        "ticket07_executed": False,
        "provider_model_io": False,
        "host_qcf17_authority_established": False,
    }
    print(_canonical_json(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

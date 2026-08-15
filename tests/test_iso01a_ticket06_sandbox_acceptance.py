from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.windows_sandbox.run_ticket06_acceptance import (
    BoundedProcessObservation,
    execute_ticket06_acceptance,
)
from tradingagents.windows_sandbox_acceptance import (
    QUALIFIED_RUNNER_TARGET_FILES,
    AcceptanceCommandStatus,
    Ticket06AcceptanceContractError,
    Ticket06AcceptanceResult,
    Ticket06AcceptanceStaticBindings,
    build_ticket06_acceptance_result,
    build_ticket06_command_ledger,
    build_ticket06_command_result,
    build_ticket06_completion_marker,
    build_ticket06_control_environment,
    validate_ticket06_acceptance_export,
)
from tradingagents.windows_sandbox_runner import SandboxExecutionMode


def _identity(namespace: str, fill: str = "b") -> str:
    return f"{namespace}:sha256:{fill * 64}"


def _static_bindings(
    command_ledger_identity: str,
    environment_policy_identity: str,
) -> Ticket06AcceptanceStaticBindings:
    return Ticket06AcceptanceStaticBindings(
        source_commit_id="1" * 40,
        source_tree_hash="2" * 40,
        source_input_identity=_identity("windows-sandbox-reviewed-source:v1"),
        source_manifest_identity=_identity("windows-sandbox-source-manifest:v1"),
        source_archive_sha256="3" * 64,
        reconstructed_source_tree_identity=_identity(
            "windows-sandbox-reconstructed-source:v1"
        ),
        runtime_bundle_identity=_identity("windows-offline-runtime-bundle:v1"),
        runtime_bundle_manifest_sha256="4" * 64,
        runtime_bundle_contract_identity=_identity(
            "windows-sandbox-runtime-bundle-input:v1"
        ),
        runner_identity=_identity("iso01-windows-sandbox-runner:v1"),
        runner_input_identity=_identity("windows-sandbox-qualified-runner-input:v2"),
        wsb_config_identity=_identity("windows-sandbox-config:v1"),
        protected_root_manifest_identity=_identity(
            "windows-sandbox-protected-roots:v1"
        ),
        mapping_manifest_identity=_identity("windows-sandbox-mapping-manifest:v1"),
        guest_workspace_identity=_identity("windows-sandbox-guest-workspace:v1"),
        environment_policy_identity=environment_policy_identity,
        control_identity=_identity("windows-sandbox-ticket06-control:v1"),
        frozen_baseline_identity=_identity("ticket06-frozen-baseline:v1"),
        boundary_qualification_identity=_identity("iso03-boundary-qualification:v1"),
        command_ledger_identity=command_ledger_identity,
    )


def _process_environment():
    return build_ticket06_control_environment(
        (
            ("PYTHON_DOTENV_DISABLED", "1"),
            ("RUN_LIVE_DEEPSEEK_TESTS", "0"),
            ("RUN_LIVE_PROVIDER_TESTS", "0"),
            ("RUN_LIVE_TUSHARE_TESTS", "0"),
        )
    )


def test_iso_01a_command_ledger_is_closed_complete_and_deterministic() -> None:
    environment_identity = (
        "windows-sandbox-environment-policy:v1:sha256:" + ("a" * 64)
    )

    ledger = build_ticket06_command_ledger(environment_identity)

    assert tuple(entry.command_id for entry in ledger.entries) == (
        "systemic_persistence_isolation",
        "cal01_acquisition_attempts",
        "cal02_publication_exact_reader",
        "cal03_programmatic_cli_bootstrap",
        "cal04_deterministic_acceptance",
        "temporal_06b",
        "qcf_matrix",
        "composition_contracts",
        "eligibility_context",
        "production_source_bindings",
        "indicator_metadata",
        "provider_adapters",
        "provider_coordination",
        "graph_shadow_pe",
        "checkpoint_reporting_evidence",
        "history_security_checks",
        "market_history_ac",
        "financial_period_pe",
        "live_test_isolation",
        "non_integration",
        "full_suite_a",
        "full_suite_b",
        "ruff",
        "diff_whitespace",
        "workspace_status",
        "offline_spec_compliance_review",
        "offline_code_quality_review",
    )
    assert tuple(entry.ordinal for entry in ledger.entries) == tuple(range(1, 28))
    assert all(entry.mandatory for entry in ledger.entries)
    assert all(entry.working_directory_role == "guest_workspace" for entry in ledger.entries)
    assert all(
        entry.environment_policy_identity == environment_identity
        for entry in ledger.entries
    )
    full_suite_argv = [
        entry.argv for entry in ledger.entries if entry.command_id.startswith("full_suite_")
    ]
    assert full_suite_argv == [
        ("$PYTHON", "-m", "pytest", "-q"),
        ("$PYTHON", "-m", "pytest", "-q"),
    ]
    serialized = ledger.canonical_json_bytes().decode("utf-8").casefold()
    assert "ticket07" not in serialized
    assert "ticket_07" not in serialized
    assert "qualified_v1" not in serialized
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert "provider/model" not in serialized
    assert build_ticket06_command_ledger(
        _identity("windows-sandbox-environment-policy:v1", "c")
    ).ledger_identity != ledger.ledger_identity


def test_iso_01a_execution_modes_are_exactly_closed() -> None:
    assert tuple(mode.value for mode in SandboxExecutionMode) == (
        "boundary_qualification",
        "ticket06_acceptance",
    )
    with pytest.raises(ValueError):
        SandboxExecutionMode("unknown_mode")


def test_iso_01a_export_is_complete_and_keeps_qcf17_qcf18_host_owned(
    tmp_path: Path,
) -> None:
    environment = _process_environment()
    ledger = build_ticket06_command_ledger(environment.policy_identity)
    static = _static_bindings(ledger.ledger_identity, environment.policy_identity)
    command_results = tuple(
        build_ticket06_command_result(
            entry,
            status=AcceptanceCommandStatus.PASS,
            returncode=0,
            passed=1,
        )
        for entry in ledger.entries
    )

    result = build_ticket06_acceptance_result(
        static=static,
        ledger=ledger,
        command_results=command_results,
        live_environment=(
            ("PYTHON_DOTENV_DISABLED", "1"),
            ("RUN_LIVE_DEEPSEEK_TESTS", "0"),
            ("RUN_LIVE_PROVIDER_TESTS", "0"),
            ("RUN_LIVE_TUSHARE_TESTS", "0"),
        ),
    )
    result_bytes = result.canonical_json_bytes()
    marker = build_ticket06_completion_marker(result=result, result_bytes=result_bytes)
    export = tmp_path / "export"
    export.mkdir()
    (export / "result.json").write_bytes(result_bytes)
    (export / "completion.json").write_bytes(marker.canonical_json_bytes())

    validated = validate_ticket06_acceptance_export(export, expected=static)

    assert validated.result.guest_claim == "pass"
    assert tuple(item.criterion_id for item in validated.result.qcf) == tuple(
        f"QCF{number}" for number in range(1, 19)
    )
    assert tuple(item.criterion_id for item in validated.result.pe) == tuple(
        f"PE{number}" for number in range(1, 16)
    )
    assert tuple(item.criterion_id for item in validated.result.ac) == tuple(
        f"AC{number}" for number in range(1, 14)
    )
    assert validated.result.authority.qcf17_guest_clause_a == "pass"
    assert validated.result.authority.qcf17_host_clause_b == "not_reported_host_owned"
    assert validated.result.authority.qcf17_final == "not_reported_host_owned"
    assert validated.result.authority.qcf18_final == (
        "not_reported_conditional_on_host_qcf17"
    )
    assert validated.host_qcf17_authority is False
    assert validated.host_qcf18_authority is False

    incomplete = json.loads(result_bytes)
    incomplete["command_results"].pop()
    with pytest.raises(ValueError, match="incomplete or unordered"):
        Ticket06AcceptanceResult.model_validate(incomplete)

    incomplete = json.loads(result_bytes)
    incomplete["ac"].pop()
    with pytest.raises(ValueError, match="AC evidence is incomplete"):
        Ticket06AcceptanceResult.model_validate(incomplete)

    tampered = json.loads(result_bytes)
    tampered["authority"]["qcf17_host_clause_b"] = "pass"
    (export / "result.json").write_text(
        json.dumps(tampered, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(Ticket06AcceptanceContractError):
        validate_ticket06_acceptance_export(export, expected=static)

    tampered = json.loads(result_bytes)
    tampered["authority"]["qcf18_final"] = "pass"
    (export / "result.json").write_text(
        json.dumps(tampered, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(Ticket06AcceptanceContractError):
        validate_ticket06_acceptance_export(export, expected=static)

    (export / "result.json").write_bytes(result_bytes)
    (export / "completion.json").write_bytes(marker.canonical_json_bytes())
    result_time = (export / "result.json").stat().st_mtime_ns
    os.utime(
        export / "completion.json",
        ns=(result_time - 1_000, result_time - 1_000),
    )
    with pytest.raises(Ticket06AcceptanceContractError):
        validate_ticket06_acceptance_export(export, expected=static)


def test_iso_01a_orchestrator_executes_fixed_order_and_writes_completion_last(
    tmp_path: Path,
) -> None:
    environment = _process_environment()
    ledger = build_ticket06_command_ledger(environment.policy_identity)
    static = _static_bindings(ledger.ledger_identity, environment.policy_identity)
    workspace = tmp_path / "workspace"
    results = tmp_path / "results"
    workspace.mkdir()
    results.mkdir()
    calls: list[tuple[tuple[str, ...], Path, dict[str, str], int]] = []

    def execute(
        argv: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> BoundedProcessObservation:
        assert maximum_output_bytes == 262_144
        calls.append((argv, cwd, environment, timeout_seconds))
        if "pytest" in argv:
            output = b"1 passed in 0.01s\n"
        elif argv[-2:] == ("--stage", "spec_compliance"):
            output = b"ticket06-offline-spec_compliance-review:v1:sha256:" + b"a" * 64
        elif argv[-2:] == ("--stage", "code_quality"):
            output = b"ticket06-offline-code_quality-review:v1:sha256:" + b"b" * 64
        else:
            output = b""
        return BoundedProcessObservation(returncode=0, output=output)

    result = execute_ticket06_acceptance(
        static=static,
        ledger=ledger,
        live_environment=environment.variables,
        workspace=workspace,
        results_root=results,
        python_executable=Path(r"C:\QCF17\Venv\Scripts\python.exe"),
        git_executable=Path(r"C:\QCF17\Input\Runtime\git\cmd\git.exe"),
        executor=execute,
    )

    assert result.guest_claim == "pass"
    assert tuple(call[0] for call in calls) == tuple(
        tuple(
            str(Path(r"C:\QCF17\Venv\Scripts\python.exe"))
            if argument == "$PYTHON"
            else str(Path(r"C:\QCF17\Input\Runtime\git\cmd\git.exe"))
            if argument == "$GIT"
            else argument
            for argument in entry.argv
        )
        for entry in ledger.entries
    )
    assert all(call[1] == workspace.resolve() for call in calls)
    assert all(
        call[2]["RUN_LIVE_DEEPSEEK_TESTS"] == "0"
        and call[2]["RUN_LIVE_PROVIDER_TESTS"] == "0"
        and call[2]["RUN_LIVE_TUSHARE_TESTS"] == "0"
        and call[2]["PYTHON_DOTENV_DISABLED"] == "1"
        for call in calls
    )
    assert all(
        call[2]["PATH"]
        == (
            r"C:\QCF17\Venv\Scripts;C:\QCF17\Input\Runtime\git\cmd;"
            r"C:\Windows\System32;C:\Windows"
        )
        and call[2]["SYSTEMROOT"] == r"C:\Windows"
        and call[2]["GIT_TERMINAL_PROMPT"] == "0"
        for call in calls
    )
    assert tuple(path.name for path in sorted(results.iterdir())) == (
        "completion.json",
        "result.json",
    )
    assert (results / "completion.json").stat().st_mtime_ns >= (
        results / "result.json"
    ).stat().st_mtime_ns


def test_iso_01a_orchestrator_stops_after_mandatory_failure_and_exports_failure(
    tmp_path: Path,
) -> None:
    environment = _process_environment()
    ledger = build_ticket06_command_ledger(environment.policy_identity)
    static = _static_bindings(ledger.ledger_identity, environment.policy_identity)
    workspace = tmp_path / "workspace"
    results = tmp_path / "results"
    workspace.mkdir()
    results.mkdir()
    calls: list[str] = []

    def execute(
        argv: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> BoundedProcessObservation:
        del cwd, environment, timeout_seconds, maximum_output_bytes
        command_id = ledger.entries[len(calls)].command_id
        calls.append(command_id)
        if len(calls) == 3:
            return BoundedProcessObservation(
                returncode=1,
                output=b"1 failed in 0.01s\nraw provider exception must not export",
            )
        output = b"1 passed in 0.01s\n" if "pytest" in argv else b""
        return BoundedProcessObservation(returncode=0, output=output)

    result = execute_ticket06_acceptance(
        static=static,
        ledger=ledger,
        live_environment=environment.variables,
        workspace=workspace,
        results_root=results,
        python_executable=Path(r"C:\QCF17\Venv\Scripts\python.exe"),
        git_executable=Path(r"C:\QCF17\Input\Runtime\git\cmd\git.exe"),
        executor=execute,
    )

    assert calls == [entry.command_id for entry in ledger.entries[:3]]
    assert result.guest_claim == "fail"
    assert result.command_results[2].status == "fail"
    assert all(item.status == "not_run" for item in result.command_results[3:])
    exported = (results / "result.json").read_bytes().lower()
    assert b"raw provider exception" not in exported
    assert (results / "completion.json").is_file()


def test_iso_01a_pytest_skip_or_xfail_only_cannot_pass_acceptance(
    tmp_path: Path,
) -> None:
    environment = _process_environment()
    ledger = build_ticket06_command_ledger(environment.policy_identity)
    static = _static_bindings(ledger.ledger_identity, environment.policy_identity)
    workspace = tmp_path / "workspace"
    results = tmp_path / "results"
    workspace.mkdir()
    results.mkdir()
    calls = 0

    def execute(
        argv: tuple[str, ...],
        cwd: Path,
        process_environment: dict[str, str],
        timeout_seconds: int,
        maximum_output_bytes: int,
    ) -> BoundedProcessObservation:
        nonlocal calls
        del argv, cwd, process_environment, timeout_seconds, maximum_output_bytes
        calls += 1
        return BoundedProcessObservation(returncode=0, output=b"1 skipped, 1 xfailed")

    result = execute_ticket06_acceptance(
        static=static,
        ledger=ledger,
        live_environment=environment.variables,
        workspace=workspace,
        results_root=results,
        python_executable=Path(r"C:\QCF17\Venv\Scripts\python.exe"),
        git_executable=Path(r"C:\QCF17\Input\Runtime\git\cmd\git.exe"),
        executor=execute,
    )

    assert calls == 1
    assert result.guest_claim == "fail"
    assert result.command_results[0].status == "fail"
    assert result.command_results[0].skipped == 1
    assert result.command_results[0].xfailed == 1
    assert all(item.status == "not_run" for item in result.command_results[1:])


def test_iso_01a_orchestrator_rejects_environment_outside_bound_policy(
    tmp_path: Path,
) -> None:
    environment = _process_environment()
    ledger = build_ticket06_command_ledger(environment.policy_identity)
    static = _static_bindings(ledger.ledger_identity, environment.policy_identity)
    workspace = tmp_path / "workspace"
    results = tmp_path / "results"
    workspace.mkdir()
    results.mkdir()
    tampered = tuple(
        (name, r"C:\Host\bin" if name == "PATH" else value)
        for name, value in environment.variables
    )

    def must_not_execute(*_args, **_kwargs) -> BoundedProcessObservation:
        raise AssertionError("invalid environment must fail before process execution")

    with pytest.raises(ValueError, match="environment_policy_invalid"):
        execute_ticket06_acceptance(
            static=static,
            ledger=ledger,
            live_environment=tampered,
            workspace=workspace,
            results_root=results,
            python_executable=Path(r"C:\QCF17\Venv\Scripts\python.exe"),
            git_executable=Path(r"C:\QCF17\Input\Runtime\git\cmd\git.exe"),
            executor=must_not_execute,
        )

    assert not any(results.iterdir())


def test_iso_01a_sealed_runner_and_bootstrap_dispatch_are_closed() -> None:
    assert QUALIFIED_RUNNER_TARGET_FILES == (
        "bootstrap.ps1",
        "install_boundary_runtime.ps1",
        "run_boundary_qualification.py",
        "run_ticket06_acceptance.py",
        "run_ticket06_offline_review.py",
        "ticket06_acceptance_contract.py",
        "verify_source.py",
    )
    repository = Path(__file__).resolve().parents[1]
    bootstrap = (
        repository / "scripts" / "windows_sandbox" / "bootstrap.ps1"
    ).read_text(encoding="utf-8")
    assert '"boundary_qualification"' in bootstrap
    assert '"ticket06_acceptance"' in bootstrap
    assert '"C:\\QCF17\\Input\\Runner\\run_boundary_qualification.py"' in bootstrap
    assert '"C:\\QCF17\\Input\\Runner\\run_ticket06_acceptance.py"' in bootstrap
    assert "$control.base.command" not in bootstrap
    assert "$control.base.script" not in bootstrap
    assert bootstrap.index("unsupported_sandbox_execution_mode") < bootstrap.index(
        '$stage = "purge_credentials"'
    )
    boundary_block = bootstrap.split(
        'if ($executionMode -eq "boundary_qualification")',
        maxsplit=1,
    )[1].split(
        'elseif ($executionMode -eq "ticket06_acceptance")',
        maxsplit=1,
    )[0]
    assert "run_boundary_qualification.py" in boundary_block
    assert "run_ticket06_acceptance.py" not in boundary_block

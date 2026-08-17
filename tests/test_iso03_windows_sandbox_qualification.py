from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from tradingagents.windows_sandbox_acceptance import (
    QUALIFIED_RUNNER_SOURCE_FILES,
    QUALIFIED_RUNNER_TARGET_FILES,
    TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
    TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
    Ticket06AcceptanceStructuralQualification,
    qualify_ticket06_acceptance_runner_structure,
)
from tradingagents.windows_sandbox_qualification import (
    AdversarialProbeResult,
    AdversarialProbeTarget,
    GuestQualificationResult,
    ISO03QualificationStatus,
    LocalWriteResult,
    LocalWriteTarget,
    MappingVerification,
    MinimalSandboxLaunchEvidence,
    NetworkVerification,
    ProbeMechanism,
    ProbeObservation,
    QualificationStaticBindings,
    WindowsHostPlatformFacts,
    WindowsHostQualificationState,
    build_guest_qualification_result,
    build_protected_writer_quiescence_contract,
    build_protected_writer_quiescence_evidence,
    build_qualification_completion_marker,
    build_reviewed_host_harness_manifest,
    capture_protected_host_inventory,
    capture_windows_host_platform_facts,
    compare_protected_host_inventories,
    execute_iso03_boundary_qualification,
    host_path_identity,
    qualify_windows_sandbox_host,
    run_minimal_sandbox_launch,
    select_adversarial_probe_targets,
    stage_boundary_runner_input,
    validate_boundary_qualification_export,
    validate_boundary_runner_input,
    validate_iso03_historical_evidence_root_coverage,
    validate_verifier_root_ownership,
)
from tradingagents.windows_sandbox_runner import (
    ProtectedObjectKind,
    ProtectedRootBinding,
    ProtectedRootRole,
    SandboxContractError,
    SandboxFailureReason,
    SandboxFeatureState,
    SandboxMappingBinding,
    SandboxMappingRole,
    SandboxPlatformReport,
    SandboxProcessResult,
    SandboxProcessTopologyEntry,
    WindowsSandboxPlatform,
    build_environment_policy,
    build_guest_layout,
    build_mapping_manifest,
    build_protected_root_manifest,
    default_protected_root_bindings,
    render_wsb_configuration,
)

_DIRECTORY_ROLES = {
    ProtectedRootRole.APPLICATION_PROFILE,
    ProtectedRootRole.PROVIDER_LEDGER_PAYLOADS,
    ProtectedRootRole.PROVIDER_STATE,
    ProtectedRootRole.CALENDAR_STATE,
    ProtectedRootRole.CALENDAR_PAYLOADS,
    ProtectedRootRole.CHECKPOINTS,
    ProtectedRootRole.REPORTS_LOGS,
    ProtectedRootRole.TICKET07_EVIDENCE,
    ProtectedRootRole.PRIOR_INVENTORIES,
    ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS,
    ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION,
    ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
    ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
}


def _protected_manifest(root: Path):
    bindings = []
    for role in ProtectedRootRole:
        path = root / role.value
        if role in _DIRECTORY_ROLES:
            path.mkdir(parents=True)
            (path / "retained.bin").write_bytes(role.value.encode("ascii"))
            kind = ProtectedObjectKind.DIRECTORY
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(role.value.encode("ascii"))
            kind = ProtectedObjectKind.FILE
        bindings.append(ProtectedRootBinding(role, path, kind))
    return build_protected_root_manifest(tuple(bindings))


def test_iso_03_host_inventory_is_deterministic_and_detects_rewrites(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    semantic = {
        "provider_ledger": {
            "attempts": {"count": 1036, "inventory_sha256": "a" * 64},
            "sequences": {"count": 1024, "inventory_sha256": "b" * 64},
        }
    }

    before = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )
    repeated = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )

    assert repeated == before
    assert len(before.roots) == len(tuple(ProtectedRootRole))
    assert before.inventory_identity.startswith(
        "windows-sandbox-protected-host-inventory:v1:sha256:"
    )

    target = tmp_path / "protected" / ProtectedRootRole.APPLICATION_PROFILE.value
    (target / "retained.bin").write_bytes(b"rewritten")
    after = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )

    delta = compare_protected_host_inventories(before, after)
    assert delta.equal is False
    assert (delta.appended, delta.deleted, delta.rewritten) == (0, 0, 1)


def test_iso_03_quiescence_requires_attestation_and_stable_prelaunch_inventory(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")

    def capture() -> dict[str, object]:
        return {}

    identity = "immutable-verifier:sha256:" + ("c" * 64)
    sentinel = capture_protected_host_inventory(
        manifest,
        semantic_capture=capture,
        immutable_verifier_identity=identity,
    )

    with pytest.raises(SandboxContractError):
        build_protected_writer_quiescence_contract(operator_attested=False)
    contract = build_protected_writer_quiescence_contract(operator_attested=True)
    evidence = build_protected_writer_quiescence_evidence(
        contract=contract,
        sentinel=sentinel,
        immediate_before=sentinel,
    )

    assert evidence.prelaunch_delta.equal is True
    assert evidence.sentinel_inventory_identity == sentinel.inventory_identity

    target = tmp_path / "protected" / ProtectedRootRole.APPLICATION_PROFILE.value
    (target / "retained.bin").write_bytes(b"changed-before-launch")
    changed = capture_protected_host_inventory(
        manifest,
        semantic_capture=capture,
        immutable_verifier_identity=identity,
    )
    with pytest.raises(SandboxContractError):
        build_protected_writer_quiescence_evidence(
            contract=contract,
            sentinel=sentinel,
            immediate_before=changed,
        )


def test_iso_03_verifier_semantics_bind_nonempty_checkpoint_report_and_ticket_roots(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")

    def semantic_row(role: ProtectedRootRole) -> dict[str, object]:
        content = role.value.encode("ascii")
        rows = [
            {
                "byte_length": len(content),
                "relative_path": "retained.bin",
                "sha256": sha256(content).hexdigest(),
            }
        ]
        return {
            "byte_length": len(content),
            "file_count": 1,
            "inventory_sha256": sha256(
                json.dumps(
                    rows,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        }

    semantic = {
        "checkpoints": semantic_row(ProtectedRootRole.CHECKPOINTS),
        "reports": semantic_row(ProtectedRootRole.REPORTS_LOGS),
        "ticket07_evidence": semantic_row(ProtectedRootRole.TICKET07_EVIDENCE),
    }
    inventory = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )

    validate_verifier_root_ownership(inventory)

    narrowed = {
        **semantic,
        "checkpoints": {
            "byte_length": 0,
            "file_count": 0,
            "inventory_sha256": sha256(b"[]").hexdigest(),
        },
    }
    narrowed_inventory = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: narrowed,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )
    with pytest.raises(SandboxContractError):
        validate_verifier_root_ownership(narrowed_inventory)


def test_iso_03_inventory_hashes_internal_symlinks_without_following_outward(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    prior = tmp_path / "protected" / ProtectedRootRole.PRIOR_INVENTORIES.value
    internal = prior / "current"
    try:
        internal.symlink_to(prior / "retained.bin")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    semantic: dict[str, object] = {}

    inventory = capture_protected_host_inventory(
        manifest,
        semantic_capture=lambda: semantic,
        immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
    )

    prior_inventory = next(
        item for item in inventory.roots if item.role is ProtectedRootRole.PRIOR_INVENTORIES
    )
    assert tuple(item.relative_path for item in prior_inventory.files) == (
        "current",
        "retained.bin",
    )

    internal.unlink()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    internal.symlink_to(outside)
    with pytest.raises(SandboxContractError):
        capture_protected_host_inventory(
            manifest,
            semantic_capture=lambda: semantic,
            immutable_verifier_identity="immutable-verifier:sha256:" + ("c" * 64),
        )


def _static_bindings() -> QualificationStaticBindings:
    return QualificationStaticBindings(
        qualification_run_identity="iso03-boundary-qualification-run:v1:sha256:" + ("1" * 64),
        source_commit_id="2" * 40,
        source_tree_hash="3" * 40,
        source_input_identity="windows-sandbox-reviewed-source:v1:sha256:" + ("4" * 64),
        source_manifest_identity="windows-sandbox-source-manifest:v1:sha256:" + ("5" * 64),
        source_archive_sha256="6" * 64,
        runtime_bundle_identity="windows-offline-runtime-bundle:v1:sha256:" + ("7" * 64),
        runtime_bundle_manifest_sha256="8" * 64,
        runtime_bundle_contract_identity=(
            "windows-sandbox-runtime-bundle-input:v1:sha256:" + ("9" * 64)
        ),
        runner_identity="iso01-windows-sandbox-runner:v1:sha256:" + ("a" * 64),
        wsb_config_identity="windows-sandbox-config:v1:sha256:" + ("b" * 64),
        protected_root_manifest_identity=(
            "windows-sandbox-protected-roots:v1:sha256:" + ("c" * 64)
        ),
        mapping_manifest_identity=(
            "windows-sandbox-mapping-manifest:v1:sha256:" + ("d" * 64)
        ),
        guest_workspace_identity=(
            "windows-sandbox-guest-workspace:v1:sha256:" + ("e" * 64)
        ),
        qualification_control_identity=(
            "windows-sandbox-qualification-control:v1:sha256:" + ("f" * 64)
        ),
        platform_qualification_identity=(
            "windows-sandbox-host-platform-qualification:v1:sha256:" + ("0" * 64)
        ),
        immutable_verifier_identity="immutable-verifier:sha256:" + ("1" * 64),
        host_before_inventory_identity=(
            "windows-sandbox-protected-host-inventory:v1:sha256:" + ("2" * 64)
        ),
        acceptance_runner_structure=Ticket06AcceptanceStructuralQualification.create(
            runner_input_identity=(
                "windows-sandbox-qualified-runner-input:v2:sha256:" + ("3" * 64)
            ),
            environment_policy_identity=(
                "windows-sandbox-environment-policy:v1:sha256:" + ("4" * 64)
            ),
            host_harness_manifest_identity=(
                "iso03-reviewed-host-harness:v1:sha256:" + ("5" * 64)
            ),
            host_acceptance_module_sha256="6" * 64,
            boundary_runner_identity=(
                "iso01-windows-sandbox-runner:v1:sha256:" + ("a" * 64)
            ),
            wsb_config_identity=(
                "windows-sandbox-config:v1:sha256:" + ("b" * 64)
            ),
            wsb_config_policy_identity=(
                "windows-sandbox-config-policy:v1:sha256:" + ("c" * 64)
            ),
            mapping_manifest_identity=(
                "windows-sandbox-mapping-manifest:v1:sha256:" + ("d" * 64)
            ),
            command_ledger_identity=TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
            result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        ),
    )


def _passing_guest_result() -> GuestQualificationResult:
    probes = tuple(
        AdversarialProbeResult(
            mechanism=mechanism,
            target_host_path=(
                rf"C:\Users\56922\.tradingagents\{mechanism.value}.probe"
            ),
            target_path_identity=host_path_identity(
                rf"C:\Users\56922\.tradingagents\{mechanism.value}.probe"
            ),
            observation=ProbeObservation.GUEST_LOCAL_ONLY,
            contained=True,
        )
        for mechanism in ProbeMechanism
    )
    writes = tuple(
        LocalWriteResult(target=target, succeeded=True)
        for target in LocalWriteTarget
    )
    return build_guest_qualification_result(
        _static_bindings(),
        mapping=MappingVerification(
            networking_disabled=True,
            read_only_input_mappings=True,
            exactly_one_writable_results_mapping=True,
            forbidden_host_mappings_absent=True,
            guest_read_only_write_checks_passed=True,
        ),
        network=NetworkVerification(
            ipv4_default_route_present=False,
            ipv6_default_route_present=False,
            external_route_usable=False,
        ),
        live_environment=(
            ("PYTHON_DOTENV_DISABLED", "1"),
            ("RUN_LIVE_DEEPSEEK_TESTS", "0"),
            ("RUN_LIVE_PROVIDER_TESTS", "0"),
            ("RUN_LIVE_TUSHARE_TESTS", "0"),
        ),
        probes=probes,
        local_writes=writes,
    )


def _write_export(root: Path, result: GuestQualificationResult) -> None:
    root.mkdir()
    result_bytes = result.canonical_json_bytes() + b"\n"
    (root / "result.json").write_bytes(result_bytes)
    marker = build_qualification_completion_marker(
        result=result,
        result_bytes=result_bytes,
    )
    (root / "completion.json").write_bytes(marker.canonical_json_bytes() + b"\n")


def test_iso_03_bounded_result_validator_accepts_only_complete_pass(
    tmp_path: Path,
) -> None:
    export = tmp_path / "export"
    result = _passing_guest_result()
    _write_export(export, result)

    validated = validate_boundary_qualification_export(
        export,
        expected=_static_bindings(),
    )

    assert validated.result == result
    assert validated.host_qcf17_authority is False


@pytest.mark.parametrize("tamper", ("missing-marker", "extra-file", "malformed"))
def test_iso_03_bounded_result_validator_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    export = tmp_path / tamper
    _write_export(export, _passing_guest_result())
    if tamper == "missing-marker":
        (export / "completion.json").unlink()
    elif tamper == "extra-file":
        (export / "runtime.sqlite3").write_bytes(b"unsafe")
    else:
        (export / "result.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SandboxContractError):
        validate_boundary_qualification_export(export, expected=_static_bindings())


def _supported_platform_facts() -> WindowsHostPlatformFacts:
    return WindowsHostPlatformFacts(
        edition_id="Professional",
        product_name="Microsoft Windows 11 Pro",
        build_number=26100,
        architecture="AMD64",
        hypervisor_present=True,
        virtualization_firmware_enabled=True,
        logical_processor_count=16,
        physical_memory_bytes=32 * 1024**3,
        system_drive_free_bytes=100 * 1024**3,
        host_is_virtual_machine=False,
        nested_virtualization_ready=False,
        systeminfo_identity="windows-systeminfo:sha256:" + ("3" * 64),
    )


def _successful_process_topology() -> tuple[SandboxProcessTopologyEntry, ...]:
    return (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=101,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=102,
        ),
    )


def _minimal_launch(*, succeeded: bool = True) -> MinimalSandboxLaunchEvidence:
    return MinimalSandboxLaunchEvidence.create(
        succeeded=succeeded,
        returncode=0 if succeeded else 1,
        timed_out=False,
        config_identity="windows-sandbox-minimal-config:v1:sha256:" + ("4" * 64),
        marker_sha256="5" * 64 if succeeded else None,
        launched_processes=_successful_process_topology() if succeeded else (),
    )


def test_iso_03_minimal_launch_pass_requires_observed_launcher_image() -> None:
    without_launcher = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=102,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxServer.exe",
            process_id=103,
        ),
    )

    with pytest.raises(ValueError, match="closed process topology"):
        MinimalSandboxLaunchEvidence.create(
            succeeded=True,
            returncode=0,
            timed_out=False,
            config_identity=(
                "windows-sandbox-minimal-config:v1:sha256:" + ("4" * 64)
            ),
            marker_sha256="5" * 64,
            launched_processes=without_launcher,
        )


def test_iso_03_platform_qualification_requires_all_real_host_signals() -> None:
    qualification = qualify_windows_sandbox_host(
        platform_report=SandboxPlatformReport.available(protected_client_supported=True),
        facts=_supported_platform_facts(),
        minimal_launch=_minimal_launch(),
    )

    assert qualification.state is WindowsHostQualificationState.QUALIFIED
    assert qualification.supported_edition is True
    assert qualification.hypervisor_ready is True
    assert qualification.protected_client_ready is True
    assert qualification.minimal_launch_succeeded is True


@pytest.mark.parametrize(
    "facts",
    (
        _supported_platform_facts().model_copy(update={"edition_id": "Core"}),
        _supported_platform_facts().model_copy(update={"hypervisor_present": False}),
        _supported_platform_facts().model_copy(update={"virtualization_firmware_enabled": False}),
    ),
)
def test_iso_03_platform_qualification_does_not_infer_support_from_feature_flag(
    facts: WindowsHostPlatformFacts,
) -> None:
    qualification = qualify_windows_sandbox_host(
        platform_report=SandboxPlatformReport.available(protected_client_supported=True),
        facts=facts,
        minimal_launch=_minimal_launch(),
    )

    assert qualification.state is WindowsHostQualificationState.NOT_QUALIFIED


def test_iso_03_platform_fact_capture_uses_bounded_cim_and_systeminfo(
    tmp_path: Path,
) -> None:
    powershell = tmp_path / "powershell.exe"
    systeminfo = tmp_path / "systeminfo.exe"
    powershell.write_bytes(b"placeholder")
    systeminfo.write_bytes(b"placeholder")
    facts_payload = _supported_platform_facts().model_dump(
        mode="json", exclude={"systeminfo_identity"}
    )
    commands: list[tuple[str, ...]] = []

    def run(
        command: tuple[str, ...], timeout_seconds: int
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        assert timeout_seconds == 30
        output = (
            json.dumps(facts_payload).encode("utf-8")
            if Path(command[0]) == powershell
            else b"bounded-systeminfo-output"
        )
        return subprocess.CompletedProcess(command, 0, output, b"")

    facts = capture_windows_host_platform_facts(
        command_runner=run,
        powershell_path=powershell,
        systeminfo_path=systeminfo,
    )

    assert facts.edition_id == "Professional"
    assert facts.hypervisor_present is True
    assert facts.systeminfo_identity.startswith("windows-systeminfo:v1:sha256:")
    assert len(commands) == 2
    assert "Get-CimInstance" in commands[0][-1]


def test_iso_03_execute_returns_needs_info_when_host_fact_capture_fails(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    platform = _MinimalLaunchPlatform(evidence_root=runs_root)

    def unavailable_facts() -> WindowsHostPlatformFacts:
        raise SandboxContractError(
            reason=SandboxFailureReason.PLATFORM_UNAVAILABLE,
        )

    report = execute_iso03_boundary_qualification(
        implementation_repository_root=tmp_path / "not-used-implementation",
        reviewed_source_repository_root=tmp_path / "not-used-source",
        protected_state_repository_root=tmp_path / "not-used-protected-state",
        historical_qualification_runs_root=tmp_path / "not-used-history",
        qualified_replacement_qualification_root=(
            tmp_path / "not-used-qualified-replacement"
        ),
        failed_iso04_ticket06_acceptance_root=(
            tmp_path / "not-used-failed-iso04"
        ),
        runs_root=runs_root,
        runtime_bundle_root=tmp_path / "not-used-runtime",
        run_id="iso03facts",
        user_profile=tmp_path / "not-used-profile",
        host_temp=tmp_path / "not-used-temp",
        host_venv=tmp_path / "not-used-venv",
        host_entrypoint_path=tmp_path / "not-used-entrypoint.py",
        protected_writers_quiesced=True,
        platform=platform,
        host_fact_capture=unavailable_facts,
    )

    assert report.status is ISO03QualificationStatus.NEEDS_INFO
    assert report.actual_sandbox_launch is False
    assert report.platform_report.diagnostic_code == "host-platform-facts-unavailable"


def test_iso_03_platform_fails_closed_before_launch_when_process_inventory_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed executable placeholder")
    config = tmp_path / "sandbox.wsb"
    config.write_text("<Configuration />", encoding="utf-8")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=26100,
        feature_state_override=SandboxFeatureState.ENABLED,
    )
    assert platform.probe().state.value == "available"
    monkeypatch.setattr(platform, "_sandbox_process_topology", lambda: None)

    def forbidden_popen(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("process launch must not occur without process inventory")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)

    result = platform.launch(config, timeout_seconds=30)

    assert result.returncode == -1
    assert result.termination_confirmed is False


def test_iso_03_platform_fails_closed_before_launch_when_sandbox_is_already_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed executable placeholder")
    config = tmp_path / "sandbox.wsb"
    config.write_text("<Configuration />", encoding="utf-8")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=26100,
        feature_state_override=SandboxFeatureState.ENABLED,
    )
    assert platform.probe().state.value == "available"
    preexisting = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=900,
        ),
    )
    monkeypatch.setattr(platform, "_sandbox_process_topology", lambda: preexisting)

    def forbidden_popen(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("process launch must not occur while Sandbox is already active")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)

    result = platform.launch(config, timeout_seconds=30)

    assert result.returncode == -1
    assert result.launched_process_ids == ()
    assert result.before_processes == preexisting
    assert result.termination_confirmed is False


def test_iso_03_platform_inventories_store_sandbox_and_utility_vm_process_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasklist = b"\r\n".join(
        (
            b'"WindowsSandbox.exe","101","Console","1","1,000 K"',
            b'"WindowsSandboxClient.exe","102","Console","1","1,000 K"',
            b'"WindowsSandboxServer.exe","103","Services","0","1,000 K"',
            b'"WindowsSandboxRemoteSession.exe","104","Console","1","1,000 K"',
            b'"vmmemWindowsSandbox","105","Services","0","1,000 K"',
            b'"unrelated.exe","999","Console","1","1,000 K"',
        )
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=tasklist,
        ),
    )

    topology = WindowsSandboxPlatform._sandbox_process_topology()

    assert topology is not None
    assert {(item.image_name, item.process_id) for item in topology} == {
        ("WindowsSandbox.exe", 101),
        ("WindowsSandboxClient.exe", 102),
        ("WindowsSandboxServer.exe", 103),
        ("WindowsSandboxRemoteSession.exe", 104),
        ("VmmemWindowsSandbox", 105),
    }


@pytest.mark.parametrize(
    "tasklist",
    (
        b'"WindowsSandbox.exe","not-a-pid","Console","1","1,000 K"',
        b'"WindowsSandboxClient.exe"',
        b'"WindowsSandbox.exe","0","Console","1","1,000 K"',
    ),
)
def test_iso_03_platform_rejects_malformed_relevant_process_rows(
    monkeypatch: pytest.MonkeyPatch,
    tasklist: bytes,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=tasklist,
        ),
    )

    assert WindowsSandboxPlatform._sandbox_process_topology() is None


def test_iso_03_platform_timeout_terminates_and_confirms_new_sandbox_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed executable placeholder")
    config = tmp_path / "sandbox.wsb"
    config.write_text("<Configuration />", encoding="utf-8")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=26100,
        feature_state_override=SandboxFeatureState.ENABLED,
    )
    assert platform.probe().state.value == "available"

    class FakeProcess:
        pid = 150
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: int) -> int:
            assert timeout == 15
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -1

    launched = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=150,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=200,
        ),
    )
    process_sets = iter(((), launched, ()))
    monkeypatch.setattr(platform, "_sandbox_process_topology", lambda: next(process_sets))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    terminated: list[int] = []
    monkeypatch.setattr(platform, "_terminate_process_tree", terminated.append)

    result = platform.launch(config, timeout_seconds=0)

    assert result.timed_out is True
    assert result.termination_confirmed is True
    assert result.launched_process_ids == (150, 200)
    assert result.launched_processes == launched
    assert result.after_processes == ()
    assert terminated == [150]


def test_iso_03_platform_concurrent_launcher_is_recorded_but_never_killed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed executable placeholder")
    config = tmp_path / "sandbox.wsb"
    config.write_text("<Configuration />", encoding="utf-8")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=26100,
        feature_state_override=SandboxFeatureState.ENABLED,
    )
    assert platform.probe().state.value == "available"

    class FakeProcess:
        pid = 150
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: int) -> int:
            assert timeout == 15
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -1

    concurrent = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=150,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=200,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=151,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxClient.exe",
            process_id=201,
        ),
    )
    process_sets = iter(((), concurrent, ()))
    monkeypatch.setattr(platform, "_sandbox_process_topology", lambda: next(process_sets))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    terminated: list[int] = []
    monkeypatch.setattr(platform, "_terminate_process_tree", terminated.append)

    result = platform.launch(config, timeout_seconds=30)

    assert result.returncode == -1
    assert result.termination_confirmed is True
    assert set(result.launched_processes) == set(concurrent)
    assert result.after_processes == ()
    assert terminated == [150]
    assert 151 not in terminated


def test_iso_03_protected_state_root_is_independent_of_runner_and_source_repositories(
    tmp_path: Path,
) -> None:
    implementation = tmp_path / "implementation"
    reviewed_source = tmp_path / "reviewed-source"
    protected_state = tmp_path / "actual-protected-state"
    historical_runs = tmp_path / "historical-sandbox-runs"
    replacement_run = tmp_path / "replacement-sandbox-run"
    qualified_replacement = tmp_path / "qualified-replacement-sandbox-run"
    failed_iso04_acceptance = tmp_path / "failed-iso04-ticket06-acceptance"
    user_profile = tmp_path / "profile"
    for root in (
        implementation,
        reviewed_source,
        protected_state,
        historical_runs,
        replacement_run,
        qualified_replacement,
        failed_iso04_acceptance,
        user_profile,
    ):
        root.mkdir()

    bindings = default_protected_root_bindings(
        user_profile=user_profile,
        repository_root=protected_state,
        historical_qualification_runs_root=historical_runs,
        replacement_qualification_run_root=replacement_run,
        qualified_replacement_qualification_root=qualified_replacement,
        failed_iso04_ticket06_acceptance_root=failed_iso04_acceptance,
    )
    by_role = {binding.role: binding.path for binding in bindings}

    assert by_role[ProtectedRootRole.CHECKPOINTS] == (
        protected_state / "data" / "cache" / "checkpoints"
    )
    assert by_role[ProtectedRootRole.REPORTS_LOGS] == protected_state / "logs"
    assert by_role[ProtectedRootRole.TICKET07_EVIDENCE].is_relative_to(protected_state)
    assert by_role[ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS] == historical_runs
    assert (
        by_role[ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION]
        == replacement_run
    )
    assert (
        by_role[ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION]
        == qualified_replacement
    )
    assert (
        by_role[ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE]
        == failed_iso04_acceptance
    )
    assert not by_role[ProtectedRootRole.TICKET07_EVIDENCE].is_relative_to(
        implementation
    )
    assert not by_role[ProtectedRootRole.TICKET07_EVIDENCE].is_relative_to(
        reviewed_source
    )


def test_iso_03_historical_evidence_roots_cannot_alias(tmp_path: Path) -> None:
    shared = tmp_path / "shared-qualified-evidence"
    shared.mkdir()
    complete = _protected_manifest(tmp_path / "protected")
    aliased_roles = {
        ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
        ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
    }
    manifest = build_protected_root_manifest(
        tuple(
            ProtectedRootBinding(
                role=binding.role,
                path=shared if binding.role in aliased_roles else binding.path,
                object_kind=binding.object_kind,
            )
            for binding in complete.bindings
        )
    )

    with pytest.raises(SandboxContractError) as captured:
        validate_iso03_historical_evidence_root_coverage(manifest)

    assert (
        captured.value.reason
        is SandboxFailureReason.PROTECTED_ROOT_MANIFEST_OVERLAP
    )


def test_iso_03_programmatic_execution_requires_final_historical_evidence_roots(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="qualified_replacement_qualification_root"):
        execute_iso03_boundary_qualification(
            implementation_repository_root=tmp_path / "implementation",
            reviewed_source_repository_root=tmp_path / "reviewed-source",
            protected_state_repository_root=tmp_path / "protected-state",
            historical_qualification_runs_root=tmp_path / "historical",
            runs_root=tmp_path / "runs",
            runtime_bundle_root=tmp_path / "runtime",
            run_id="iso03-required-roots",
            user_profile=tmp_path / "profile",
            host_temp=tmp_path / "temp",
            host_venv=tmp_path / "venv",
            host_entrypoint_path=tmp_path / "entrypoint.py",
        )


class _MinimalLaunchPlatform:
    def __init__(
        self,
        *,
        evidence_root: Path,
        write_marker: bool = True,
        termination_confirmed: bool = True,
    ) -> None:
        self.evidence_root = evidence_root
        self.write_marker = write_marker
        self.termination_confirmed = termination_confirmed
        self.config_xml = ""

    def probe(self) -> SandboxPlatformReport:
        return SandboxPlatformReport.available(protected_client_supported=True)

    def launch(
        self, config_path: Path, *, timeout_seconds: int
    ) -> SandboxProcessResult:
        assert timeout_seconds == 30
        self.config_xml = config_path.read_text(encoding="utf-8")
        if self.write_marker:
            marker = {
                "contract_version": "windows-sandbox-minimal-launch-marker-v1",
                "run_id": "iso03min",
                "status": "pass",
            }
            path = (
                self.evidence_root
                / "minimal-iso03min"
                / "results-export"
                / "minimal-launch.json"
            )
            path.write_text(
                json.dumps(marker, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
        return SandboxProcessResult(
            returncode=0,
            timed_out=False,
            launched_processes=_successful_process_topology(),
            termination_confirmed=self.termination_confirmed,
        )


@pytest.mark.parametrize("write_marker", (True, False))
def test_iso_03_minimal_launch_requires_exact_guest_marker(
    tmp_path: Path,
    write_marker: bool,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    platform = _MinimalLaunchPlatform(
        evidence_root=evidence,
        write_marker=write_marker,
    )

    result = run_minimal_sandbox_launch(
        platform=platform,
        evidence_root=evidence,
        run_id="iso03min",
        timeout_seconds=30,
    )

    assert result.succeeded is write_marker
    assert "<Networking>Disable</Networking>" in platform.config_xml
    assert "<ProtectedClient>Enable</ProtectedClient>" in platform.config_xml
    assert platform.config_xml.count("<MappedFolder>") == 1


def test_iso_03_minimal_launch_rejects_unconfirmed_sandbox_termination(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    platform = _MinimalLaunchPlatform(
        evidence_root=evidence,
        termination_confirmed=False,
    )

    result = run_minimal_sandbox_launch(
        platform=platform,
        evidence_root=evidence,
        run_id="iso03min",
        timeout_seconds=30,
    )

    assert result.succeeded is False
    assert result.termination_confirmed is False


def test_iso_03_boundary_runner_input_is_closed_and_tamper_evident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_repository = Path(__file__).resolve().parents[1]
    repository = tmp_path / "repository"
    runner_sources = repository / "scripts" / "windows_sandbox"
    runner_sources.mkdir(parents=True)
    for relative_path, _target_name in QUALIFIED_RUNNER_SOURCE_FILES:
        target = repository / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_repository / relative_path).read_bytes())
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "-c", "core.autocrlf=false", "add", "."),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=ISO03 Test",
            "-c",
            "user.email=iso03@example.invalid",
            "commit",
            "-qm",
            "runner fixture",
        ),
        check=True,
    )
    committed_bootstrap = (runner_sources / "bootstrap.ps1").read_bytes()
    (runner_sources / "bootstrap.ps1").write_bytes(b"dirty worktree must not enter\n")
    redirected = tmp_path / "redirected-git-directory"
    redirected.mkdir()
    subprocess.run(("git", "-C", str(redirected), "init", "-q"), check=True)
    monkeypatch.setenv("GIT_DIR", str(redirected / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(redirected))
    staged = tmp_path / "runner"
    manifest = stage_boundary_runner_input(repository, staged)

    validate_boundary_runner_input(staged, expected=manifest)
    assert manifest.source_mode == "reviewed_index"
    assert (staged / "bootstrap.ps1").read_bytes() == committed_bootstrap
    assert tuple(item.relative_path for item in manifest.files) == (
        QUALIFIED_RUNNER_TARGET_FILES
    )
    bootstrap = (staged / "bootstrap.ps1").read_text(encoding="utf-8")
    assert '$property.Name -ne "workspace_repository"' in bootstrap

    layout = build_guest_layout()
    boundary_environment = build_environment_policy(
        run_live_environment=(
            "RUN_LIVE_CUSTOM_TESTS",
            "RUN_LIVE_DEEPSEEK_TESTS",
            "RUN_LIVE_ENVIRONMENT",
            "RUN_LIVE_PROVIDER_TESTS",
            "RUN_LIVE_TUSHARE_TESTS",
        ),
        layout=layout,
    )
    mapping_root = tmp_path / "mappings"
    guest_paths = {
        SandboxMappingRole.SOURCE: r"C:\QCF17\Input\Source",
        SandboxMappingRole.RUNNER: r"C:\QCF17\Input\Runner",
        SandboxMappingRole.CONTROL: r"C:\QCF17\Input\Control",
        SandboxMappingRole.RUNTIME_BUNDLE: r"C:\QCF17\Input\Runtime",
        SandboxMappingRole.RESULTS_EXPORT: r"C:\QCF17\Export",
    }
    mapping_bindings = []
    for index, role in enumerate(SandboxMappingRole, start=1):
        host_path = mapping_root / role.value
        host_path.mkdir(parents=True)
        mapping_bindings.append(
            SandboxMappingBinding(
                role=role,
                host_path=host_path,
                guest_path=guest_paths[role],
                read_only=role is not SandboxMappingRole.RESULTS_EXPORT,
                content_identity=(
                    f"iso03-test-{role.value}:v1:sha256:{index:064x}"
                ),
                fresh_empty=role is SandboxMappingRole.RESULTS_EXPORT,
            )
        )
    mapping_manifest = build_mapping_manifest(
        tuple(mapping_bindings),
        protected_manifest=_protected_manifest(tmp_path / "protected"),
        forbidden_host_roots=(),
    )
    configuration = render_wsb_configuration(
        mapping_manifest,
        protected_client_supported=True,
    )
    host_acceptance_module = (
        source_repository / "tradingagents" / "windows_sandbox_qualification.py"
    )
    host_acceptance_sha256 = sha256(host_acceptance_module.read_bytes()).hexdigest()
    structure = qualify_ticket06_acceptance_runner_structure(
        staged,
        expected_runner_identity=manifest.runner_input_identity,
        base_environment_variables=boundary_environment.variables,
        mapping_manifest=mapping_manifest,
        wsb_configuration=configuration,
        boundary_runner_identity=(
            "iso01-windows-sandbox-runner:v1:sha256:" + ("7" * 64)
        ),
        host_harness_manifest_identity=(
            "iso03-reviewed-host-harness:v1:sha256:" + ("8" * 64)
        ),
        host_acceptance_module_path=host_acceptance_module,
        expected_host_acceptance_module_sha256=host_acceptance_sha256,
    )
    assert structure.ticket06_executed is False
    assert structure.command_ledger_identity == TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY
    assert structure.mandatory_command_count == 27
    assert structure.full_suite_a_entry_id == "full_suite_a"
    assert structure.full_suite_b_entry_id == "full_suite_b"
    assert structure.result_schema_validated is True
    assert structure.completion_marker_write_order == "last"
    assert structure.additional_host_mapping_required is False
    assert structure.network_permission_required is False
    assert structure.mapping_manifest_identity == (
        mapping_manifest.mapping_manifest_identity
    )
    assert structure.wsb_config_identity == configuration.config_identity
    assert structure.host_acceptance_module_sha256 == host_acceptance_sha256
    assert structure.offline_review_executable_closed is True
    assert structure.acceptance_orchestrator_write_order_validated is True
    assert structure.host_acceptance_mapping_reuse_validated is True
    assert structure.host_acceptance_export_validation_path_validated is True

    (staged / "run_boundary_qualification.py").write_text(
        "raise SystemExit('tampered')\n", encoding="utf-8"
    )
    with pytest.raises(SandboxContractError):
        validate_boundary_runner_input(staged, expected=manifest)


def test_iso_03_reviewed_host_harness_rejects_dirty_executing_module_bytes(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "candidate"
    relative_paths = (
        "scripts/run_iso03_windows_sandbox_qualification.py",
        "tradingagents/offline_runtime_bundle.py",
        "tradingagents/windows_sandbox_acceptance.py",
        "tradingagents/windows_sandbox_qualification.py",
        "tradingagents/windows_sandbox_runner.py",
    )
    loaded_paths: dict[str, Path] = {}
    for relative_path in relative_paths:
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# committed {relative_path}\n", encoding="utf-8")
        loaded_paths[relative_path] = path
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "-c", "core.autocrlf=false", "add", "."),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=ISO03 Test",
            "-c",
            "user.email=iso03@example.invalid",
            "commit",
            "-qm",
            "host harness fixture",
        ),
        check=True,
    )
    entrypoint = loaded_paths[
        "scripts/run_iso03_windows_sandbox_qualification.py"
    ]

    manifest = build_reviewed_host_harness_manifest(
        repository,
        entrypoint_path=entrypoint,
        loaded_paths=loaded_paths,
    )

    assert manifest.implementation_base_commit_id
    assert manifest.candidate_tree_hash
    loaded_paths["tradingagents/windows_sandbox_qualification.py"].write_text(
        "# dirty host authority\n",
        encoding="utf-8",
    )
    with pytest.raises(SandboxContractError):
        build_reviewed_host_harness_manifest(
            repository,
            entrypoint_path=entrypoint,
            loaded_paths=loaded_paths,
        )


def test_iso_03_probe_plan_binds_python_i_and_sqlite_to_exact_protected_files(
    tmp_path: Path,
) -> None:
    manifest = _protected_manifest(tmp_path / "protected")
    targets = select_adversarial_probe_targets(manifest, run_id="iso03plan")
    by_mechanism = {item.mechanism: item for item in targets}
    by_role = {item.role: item.path for item in manifest.bindings}

    assert tuple(item.mechanism for item in targets) == tuple(ProbeMechanism)
    assert Path(
        by_mechanism[ProbeMechanism.PYTHON_ISOLATED].target_host_path
    ) == by_role[ProtectedRootRole.ISOLATED_PYTHON_WRITE_PROBE]
    assert Path(by_mechanism[ProbeMechanism.SQLITE].target_host_path) == by_role[
        ProtectedRootRole.PROVIDER_LEDGER
    ]


def test_iso_03_export_rejects_a_different_exact_path_probe_plan(
    tmp_path: Path,
) -> None:
    export = tmp_path / "export"
    result = _passing_guest_result()
    _write_export(export, result)
    expected_targets = tuple(
        AdversarialProbeTarget(
            mechanism=item.mechanism,
            target_host_path=item.target_host_path,
            target_path_identity=item.target_path_identity,
        )
        for item in result.probes
    )
    changed_path = r"C:\Users\56922\.tradingagents\different.probe"
    tampered = (
        AdversarialProbeTarget(
            mechanism=expected_targets[0].mechanism,
            target_host_path=changed_path,
            target_path_identity=host_path_identity(changed_path),
        ),
        *expected_targets[1:],
    )

    with pytest.raises(SandboxContractError):
        validate_boundary_qualification_export(
            export,
            expected=_static_bindings(),
            expected_probe_targets=tampered,
        )

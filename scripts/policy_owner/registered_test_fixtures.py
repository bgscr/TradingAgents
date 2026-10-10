"""Synthetic vectors for the human-operated registered TEST owner exercise.

These functions manufacture labelled data only. No sandbox, provider or model is
launched. Core verifier methods remain in the deployed registered owner.
The pure vector builders below are adapted from existing contract-test fixtures;
they do not import pytest, replace imports, or emulate Windows Hello.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from scripts.policy_owner.predeployment_fixtures import _build_request, canonical_model
from tradingagents.policy_qualified_contract import (
    BoundEvidence,
    EvidenceInventoryRoot,
    ExecutionEvidence,
    IndependentReviewRecord,
    PreResultClosureEvidence,
    QualifiedBaseline,
    canonical_bytes,
    content_identity,
    strict_json,
)
from tradingagents.policy_qualified_results import (
    ObservationResolution,
    acceptance_binding,
    capture_pre_result_evidence_inventory,
    reference,
)
from tradingagents.windows_sandbox_acceptance import (
    TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
    TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
    AcceptanceCommandStatus,
    Ticket06AcceptanceStaticBindings,
    Ticket06AcceptanceStructuralQualification,
    build_ticket06_acceptance_result,
    build_ticket06_command_ledger,
    build_ticket06_command_result,
    build_ticket06_completion_marker,
    build_ticket06_control_environment,
    validate_ticket06_acceptance_export,
)
from tradingagents.windows_sandbox_qualification import (
    AdversarialProbeResult,
    GuestQualificationResult,
    LocalWriteResult,
    LocalWriteTarget,
    MappingVerification,
    MinimalSandboxLaunchEvidence,
    NetworkVerification,
    ProbeMechanism,
    ProbeObservation,
    QualificationStaticBindings,
    ReviewedHostHarnessFile,
    ReviewedHostHarnessManifest,
    WindowsHostPlatformFacts,
    build_guest_qualification_result,
    host_path_identity,
    qualify_windows_sandbox_host,
)
from tradingagents.windows_sandbox_runner import (
    SandboxPlatformReport,
    SandboxProcessResult,
    SandboxProcessTopologyEntry,
)

_RECORDED_SOURCE_INPUT_IDENTITY = "windows-sandbox-reviewed-source:v1:sha256:" + "1" * 64
_RECORDED_RUNTIME_BUNDLE_IDENTITY = "windows-offline-runtime-bundle:v1:sha256:" + "2" * 64
_RECORDED_RUNNER_IDENTITY = "iso01-windows-sandbox-runner:v1:sha256:" + "3" * 64
_RECORDED_WSB_CONFIG_IDENTITY = "windows-sandbox-config:v1:sha256:" + "4" * 64


def _test_identity(namespace: str, value: object) -> str:
    material = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{namespace}:sha256:{sha256(material).hexdigest()}"


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
        mapping_manifest_identity=("windows-sandbox-mapping-manifest:v1:sha256:" + ("d" * 64)),
        guest_workspace_identity=("windows-sandbox-guest-workspace:v1:sha256:" + ("e" * 64)),
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
            host_harness_manifest_identity=("iso03-reviewed-host-harness:v1:sha256:" + ("5" * 64)),
            host_acceptance_module_sha256="6" * 64,
            boundary_runner_identity=("iso01-windows-sandbox-runner:v1:sha256:" + ("a" * 64)),
            wsb_config_identity=("windows-sandbox-config:v1:sha256:" + ("b" * 64)),
            wsb_config_policy_identity=("windows-sandbox-config-policy:v1:sha256:" + ("c" * 64)),
            mapping_manifest_identity=("windows-sandbox-mapping-manifest:v1:sha256:" + ("d" * 64)),
            command_ledger_identity=TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY,
            result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        ),
    )


def _reviewed_host_harness(
    *,
    implementation_base_commit_id: str,
    candidate_tree_hash: str,
) -> ReviewedHostHarnessManifest:
    paths = (
        "scripts/run_iso03_windows_sandbox_qualification.py",
        "tradingagents/offline_runtime_bundle.py",
        "tradingagents/windows_sandbox_acceptance.py",
        "tradingagents/windows_sandbox_qualification.py",
        "tradingagents/windows_sandbox_runner.py",
    )
    files = tuple(
        ReviewedHostHarnessFile(
            relative_path=path,
            byte_length=index + 1,
            sha256=f"{index + 1:x}" * 64,
        )
        for index, path in enumerate(paths)
    )
    payload = {
        "contract_version": "iso03-reviewed-host-harness-v1",
        "implementation_base_commit_id": implementation_base_commit_id,
        "candidate_tree_hash": candidate_tree_hash,
        "source_mode": "reviewed_index",
        "files": [item.model_dump(mode="json") for item in files],
    }
    return ReviewedHostHarnessManifest(
        **payload,
        manifest_identity=_test_identity("iso03-reviewed-host-harness:v1", payload),
    )


def _passing_guest_result() -> GuestQualificationResult:
    probes = tuple(
        AdversarialProbeResult(
            mechanism=mechanism,
            target_host_path=(rf"C:\Users\56922\.tradingagents\{mechanism.value}.probe"),
            target_path_identity=host_path_identity(
                rf"C:\Users\56922\.tradingagents\{mechanism.value}.probe"
            ),
            observation=ProbeObservation.GUEST_LOCAL_ONLY,
            contained=True,
        )
        for mechanism in ProbeMechanism
    )
    writes = tuple(LocalWriteResult(target=target, succeeded=True) for target in LocalWriteTarget)
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


def _bind_recorded_structure(
    payload: dict[str, object],
    *,
    command_ledger_identity: str,
    host_harness: ReviewedHostHarnessManifest,
) -> None:
    structure = dict(payload["acceptance_runner_structure"])
    host_acceptance_module = next(
        item
        for item in host_harness.files
        if item.relative_path == "tradingagents/windows_sandbox_qualification.py"
    )
    structure.update(
        command_ledger_identity=command_ledger_identity,
        boundary_runner_identity=_RECORDED_RUNNER_IDENTITY,
        wsb_config_identity=_RECORDED_WSB_CONFIG_IDENTITY,
        host_harness_manifest_identity=host_harness.manifest_identity,
        host_acceptance_module_sha256=host_acceptance_module.sha256,
    )
    structure["qualification_identity"] = _test_identity(
        "ticket06-acceptance-runner-structure:v2",
        {key: value for key, value in structure.items() if key != "qualification_identity"},
    )
    payload.update(
        source_input_identity=_RECORDED_SOURCE_INPUT_IDENTITY,
        runtime_bundle_identity=_RECORDED_RUNTIME_BUNDLE_IDENTITY,
        runner_identity=_RECORDED_RUNNER_IDENTITY,
        wsb_config_identity=_RECORDED_WSB_CONFIG_IDENTITY,
        acceptance_runner_structure=structure,
    )


def _safe_historical_report_bytes(
    *,
    run_id: str,
    command_ledger_identity: str,
    mismatch: str | None = None,
) -> bytes:
    platform_report = SandboxPlatformReport.available(protected_client_supported=True)
    platform_qualification = qualify_windows_sandbox_host(
        platform_report=platform_report,
        facts=_supported_platform_facts(),
        minimal_launch=_minimal_launch(),
    )
    host_harness = _reviewed_host_harness(
        implementation_base_commit_id="a" * 40,
        candidate_tree_hash="b" * 40,
    )
    static = _static_bindings().model_dump(mode="json")
    _bind_recorded_structure(
        static,
        command_ledger_identity=command_ledger_identity,
        host_harness=host_harness,
    )
    static["platform_qualification_identity"] = platform_qualification.qualification_identity
    guest = _passing_guest_result().model_dump(mode="json")
    _bind_recorded_structure(
        guest,
        command_ledger_identity=(
            TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY
            if mismatch == "ledger"
            else command_ledger_identity
        ),
        host_harness=host_harness,
    )
    guest["platform_qualification_identity"] = platform_qualification.qualification_identity
    if mismatch == "nested_runner_binding":
        mismatched_runner = "iso01-windows-sandbox-runner:v1:sha256:" + ("8" * 64)
        static["runner_identity"] = mismatched_runner
        guest["runner_identity"] = mismatched_runner
    guest_structure = dict(guest["acceptance_runner_structure"])
    if mismatch == "structure_identity":
        guest_structure["qualification_identity"] = (
            "ticket06-acceptance-runner-structure:v2:sha256:" + ("0" * 64)
        )
        guest["acceptance_runner_structure"] = guest_structure
    elif mismatch == "static_binding":
        guest["source_input_identity"] = "windows-sandbox-reviewed-source:v1:sha256:" + ("9" * 64)
    guest["result_identity"] = _test_identity(
        "windows-sandbox-boundary-qualification-result:v1",
        {key: value for key, value in guest.items() if key != "result_identity"},
    )
    if mismatch == "result_identity":
        guest["result_identity"] = "windows-sandbox-boundary-qualification-result:v1:sha256:" + (
            "0" * 64
        )

    static_structure = dict(static["acceptance_runner_structure"])
    payload: dict[str, object] = {
        "contract_version": "iso03-host-qualification-report-v1",
        "status": "fail",
        "run_id": run_id,
        "platform_report": platform_report.model_dump(mode="json"),
        "platform_qualification": platform_qualification.model_dump(mode="json"),
        "static_bindings": static,
        "runner_input_identity": static_structure["runner_input_identity"],
        "implementation_base_commit_id": (host_harness.implementation_base_commit_id),
        "candidate_tree_hash": host_harness.candidate_tree_hash,
        "host_harness": host_harness.model_dump(mode="json"),
        "protected_writer_quiescence": None,
        "actual_sandbox_launch": False,
        "sandbox_returncode": None,
        "sandbox_timed_out": False,
        "sandbox_termination_confirmed": False,
        "sandbox_process_ids": [],
        "sandbox_process_before": [],
        "sandbox_process_observed": [],
        "sandbox_process_after": [],
        "guest_result": guest,
        "guest_export_identity": None,
        "host_before_inventory_identity": static["host_before_inventory_identity"],
        "host_after_inventory_identity": None,
        "host_delta": None,
        "semantic_before": None,
        "semantic_after": None,
        "failure_reason": "safe-historical-fixture",
        "ticket06_executed": False,
        "ticket07_executed": False,
        "provider_model_io": False,
        "calendar_refresh_executed": False,
        "baseline_frozen": False,
        "qualified_v1_enabled": False,
    }
    payload["report_identity"] = _test_identity(
        "iso03-host-qualification-report:v1",
        payload,
    )
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


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


def _identity(namespace: str, fill: str) -> str:
    return f"{namespace}:sha256:{fill * 64}"


def _process_result(returncode: int) -> SandboxProcessResult:
    launched = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=101,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxServer.exe",
            process_id=102,
        ),
    )
    return SandboxProcessResult(
        returncode=returncode,
        timed_out=False,
        launched_processes=launched,
        termination_confirmed=True,
    )


def _validated_export(tmp_path: Path, *, guest_pass: bool):
    environment = build_ticket06_control_environment(
        (
            ("PYTHON_DOTENV_DISABLED", "1"),
            ("RUN_LIVE_DEEPSEEK_TESTS", "0"),
            ("RUN_LIVE_PROVIDER_TESTS", "0"),
            ("RUN_LIVE_TUSHARE_TESTS", "0"),
        )
    )
    ledger = build_ticket06_command_ledger(environment.policy_identity)
    static = Ticket06AcceptanceStaticBindings(
        source_commit_id="1" * 40,
        source_tree_hash="2" * 40,
        source_input_identity=_identity("windows-sandbox-reviewed-source:v1", "3"),
        source_manifest_identity=_identity("windows-sandbox-source-manifest:v1", "4"),
        source_archive_sha256="5" * 64,
        reconstructed_source_tree_identity=_identity(
            "windows-sandbox-reconstructed-source:v1", "6"
        ),
        runtime_bundle_identity=_identity("windows-offline-runtime-bundle:v1", "7"),
        runtime_bundle_manifest_sha256="8" * 64,
        runtime_bundle_contract_identity=_identity("windows-sandbox-runtime-bundle-input:v1", "9"),
        runner_identity=_identity("iso01-windows-sandbox-runner:v1", "a"),
        runner_input_identity=_identity("windows-sandbox-qualified-runner-input:v2", "b"),
        wsb_config_identity=_identity("windows-sandbox-config:v1", "c"),
        protected_root_manifest_identity=_identity("windows-sandbox-protected-roots:v1", "d"),
        mapping_manifest_identity=_identity("windows-sandbox-mapping-manifest:v1", "e"),
        guest_workspace_identity=_identity("windows-sandbox-guest-workspace:v1", "f"),
        environment_policy_identity=environment.policy_identity,
        control_identity=_identity("windows-sandbox-ticket06-control:v1", "a"),
        frozen_baseline_identity=_identity("ticket06-frozen-baseline:v1", "b"),
        boundary_qualification_identity=_identity("iso03-boundary-qualification:v1", "c"),
        command_ledger_identity=ledger.ledger_identity,
    )
    if guest_pass:
        command_results = tuple(
            build_ticket06_command_result(
                entry,
                status=AcceptanceCommandStatus.PASS,
                returncode=0,
                passed=1,
            )
            for entry in ledger.entries
        )
    else:
        command_results = (
            build_ticket06_command_result(
                ledger.entries[0],
                status=AcceptanceCommandStatus.FAIL,
                returncode=3,
                failed=1,
            ),
            *(
                build_ticket06_command_result(
                    entry,
                    status=AcceptanceCommandStatus.NOT_RUN,
                )
                for entry in ledger.entries[1:]
            ),
        )
    result = build_ticket06_acceptance_result(
        static=static,
        ledger=ledger,
        command_results=command_results,
        live_environment=environment.variables,
    )
    export_root = tmp_path / "results-export"
    export_root.mkdir()
    result_bytes = result.canonical_json_bytes()
    completion = build_ticket06_completion_marker(
        result=result,
        result_bytes=result_bytes,
    )
    (export_root / "result.json").write_bytes(result_bytes)
    (export_root / "completion.json").write_bytes(completion.canonical_json_bytes())
    return validate_ticket06_acceptance_export(export_root, expected=static)


def synthetic_stage_vectors(case, *, guest_pass=True):
    # These are explicitly synthetic stage vectors, never actual ISO execution.
    from tradingagents.policy_coverage_models import (
        CoverageBaseline,
        CoverageBoundEvidence,
        CoverageClosure,
        CoverageExecution,
        CoverageReview,
    )

    coverage = getattr(case, "coverage", None)
    coverage_models = {
        QualifiedBaseline: CoverageBaseline,
        BoundEvidence: CoverageBoundEvidence,
        IndependentReviewRecord: CoverageReview,
        PreResultClosureEvidence: CoverageClosure,
    }

    def seal_vector(model, **payload):
        if coverage is not None:
            model = coverage_models[model]
            payload["contract_version"] = model.model_fields[
                "contract_version"
            ].annotation.__args__[0]
            payload["coverage"] = coverage
        return model.seal(**payload)

    from tradingagents.offline_runtime_bundle import (
        BUNDLE_MANIFEST_FILENAME,
        OfflineBundleSourceBinding,
        build_offline_runtime_bundle,
    )
    from tradingagents.windows_sandbox_acceptance import (
        Ticket06AcceptanceStaticBindings,
        build_ticket06_acceptance_result,
        build_ticket06_command_ledger,
        build_ticket06_completion_marker,
        build_ticket06_control_environment,
        validate_ticket06_acceptance_export,
    )
    from tradingagents.windows_sandbox_qualification import (
        ISO03HostQualificationReport,
        ISO04SandboxProcessEvidence,
        ISO04Ticket06AcceptanceReport,
        ISO04Ticket06OperationalEvidence,
        ProtectedHostInventory,
        ProtectedRootInventory,
        ProtectedWriterQuiescenceContract,
        ProtectedWriterQuiescenceEvidence,
        compare_protected_host_inventories,
    )
    from tradingagents.windows_sandbox_runner import (
        ProtectedObjectKind,
        ProtectedRootRole,
        ReviewedSourceIdentity,
        ReviewedSourceManifest,
        build_environment_policy,
        build_guest_layout,
        build_sealed_runtime_bundle_input,
    )

    owner, plan = case.owner, case.policy.lineage_plan
    owner.evidence_root = Path(case.claim.pre_result_evidence_root)
    owner.evidence_root.mkdir()
    for stage in case.claim.stages:
        Path(stage.root).mkdir()
    for raw in owner.artifacts.values():
        (owner.evidence_root / (sha256(raw).hexdigest() + ".json")).write_bytes(raw)
    owner.store(case.claim, "lineage_identity")
    owner.store(case.approval, "approval_identity")
    source = canonical_model(
        ReviewedSourceIdentity,
        {
            "commit_id": case.candidate.commit,
            "tree_hash": case.candidate.tree,
            "git_object_format": "sha1",
            "commit_object_sha256": "1" * 64,
            "archive_sha256": "2" * 64,
            "source_manifest_identity": "windows-sandbox-source-manifest:v1:sha256:" + "3" * 64,
            "tracked_file_count": 1,
            "run_live_environment": [
                "RUN_LIVE_CUSTOM_TESTS",
                "RUN_LIVE_DEEPSEEK_TESTS",
                "RUN_LIVE_ENVIRONMENT",
                "RUN_LIVE_PROVIDER_TESTS",
                "RUN_LIVE_TUSHARE_TESTS",
            ],
        },
        "windows-sandbox-reviewed-source:v1",
        "source_input_identity",
    )
    request = _build_request(Path(case.claim.stages[0].root) / "SYNTHETIC-ISO02")
    manifest_payload = strict_json(request.reviewed_source_manifest.read_bytes())
    manifest_payload["run_live_environment"] = list(source.run_live_environment)
    manifest = canonical_model(
        ReviewedSourceManifest,
        manifest_payload,
        "windows-sandbox-source-manifest:v1",
        "source_manifest_identity",
    )
    manifest_raw = canonical_bytes(manifest.model_dump(mode="json")) + b"\n"
    request.reviewed_source_manifest.write_bytes(manifest_raw)
    archive = io.BytesIO()
    tracked_paths = {
        "pyproject.toml": request.project_metadata,
        "scripts/windows_sandbox/iso02-artifact-lock.json": request.runtime_artifact_lock,
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt": request.expected_dependency_lock,
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1": request.bootstrap_script,
        "scripts/windows_sandbox/verify_offline_bundle.py": request.verifier_script,
    }
    with tarfile.open(fileobj=archive, mode="w") as stream:
        for entry in manifest.entries:
            data = tracked_paths[entry.relative_path].read_bytes()
            info = tarfile.TarInfo(entry.relative_path)
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
    archive_raw = archive.getvalue()
    commit_raw = ("tree " + case.candidate.tree + "\n\nSYNTHETIC commit fixture\n").encode()
    source_payload = source.model_dump(mode="json")
    source_payload.update(
        source_manifest_identity=manifest.source_manifest_identity,
        tracked_file_count=len(manifest.entries),
        archive_sha256=sha256(archive_raw).hexdigest(),
        commit_object_sha256=sha256(commit_raw).hexdigest(),
    )
    source = canonical_model(
        ReviewedSourceIdentity,
        source_payload,
        "windows-sandbox-reviewed-source:v1",
        "source_input_identity",
    )
    owner.source_root = Path(case.claim.stages[0].root) / "SYNTHETIC-source-input"
    owner.source_root.mkdir()
    (owner.source_root / "source-manifest.json").write_bytes(manifest_raw)
    (owner.source_root / "reviewed-source.tar").write_bytes(archive_raw)
    (owner.source_root / "reviewed-commit.object").write_bytes(commit_raw)
    bundle_source = OfflineBundleSourceBinding(
        contract_version=source.contract_version,
        commit_id=source.commit_id,
        tree_hash=source.tree_hash,
        source_input_identity=source.source_input_identity,
        source_manifest_identity=source.source_manifest_identity,
        source_archive_sha256=source.archive_sha256,
    )
    request = replace(request, source=bundle_source)
    built = build_offline_runtime_bundle(request)
    owner.bundle_root = request.destination
    runtime = build_sealed_runtime_bundle_input(
        built.manifest,
        manifest_sha256=sha256(
            (request.destination / BUNDLE_MANIFEST_FILENAME).read_bytes()
        ).hexdigest(),
    )
    layout = build_guest_layout()
    environment = build_ticket06_control_environment(
        build_environment_policy(
            run_live_environment=source.run_live_environment,
            layout=layout,
        ).variables
    )
    ledger = build_ticket06_command_ledger(environment.policy_identity)
    assert ledger.ledger_identity == plan.ledger_identity
    source_ref = owner.store(source, "source_input_identity")
    runtime_ref = owner.store(runtime, "runtime_bundle_contract_identity")
    raw03 = strict_json(
        _safe_historical_report_bytes(
            run_id=case.claim.stages[1].run_id,
            command_ledger_identity=plan.ledger_identity,
        )
    )
    roots = []
    for role in ProtectedRootRole:
        roots.append(
            canonical_model(
                ProtectedRootInventory,
                {
                    "role": role.value,
                    "object_kind": ProtectedObjectKind.DIRECTORY.value,
                    "root_path_identity": "host-path:sha256:" + "6" * 64,
                    "file_count": 0,
                    "byte_length": 0,
                    "files": [],
                },
                "windows-sandbox-protected-root-inventory:v1",
                "inventory_identity",
            ).model_dump(mode="json")
        )
    host = canonical_model(
        ProtectedHostInventory,
        {
            "protected_root_manifest_identity": raw03["static_bindings"][
                "protected_root_manifest_identity"
            ],
            "immutable_verifier_identity": raw03["static_bindings"]["immutable_verifier_identity"],
            "roots": roots,
            "files": [],
            "file_count": 0,
            "byte_length": 0,
            "semantic_inventory": {"SYNTHETIC": "unchanged"},
            "semantic_inventory_identity": content_identity(
                "ticket06-immutable-semantic-inventory:v1", {"SYNTHETIC": "unchanged"}
            ),
        },
        "windows-sandbox-protected-host-inventory:v1",
        "inventory_identity",
    )
    host_ref = owner.store(host, "inventory_identity")
    delta = compare_protected_host_inventories(host, host)
    quiescence_contract = canonical_model(
        ProtectedWriterQuiescenceContract,
        {},
        "windows-sandbox-protected-writer-quiescence:v1",
        "contract_identity",
    )
    quiescence = canonical_model(
        ProtectedWriterQuiescenceEvidence,
        {
            "contract": quiescence_contract.model_dump(mode="json"),
            "sentinel_inventory_identity": host.inventory_identity,
            "immediate_before_inventory_identity": host.inventory_identity,
            "prelaunch_delta": delta.model_dump(mode="json"),
        },
        "windows-sandbox-protected-writer-quiescence-evidence:v1",
        "evidence_identity",
    )
    common = {
        "source_commit_id": source.commit_id,
        "source_tree_hash": source.tree_hash,
        "source_input_identity": source.source_input_identity,
        "source_manifest_identity": source.source_manifest_identity,
        "source_archive_sha256": source.archive_sha256,
        "runtime_bundle_manifest_sha256": runtime.bundle_manifest_sha256,
        "runtime_bundle_contract_identity": runtime.runtime_bundle_contract_identity,
        "runtime_bundle_identity": runtime.bundle_manifest_identity,
    }
    raw03["static_bindings"].update(common, host_before_inventory_identity=host.inventory_identity)
    from tradingagents.windows_sandbox_acceptance import Ticket06AcceptanceStructuralQualification

    structure = canonical_model(
        Ticket06AcceptanceStructuralQualification,
        {
            **raw03["static_bindings"]["acceptance_runner_structure"],
            "environment_policy_identity": environment.policy_identity,
        },
        "ticket06-acceptance-runner-structure:v2",
        "qualification_identity",
    )
    raw03["static_bindings"]["acceptance_runner_structure"] = structure.model_dump(mode="json")
    raw03["guest_result"].update(raw03["static_bindings"])
    raw03["guest_result"]["result_identity"] = content_identity(
        "windows-sandbox-boundary-qualification-result:v1",
        {k: v for k, v in raw03["guest_result"].items() if k != "result_identity"},
    )
    topology = [p.model_dump(mode="json") for p in _successful_process_topology()]
    raw03.update(
        status="pass",
        actual_sandbox_launch=True,
        sandbox_returncode=0,
        sandbox_termination_confirmed=True,
        sandbox_process_observed=topology,
        sandbox_process_ids=[p["process_id"] for p in topology],
        host_before_inventory_identity=host.inventory_identity,
        host_after_inventory_identity=host.inventory_identity,
        host_delta=delta.model_dump(mode="json"),
        protected_writer_quiescence=quiescence.model_dump(mode="json"),
        semantic_before=host.semantic_inventory,
        semantic_after=host.semantic_inventory,
        guest_export_identity="synthetic-iso03-export:v1:sha256:" + "7" * 64,
        failure_reason=None,
    )
    iso03 = canonical_model(
        ISO03HostQualificationReport, raw03, "iso03-host-qualification-report:v1", "report_identity"
    )
    iso03_ref = owner.store(iso03, "report_identity")
    binding = acceptance_binding(case.policy, case.approval, case.claim)
    baseline = seal_vector(
        QualifiedBaseline,
        contract_version="policy-qualified-qcf17-baseline-v1",
        binding=binding,
        candidate=case.candidate,
        additive_baseline_version=case.claim.additive_baseline_version,
        iso03_report=iso03_ref,
        host_inventory=host_ref,
        ledger_identity=plan.ledger_identity,
        result_schema_identity=plan.result_schema_identity,
    )
    baseline_ref = owner.store(baseline)
    base_export = _validated_export(Path(case.claim.stages[3].root), guest_pass=guest_pass)
    guest_payload = base_export.result.model_dump(mode="json")
    guest_payload["static"].update(
        common,
        frozen_baseline_identity=baseline.identity,
        boundary_qualification_identity=iso03.report_identity,
        command_ledger_identity=ledger.ledger_identity,
        environment_policy_identity=environment.policy_identity,
        runner_input_identity=structure.runner_input_identity,
        protected_root_manifest_identity=host.protected_root_manifest_identity,
    )
    guest = build_ticket06_acceptance_result(
        static=Ticket06AcceptanceStaticBindings.model_validate(guest_payload["static"]),
        ledger=ledger,
        command_results=base_export.result.command_results,
        live_environment=environment.variables,
    )
    assert guest.static.command_ledger_identity == plan.ledger_identity
    guest_ref = owner.store(guest, "result_identity")
    completion = build_ticket06_completion_marker(
        result=guest, result_bytes=guest.canonical_json_bytes() + b"\n"
    )
    completion_ref = owner.store(completion, "marker_identity")
    owner.export_root = Path(case.claim.stages[3].root) / "results-export"
    (owner.export_root / "result.json").write_bytes(owner.read_artifact(guest_ref))
    (owner.export_root / "completion.json").write_bytes(owner.read_artifact(completion_ref))
    export = validate_ticket06_acceptance_export(owner.export_root, expected=guest.static)
    process = ISO04SandboxProcessEvidence.create(
        run_id=case.claim.stages[3].run_id,
        acceptance_control_identity=guest.static.control_identity,
        process=_process_result(0),
    )
    op = ISO04Ticket06OperationalEvidence.create(
        sandbox_process=process,
        export=export,
        host_before_inventory_identity=host.inventory_identity,
        host_after_inventory_identity=host.inventory_identity,
    )
    iso04_ref = owner.store(ISO04Ticket06AcceptanceReport.create(op), "report_identity")
    raw_comparisons = (case.raw_comparison("ISO03"), case.raw_comparison("ISO04_TICKET06"))
    for obj in (case.bridge, *raw_comparisons):
        owner.store(obj)
    nodes = []

    def node(kind, predecessors, artifacts):
        case.before_node(kind)
        result = seal_vector(
            BoundEvidence,
            contract_version="policy-qualified-pre-result-evidence-v1",
            kind=kind,
            binding=binding,
            candidate=case.candidate,
            predecessors=predecessors,
            artifacts=artifacts,
            recorded_at_utc=owner.resolution.now_utc,
        )
        nodes.append(result)
        owner.stages[(case.claim.lineage_identity, kind)] = result.to_bytes()
        ref = owner.store(result)
        case.after_node(result)
        return ref

    first = node("ISO02", (), (source_ref, runtime_ref))
    second = node(
        "ISO03",
        (first,),
        (iso03_ref, host_ref, host_ref, reference(case.bridge), reference(raw_comparisons[0])),
    )
    third = node("QCF17", (second,), (baseline_ref,))
    fourth = node(
        "ISO04_TICKET06",
        (third,),
        (iso04_ref, host_ref, host_ref, guest_ref, completion_ref, reference(raw_comparisons[1])),
    )
    stage_refs = (first, second, third, fourth)
    review_refs = []
    review_nodes = []
    for axis, kind in (("STANDARDS", "REVIEW_STANDARDS"), ("SPEC", "REVIEW_SPEC")):
        review = seal_vector(
            IndependentReviewRecord,
            contract_version="policy-qualified-independent-review-v1",
            binding=binding,
            candidate=case.candidate,
            axis=axis,
            input_evidence=stage_refs,
            reviewer_principal="SYNTHETIC-independent-" + axis,
            qualification_critical=0,
            qualification_important=0,
            historical_important_unresolved=1,
        )
        review_ref = owner.store(review)
        owner.principals[review_ref.identity] = review.reviewer_principal
        review_refs.append(review_ref)
        review_nodes.append(node(kind, stage_refs, (review_ref,)))
    closing_raw = case.raw_comparison("CLOSURE_HANDOFF")
    owner.store(closing_raw)
    host_observation = "SYNTHETIC-new-post-review-host-after"
    owner.observations[host_observation] = ObservationResolution(
        host_observation,
        host_ref,
        plan.measurement_contract_pin,
        case.claim.lineage_identity,
        case.policy.lineage_plan.lineage_plan_identity,
        case.allocation.allocation_identity,
        "CLOSURE_HANDOFF",
        "after",
        "NEW_QUALIFICATION",
        owner.resolution.now_utc,
    )
    roots = (
        EvidenceInventoryRoot(role="EVIDENCE", path=case.claim.pre_result_evidence_root),
        *(
            EvidenceInventoryRoot(role=stage.stage, path=stage.root)
            for stage in case.claim.stages[:5]
        ),
    )
    inventory = capture_pre_result_evidence_inventory(
        binding=binding,
        candidate=case.candidate,
        roots=roots,
        captured_at_utc=owner.resolution.now_utc,
    )
    # Immutable inventory roots are now closed. Successor artifacts are written
    # only to the separately allocated closure/publication root, outside them.
    owner.evidence_root = None
    owner.publication_root = Path(case.claim.stages[5].root)
    inventory_ref = owner.store(inventory)
    closure = seal_vector(
        PreResultClosureEvidence,
        contract_version="policy-qualified-pre-result-closure-v1",
        binding=binding,
        candidate=case.candidate,
        reviews=tuple(review_nodes),
        post_review_host_before=host_ref,
        post_review_host_after=host_ref,
        post_review_host_observation=host_observation,
        post_review_raw=closing_raw,
        evidence_inventory=inventory_ref,
        recorded_at_utc=owner.resolution.now_utc,
    )
    closure_ref = owner.store(closure)
    node("PRE_RESULT_CLOSURE", tuple(review_nodes), (closure_ref,))
    execution_model = CoverageExecution if coverage is not None else ExecutionEvidence
    extra = (
        {"contract_version": "policy-coverage-execution-evidence-v2", "coverage": coverage}
        if coverage is not None
        else {}
    )
    return execution_model(
        **extra,
        source=source_ref,
        runtime=runtime_ref,
        iso03_report=iso03_ref,
        iso03_before=host_ref,
        iso03_after=host_ref,
        baseline=baseline_ref,
        iso04_report=iso04_ref,
        iso04_before=host_ref,
        iso04_after=host_ref,
        guest_result=guest_ref,
        guest_completion=completion_ref,
        guest_export_identity=export.export_identity,
        new_start_bridge=case.bridge,
        current_raw_comparisons=raw_comparisons,
        pre_result_closure=closure_ref,
        graph=tuple(nodes),
    )

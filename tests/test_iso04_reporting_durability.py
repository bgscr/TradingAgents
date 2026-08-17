from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradingagents.windows_sandbox_acceptance import (
    AcceptanceCommandStatus,
    Ticket06AcceptanceStaticBindings,
    build_ticket06_acceptance_result,
    build_ticket06_command_ledger,
    build_ticket06_command_result,
    build_ticket06_completion_marker,
    build_ticket06_control_environment,
    validate_ticket06_acceptance_export,
)
from tradingagents.windows_sandbox_qualification import (
    ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME,
    ISO04_TICKET06_OPERATIONAL_EVIDENCE_FILENAME,
    ISO04_TICKET06_REPORT_FILENAME,
    ISO04SandboxProcessEvidence,
    ISO04Ticket06AcceptanceReport,
    ISO04Ticket06OperationalEvidence,
    load_iso04_sandbox_process_evidence,
    load_iso04_ticket06_operational_evidence,
    persist_iso04_sandbox_process_evidence,
    persist_iso04_ticket06_operational_evidence,
    write_iso04_ticket06_report,
)
from tradingagents.windows_sandbox_runner import (
    SandboxContractError,
    SandboxProcessResult,
    SandboxProcessTopologyEntry,
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


def _validated_failure_export(tmp_path: Path):
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
        source_manifest_identity=_identity(
            "windows-sandbox-source-manifest:v1", "4"
        ),
        source_archive_sha256="5" * 64,
        reconstructed_source_tree_identity=_identity(
            "windows-sandbox-reconstructed-source:v1", "6"
        ),
        runtime_bundle_identity=_identity("windows-offline-runtime-bundle:v1", "7"),
        runtime_bundle_manifest_sha256="8" * 64,
        runtime_bundle_contract_identity=_identity(
            "windows-sandbox-runtime-bundle-input:v1", "9"
        ),
        runner_identity=_identity("iso01-windows-sandbox-runner:v1", "a"),
        runner_input_identity=_identity(
            "windows-sandbox-qualified-runner-input:v2", "b"
        ),
        wsb_config_identity=_identity("windows-sandbox-config:v1", "c"),
        protected_root_manifest_identity=_identity(
            "windows-sandbox-protected-roots:v1", "d"
        ),
        mapping_manifest_identity=_identity(
            "windows-sandbox-mapping-manifest:v1", "e"
        ),
        guest_workspace_identity=_identity(
            "windows-sandbox-guest-workspace:v1", "f"
        ),
        environment_policy_identity=environment.policy_identity,
        control_identity=_identity("windows-sandbox-ticket06-control:v1", "a"),
        frozen_baseline_identity=_identity("ticket06-frozen-baseline:v1", "b"),
        boundary_qualification_identity=_identity(
            "iso03-boundary-qualification:v1", "c"
        ),
        command_ledger_identity=ledger.ledger_identity,
    )
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
    (export_root / "completion.json").write_bytes(
        completion.canonical_json_bytes()
    )
    return validate_ticket06_acceptance_export(export_root, expected=static)


@pytest.mark.parametrize("returncode", (0, 0xC000013A))
def test_iso04_outer_sandbox_returncode_is_durable_before_synthesis(
    tmp_path: Path,
    returncode: int,
) -> None:
    audit_root = tmp_path / "host-audit"
    audit_root.mkdir()
    control_identity = _identity("windows-sandbox-ticket06-control:v1", "a")

    persisted = persist_iso04_sandbox_process_evidence(
        audit_root=audit_root,
        run_id="iso04durability",
        acceptance_control_identity=control_identity,
        process=_process_result(returncode),
    )

    evidence_path = audit_root / ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME
    assert evidence_path.is_file()
    assert persisted.outer_sandbox_process_returncode == returncode
    assert load_iso04_sandbox_process_evidence(
        evidence_path,
        expected_run_id="iso04durability",
        expected_acceptance_control_identity=control_identity,
    ) == persisted


def test_iso04_report_synthesis_failure_cannot_erase_persisted_outer_rc(
    tmp_path: Path,
) -> None:
    audit_root = tmp_path / "host-audit"
    audit_root.mkdir()
    control_identity = _identity("windows-sandbox-ticket06-control:v1", "a")
    process_path = audit_root / ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME
    persist_iso04_sandbox_process_evidence(
        audit_root=audit_root,
        run_id="iso04durability",
        acceptance_control_identity=control_identity,
        process=_process_result(17),
    )
    original_process_evidence = process_path.read_bytes()
    exported = _validated_failure_export(tmp_path)
    before_identity = _identity(
        "windows-sandbox-protected-host-inventory:v1", "1"
    )
    after_identity = _identity(
        "windows-sandbox-protected-host-inventory:v1", "2"
    )
    def failing_report(
        evidence: ISO04Ticket06OperationalEvidence,
    ) -> ISO04Ticket06AcceptanceReport:
        assert evidence.sandbox_process.outer_sandbox_process_returncode == 17
        raise RuntimeError("report synthesis failed")

    with pytest.raises(RuntimeError, match="report synthesis failed"):
        write_iso04_ticket06_report(
            audit_root=audit_root,
            expected_run_id="iso04durability",
            expected_acceptance_control_identity=control_identity,
            export=exported,
            host_before_inventory_identity=before_identity,
            host_after_inventory_identity=after_identity,
            report_builder=failing_report,
        )

    recovered = load_iso04_ticket06_operational_evidence(
        audit_root / ISO04_TICKET06_OPERATIONAL_EVIDENCE_FILENAME,
        expected_run_id="iso04durability",
        expected_acceptance_control_identity=control_identity,
    )
    assert not (audit_root / ISO04_TICKET06_REPORT_FILENAME).exists()
    assert process_path.read_bytes() == original_process_evidence
    assert recovered.sandbox_process.outer_sandbox_process_returncode == 17
    assert recovered.sandbox_process.termination_confirmed is True
    assert recovered.guest_result_identity == exported.result.result_identity
    assert (
        recovered.completion_marker_identity
        == exported.completion.marker_identity
    )
    assert recovered.guest_export_identity == exported.export_identity
    assert recovered.host_before_inventory_identity == before_identity
    assert recovered.host_after_inventory_identity == after_identity
    assert (
        recovered.command_failure_identity
        == exported.result.command_results[0].summary_identity
    )
    assert exported.result.command_results[0].returncode == 3
    assert recovered.sandbox_process.outer_sandbox_process_returncode == 17


def test_iso04_report_persistence_failure_preserves_process_evidence(
    tmp_path: Path,
) -> None:
    audit_root = tmp_path / "host-audit"
    audit_root.mkdir()
    control_identity = _identity("windows-sandbox-ticket06-control:v1", "a")
    process_path = audit_root / ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME
    persisted = persist_iso04_sandbox_process_evidence(
        audit_root=audit_root,
        run_id="iso04serialization",
        acceptance_control_identity=control_identity,
        process=_process_result(23),
    )
    original_process_evidence = process_path.read_bytes()
    report_path = audit_root / ISO04_TICKET06_REPORT_FILENAME
    historical_report = b'{"historical":"must-not-rewrite"}\n'
    report_path.write_bytes(historical_report)

    with pytest.raises(SandboxContractError):
        write_iso04_ticket06_report(
            audit_root=audit_root,
            expected_run_id="iso04serialization",
            expected_acceptance_control_identity=control_identity,
            export=None,
            host_before_inventory_identity=_identity(
                "windows-sandbox-protected-host-inventory:v1",
                "1",
            ),
            host_after_inventory_identity=_identity(
                "windows-sandbox-protected-host-inventory:v1",
                "2",
            ),
            report_builder=ISO04Ticket06AcceptanceReport.create,
        )

    assert process_path.read_bytes() == original_process_evidence
    assert report_path.read_bytes() == historical_report
    assert load_iso04_sandbox_process_evidence(
        process_path,
        expected_run_id="iso04serialization",
        expected_acceptance_control_identity=control_identity,
    ) == persisted


def test_iso04_timeout_and_process_failure_remain_distinguishable(
    tmp_path: Path,
) -> None:
    control_identity = _identity("windows-sandbox-ticket06-control:v1", "a")
    observations = (
        SandboxProcessResult(
            returncode=-1,
            timed_out=True,
            termination_confirmed=True,
        ),
        SandboxProcessResult(
            returncode=-1,
            timed_out=False,
            termination_confirmed=False,
        ),
    )
    persisted = []
    for index, observation in enumerate(observations):
        audit_root = tmp_path / f"host-audit-{index}"
        audit_root.mkdir()
        persisted.append(
            persist_iso04_sandbox_process_evidence(
                audit_root=audit_root,
                run_id=f"iso04state{index}",
                acceptance_control_identity=control_identity,
                process=observation,
            )
        )

    assert persisted[0].timed_out is True
    assert persisted[0].termination_confirmed is True
    assert persisted[1].timed_out is False
    assert persisted[1].termination_confirmed is False
    assert persisted[0].evidence_identity != persisted[1].evidence_identity


def test_iso04_process_evidence_rejects_noncanonical_or_duplicate_topology() -> None:
    control_identity = _identity("windows-sandbox-ticket06-control:v1", "a")
    duplicate = (
        SandboxProcessTopologyEntry(
            image_name="WindowsSandboxServer.exe",
            process_id=102,
        ),
        SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=102,
        ),
    )
    noncanonical = tuple(reversed(_process_result(0).launched_processes))

    for launched_processes in (duplicate, noncanonical):
        with pytest.raises(ValueError, match="process topology is invalid"):
            ISO04SandboxProcessEvidence.create(
                run_id="iso04durability",
                acceptance_control_identity=control_identity,
                process=SandboxProcessResult(
                    returncode=0,
                    timed_out=False,
                    launched_processes=launched_processes,
                ),
            )


def test_iso04_operational_evidence_has_one_noncontradictory_outer_rc(
    tmp_path: Path,
) -> None:
    audit_root = tmp_path / "host-audit"
    audit_root.mkdir()
    control_identity = _identity("windows-sandbox-ticket06-control:v1", "a")
    persist_iso04_sandbox_process_evidence(
        audit_root=audit_root,
        run_id="iso04durability",
        acceptance_control_identity=control_identity,
        process=_process_result(0),
    )
    evidence = persist_iso04_ticket06_operational_evidence(
        audit_root=audit_root,
        expected_run_id="iso04durability",
        expected_acceptance_control_identity=control_identity,
        export=None,
        host_before_inventory_identity=_identity(
            "windows-sandbox-protected-host-inventory:v1", "1"
        ),
        host_after_inventory_identity=_identity(
            "windows-sandbox-protected-host-inventory:v1", "1"
        ),
    )
    payload = evidence.model_dump(mode="json")
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    assert serialized.count('"outer_sandbox_process_returncode"') == 1
    payload["outer_sandbox_process_returncode"] = 9
    with pytest.raises(ValueError, match="extra_forbidden"):
        ISO04Ticket06OperationalEvidence.model_validate(payload)

    payload.pop("outer_sandbox_process_returncode")
    payload["sandbox_process"]["outer_sandbox_process_returncode"] = 9
    with pytest.raises(ValueError, match="process evidence identity mismatch"):
        ISO04Ticket06OperationalEvidence.model_validate(payload)

    duplicate_root = tmp_path / "duplicate-host-audit"
    duplicate_root.mkdir()
    persist_iso04_sandbox_process_evidence(
        audit_root=duplicate_root,
        run_id="iso04duplicate",
        acceptance_control_identity=control_identity,
        process=_process_result(0),
    )
    with pytest.raises(SandboxContractError):
        write_iso04_ticket06_report(
            audit_root=duplicate_root,
            expected_run_id="iso04duplicate",
            expected_acceptance_control_identity=control_identity,
            export=None,
            host_before_inventory_identity=_identity(
                "windows-sandbox-protected-host-inventory:v1", "1"
            ),
            host_after_inventory_identity=_identity(
                "windows-sandbox-protected-host-inventory:v1", "1"
            ),
            report_builder=lambda persisted: {
                "operational_evidence": persisted.model_dump(mode="json"),
                "outer_sandbox_process_returncode": 9,
            },
        )
    assert not (duplicate_root / ISO04_TICKET06_REPORT_FILENAME).exists()


def test_iso04_malformed_process_evidence_fails_closed(tmp_path: Path) -> None:
    evidence_path = tmp_path / ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME
    evidence_path.write_bytes(b'{"outer_sandbox_process_returncode":0}\n')

    with pytest.raises(SandboxContractError):
        load_iso04_sandbox_process_evidence(
            evidence_path,
            expected_run_id="iso04durability",
            expected_acceptance_control_identity=_identity(
                "windows-sandbox-ticket06-control:v1",
                "a",
            ),
        )


def test_iso04_incompatible_process_evidence_is_never_rewritten(
    tmp_path: Path,
) -> None:
    audit_root = tmp_path / "historical-host-audit"
    audit_root.mkdir()
    control_identity = _identity("windows-sandbox-ticket06-control:v1", "a")
    evidence_path = audit_root / ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME
    persist_iso04_sandbox_process_evidence(
        audit_root=audit_root,
        run_id="iso04historical",
        acceptance_control_identity=control_identity,
        process=_process_result(1),
    )
    historical_bytes = evidence_path.read_bytes()

    with pytest.raises(SandboxContractError):
        persist_iso04_sandbox_process_evidence(
            audit_root=audit_root,
            run_id="iso04historical",
            acceptance_control_identity=control_identity,
            process=_process_result(0),
        )

    assert evidence_path.read_bytes() == historical_bytes
    assert load_iso04_sandbox_process_evidence(
        evidence_path,
        expected_run_id="iso04historical",
        expected_acceptance_control_identity=control_identity,
    ).outer_sandbox_process_returncode == 1


def test_iso04_report_rejects_unknown_derived_process_claims(
    tmp_path: Path,
) -> None:
    audit_root = tmp_path / "host-audit"
    audit_root.mkdir()
    control_identity = _identity("windows-sandbox-ticket06-control:v1", "a")
    persist_iso04_sandbox_process_evidence(
        audit_root=audit_root,
        run_id="iso04closedreport",
        acceptance_control_identity=control_identity,
        process=_process_result(0),
    )

    with pytest.raises(SandboxContractError):
        write_iso04_ticket06_report(
            audit_root=audit_root,
            expected_run_id="iso04closedreport",
            expected_acceptance_control_identity=control_identity,
            export=None,
            host_before_inventory_identity=_identity(
                "windows-sandbox-protected-host-inventory:v1",
                "1",
            ),
            host_after_inventory_identity=_identity(
                "windows-sandbox-protected-host-inventory:v1",
                "2",
            ),
            report_builder=lambda evidence: {
                "operational_evidence": evidence.model_dump(mode="json"),
                "timed_out": True,
            },
        )

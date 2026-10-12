"""Versioned pure consumers; fixtures never represent native execution receipts."""

from copy import deepcopy

import pytest

from tests.fixtures.coverage_case import coverage_case
from tradingagents.policy_qualified_contract import content_identity


@pytest.fixture(scope="module")
def completed(tmp_path_factory):
    from scripts.policy_owner.registered_test_fixtures import synthetic_stage_vectors
    from tradingagents.policy_coverage_models import CoverageEnvelope

    case = coverage_case(tmp_path_factory.mktemp("coverage-consumers"))
    case.reserve()
    execution = synthetic_stage_vectors(case)
    result = case.verifier.finalize(
        policy=case.policy, approval=case.approval, claim=case.claim, execution=execution
    )
    case.owner.store(result, "result_identity")
    receipt = case.verifier.issue_receipt(result.to_bytes())
    envelope = CoverageEnvelope(
        contract_version="policy-qualified-downstream-envelope-v2",
        core=receipt.core,
        receipt=receipt,
        execution_authorized=False,
    )
    return case, result, receipt, envelope


@pytest.mark.windows
@pytest.mark.parametrize(
    "boundary",
    ["stage", "review", "closure", "execution", "history", "result", "receipt", "envelope"],
)
def test_missing_coverage_never_parses_at_any_boundary(completed, boundary):
    from tradingagents.policy_coverage_models import CoverageClosure, CoverageReview

    case, result, receipt, envelope = completed
    values = {
        "stage": result.execution.graph[0],
        "execution": result.execution,
        "history": result.history,
        "result": result,
        "receipt": receipt,
        "envelope": envelope,
    }
    values["review"] = case.verifier._model(
        next(
            r
            for r in result.execution.graph[4].artifacts
            if r.identity.startswith(CoverageReview.namespace + ":")
        ),
        CoverageReview,
    )
    values["closure"] = case.verifier._model(
        next(
            r
            for r in result.execution.graph[-1].artifacts
            if r.identity.startswith(CoverageClosure.namespace + ":")
        ),
        CoverageClosure,
    )
    value = values[boundary]
    body = value.model_dump(mode="json")
    if boundary in ("result", "receipt", "envelope"):
        holder = body["history"] if boundary == "result" else body["core"]["history"]
        holder.pop("coverage")
    else:
        body.pop("coverage")
    with pytest.raises(ValueError):
        type(value).read(body)


@pytest.mark.windows
def test_native_v3_prepared_controls_completion_and_old_schema_rejection(completed):
    from tests.test_runtime_protection_handoff import handoff_fixture, prepared_evidence
    from tradingagents.policy_coverage_models import CoverageEnvelope
    from tradingagents.policy_qualified_native import (
        ConditionalNativeCompletion,
        CoverageNativeCompletion,
        NativeOwnerBinding,
        PreparedConditionalProtectionAttempt,
        PreparedCoverageProtectionAttempt,
        build_conditional_inert_control,
        parse_prepared_conditional,
    )
    from tradingagents.runtime_protection import (
        CoverageAttemptInputs,
        derive_attempt,
        evidence_identity,
    )
    from tradingagents.runtime_protection_handoff import (
        ConditionalHandoffContract,
        CoverageHandoffContract,
        build_conditional_guard_control,
    )

    case, result, receipt, envelope = completed
    old = prepared_evidence()
    inputs = old["attempt"]["inputs"]
    inputs.update(
        source_commit=case.candidate.commit,
        source_tree=case.candidate.tree,
        qualified_source_identity=result.result_identity,
        acceptance=envelope.model_dump(mode="json"),
        production_preparation_identity="production-policy-qualified-routing-preparation:v2:"
        + "4" * 64,
        contract_version="policy-qualified-runtime-protection-attempt-v2",
    )
    attempt = derive_attempt(
        CoverageAttemptInputs.model_validate_json(__import__("json").dumps(inputs)),
        policy_verifier=case.verifier,
    )
    owner = NativeOwnerBinding(
        contract_version="policy-qualified-native-owner-binding-v1",
        instance="test-20990101-999",
        storage_binding_identity=content_identity("synthetic-storage:v1", "NO AUTHORITY"),
        native_owner_sha256="1" * 64,
        worker_manifest_sha256="2" * 64,
        native_consumer_source_identity=content_identity("synthetic-native:v1", "NO AUTHORITY"),
        execution_authorized=False,
    )
    old.update(
        contract_version="policy-qualified-runtime-protection-prepared-v3",
        attempt=attempt.model_dump(mode="json"),
        owner_binding=owner.model_dump(mode="json"),
    )
    old.pop("identity")
    old["identity"] = evidence_identity("policy-qualified-runtime-protection-prepared:v3", old)
    prepared = parse_prepared_conditional(old)
    assert isinstance(prepared, PreparedCoverageProtectionAttempt)
    with pytest.raises(ValueError):
        PreparedConditionalProtectionAttempt.model_validate(old)
    args = ["-I", "-B", attempt.paths.private_app_root + "\\inert.py"]
    control = build_conditional_inert_control(
        prepared, child_arguments=args, policy_verifier=case.verifier
    )
    assert control["contract_version"] == "policy-qualified-runtime-protection-control-v3"
    gate = handoff_fixture(old)
    gate.update(
        contract_version="policy-qualified-runtime-protection-handoff-v3",
        acceptance=envelope.model_dump(mode="json"),
        owner_binding=owner.model_dump(mode="json"),
        valid_before_exclusive=envelope.core.expires_utc,
    )
    parsed = CoverageHandoffContract.model_validate_json(__import__("json").dumps(gate))
    with pytest.raises(ValueError):
        ConditionalHandoffContract.model_validate(gate)
    guarded = build_conditional_guard_control(
        old,
        guard_arguments=args,
        handoff=parsed.model_dump(mode="json"),
        policy_verifier=case.verifier,
    )
    assert guarded["operation"] == "guard_only" and guarded["live_child_arguments"] is None
    # Synthetic schema vector only: this does NOT claim a child was actually run.
    body = {
        "contract_version": "policy-qualified-runtime-protection-completion-v3",
        "attempt_identity": attempt.attempt_identity,
        "prepared_identity": prepared.identity,
        "packet_identity": control["identity"],
        "owner_binding": owner.model_dump(mode="json"),
        "acceptance": envelope.model_dump(mode="json"),
        "state": "INCOMPLETE_RETAINED",
        "child_launches": 0,
        "exact_baseline_restored": False,
        "failure_count": 1,
        "reusable": False,
        "execution_authorized": False,
    }
    body["identity"] = evidence_identity("policy-qualified-runtime-protection-completion:v3", body)
    assert CoverageNativeCompletion.read(body).child_launches == 0
    with pytest.raises(ValueError):
        ConditionalNativeCompletion.read(body)
    corrupted = deepcopy(body)
    corrupted["acceptance"]["core"]["history"].pop("coverage")
    with pytest.raises(ValueError):
        CoverageNativeCompletion.read(corrupted)
    assert CoverageEnvelope.read(envelope) == envelope


@pytest.mark.windows
def test_real_worker_materialization_dispatch_accepts_v2_and_preserves_ancestry(
    completed, tmp_path, monkeypatch
):
    """Only owner construction is an explicit synthetic seam; dispatch is real."""
    from scripts.policy_owner.owner_worker import dispatch
    from tests.test_runtime_protection_contract import attempt_inputs
    from tests.test_runtime_protection_handoff import prepared_evidence
    from tradingagents import windows_policy_owner
    from tradingagents.policy_qualified_contract import canonical_bytes
    from tradingagents.policy_qualified_native import (
        NativeOwnerBinding,
        NativePayloadProvenance,
        seal_conditional_materialization_plan,
    )
    from tradingagents.runtime_protection import (
        ContentManifest,
        CoverageAttemptInputs,
        derive_attempt,
        evidence_identity,
        make_content_manifest,
    )

    case, result, receipt, envelope = completed
    manifests = {}
    for role, ancestor in [("runtime", result.execution.runtime), ("app", result.execution.source)]:
        manifests[role] = make_content_manifest(
            role=role,
            source_commit=case.candidate.commit,
            source_tree=case.candidate.tree,
            ancestry_identity=ancestor.identity,
            files=[
                {
                    "relative_path": (
                        "base/fixture.txt" if role == "runtime" else "tradingagents/fixture.txt"
                    ),
                    "byte_length": 3,
                    "sha256": "0" * 64,
                }
            ],
            native_images=[],
            python_version="3.13.7" if role == "runtime" else None,
        )
    template = prepared_evidence()
    environment = template["environment"]
    cwd = template["working_directory"]
    config = {
        "project_dir": r"D:\SYNTHETIC\unopened",
        "output_language": "English",
        "crypto_identity_registry_path": r"D:\SYNTHETIC\registry.json",
        "crypto_identity_registry_checksum_path": r"D:\SYNTHETIC\registry.sha256",
        "cninfo_operator_minimum_interval_seconds": 7,
    }
    inputs = attempt_inputs(
        source_commit=case.candidate.commit,
        source_tree=case.candidate.tree,
        qualified_source_identity=result.result_identity,
        production_preparation_identity="production-policy-qualified-routing-preparation:v2:"
        + "4" * 64,
        runtime_content_identity=manifests["runtime"]["identity"],
        app_content_identity=manifests["app"]["identity"],
        runtime_ancestry_identity=result.execution.runtime.identity,
        app_ancestry_identity=result.execution.source.identity,
        deployment_configuration_identity=evidence_identity(
            "runtime-protection-configuration:v1", config
        ),
        environment_identity=template["attempt"]["inputs"]["environment_identity"],
    )
    inputs.update(
        contract_version="policy-qualified-runtime-protection-attempt-v2",
        acceptance=envelope.model_dump(mode="json"),
    )
    attempt = derive_attempt(
        CoverageAttemptInputs.model_validate_json(canonical_bytes(inputs)),
        policy_verifier=case.verifier,
    )
    native_source = {"contract_version": "policy-qualified-native-consumer-source-v1", "files": []}
    native_source["identity"] = content_identity(
        "policy-qualified-native-consumer-source:v1", native_source
    )
    (tmp_path / "native-consumer-source.json").write_bytes(canonical_bytes(native_source))
    anchor = {
        "instance": "test-20990101-999",
        "storage_binding_identity": content_identity("synthetic-storage:v1", "none"),
        "native_sha256": "1" * 64,
        "worker_manifest_sha256": "2" * 64,
        "operational": False,
    }
    binding = NativeOwnerBinding(
        contract_version="policy-qualified-native-owner-binding-v1",
        instance=anchor["instance"],
        storage_binding_identity=anchor["storage_binding_identity"],
        native_owner_sha256=anchor["native_sha256"],
        worker_manifest_sha256=anchor["worker_manifest_sha256"],
        native_consumer_source_identity=native_source["identity"],
        execution_authorized=False,
    )
    plan = seal_conditional_materialization_plan(
        attempt=attempt,
        owner_binding=binding,
        policy_verifier=case.verifier,
        runtime_content=manifests["runtime"],
        app_content=manifests["app"],
        source_locations={
            "runtime/base/fixture.txt": r"D:\SYNTHETIC\runtime.txt",
            "app/tradingagents/fixture.txt": r"D:\SYNTHETIC\app.txt",
        },
        deployment_configuration=config,
        environment=environment,
        working_directory=cwd,
        python_version="3.13.7",
    )
    collection = case.owner.save("synthetic-test-collection:v1", {"execution_authorized": False})
    provenance = NativePayloadProvenance.seal(
        contract_version="policy-qualified-native-payload-provenance-v1",
        candidate=case.candidate,
        preparation_lineage_identity=attempt.inputs.preparation_lineage_identity,
        qualified_source=result.execution.source,
        qualified_runtime=result.execution.runtime,
        runtime_content=ContentManifest.model_validate_json(canonical_bytes(manifests["runtime"])),
        app_content=ContentManifest.model_validate_json(canonical_bytes(manifests["app"])),
        collection_provenance=collection,
        execution_authorized=False,
    )
    owner = case.owner
    owner.root = tmp_path
    owner.anchor = anchor
    owner.ledger = case.ledger
    owner.reconcile_authenticated_records = lambda: None

    def registration(kind, key):
        assert kind == "native-payload" and key == provenance.preparation_lineage_identity
        return provenance.model_dump(mode="json")

    owner._registration = registration
    monkeypatch.setattr(windows_policy_owner, "LocalWindowsPolicyOwner", lambda *args: owner)
    request = {
        "anchor": anchor,
        "authenticated_records": [],
        "now_utc": owner.resolution.now_utc,
        "operation": "verify-materialization",
        "payload": {
            "materialization": plan,
            "native_consumer_source_identity": native_source["identity"],
        },
    }
    response = dispatch(request)
    assert response["contract_version"] == "policy-owner-native-conditional-verification-v2"
    assert response["envelope"] == envelope.model_dump(mode="json")
    assert response["execution_authorized"] is False
    changed = deepcopy(request)
    changed["payload"]["materialization"]["app_content"]["source_commit"] = "0" * 40
    with pytest.raises(ValueError):
        dispatch(changed)

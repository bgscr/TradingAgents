"""Real verifier and disposable ledger; deliberately synthetic authority only."""

import pytest

from tests.fixtures.coverage_case import coverage_case
from tradingagents.policy_qualified_contract import AcceptancePolicy, OperatorApproval


def test_v2_claim_irreversibly_binds_coverage_baseline_with_unknown_history(tmp_path):
    case = coverage_case(tmp_path)
    claim = case.verifier.claim(
        case.policy, case.approval, case.allocation, new_start_bridge=case.bridge
    )
    assert claim.coverage == case.coverage
    assert claim.contract_version == "historical-state-qualification-lineage-v2"
    assert claim.coverage.historical_coverage == "UNKNOWN"
    with pytest.raises(ValueError):
        case.verifier.claim(
            case.policy, case.approval, case.allocation, new_start_bridge=case.bridge
        )


def test_old_schema_does_not_reinterpret_new_policy_or_approval(tmp_path):
    case = coverage_case(tmp_path)
    with pytest.raises(ValueError):
        AcceptancePolicy.read(case.policy)
    with pytest.raises(ValueError):
        OperatorApproval.read(case.approval)


def test_new_policy_rejects_old_approval_before_any_claim(tmp_path):
    case = coverage_case(tmp_path)
    with pytest.raises(ValueError):
        case.verifier.eligibility(case.policy, case.old_approval)


def test_six_stage_fixture_result_separate_receipt_and_report_keep_coverage_unknown(tmp_path):
    from scripts.policy_owner.registered_test_fixtures import synthetic_stage_vectors
    from tradingagents.policy_coverage_models import CoverageEnvelope

    case = coverage_case(tmp_path)
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
    report = case.verifier.report(envelope)
    assert result.history.historical_equal is False
    assert result.history.coverage.historical_coverage == "UNKNOWN"
    assert result.history.coverage == case.claim.coverage
    assert (
        report["acceptance"]["core"]["history"]["coverage"]["historical_object_continuity"]
        == "NOT_ESTABLISHED"
    )
    assert report["execution_authorized"] is False


def test_actual_coverage_preparation_and_attempt_preserve_complete_caveat(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    from scripts.policy_owner.registered_test_fixtures import synthetic_stage_vectors
    from tests.test_plan15_production_binding import _blueprint, _config
    from tests.test_runtime_protection_contract import attempt_inputs
    from tradingagents.policy_coverage_models import CoverageEnvelope
    from tradingagents.qualified_routing_composition import (
        CoverageProductionQualifiedRoutingSourceBinding,
        seal_production_qualified_routing_preparation,
    )
    from tradingagents.runtime_protection import (
        bind_production_preparation,
        validate_conditional_attempt,
    )

    case = coverage_case(tmp_path)
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
    source = CoverageProductionQualifiedRoutingSourceBinding(
        contract_version="production-policy-qualified-routing-source-binding-v2",
        commit_id=case.candidate.commit,
        tree_hash=case.candidate.tree,
        production_inventory_identity=case.owner.resolution.current_production_inventory_identity,
        qualified_source_identity=result.result_identity,
        acceptance=envelope,
    )
    prepared = seal_production_qualified_routing_preparation(
        blueprint=_blueprint(_config(case.root / "SYNTHETIC-runtime")),
        qualified_source=source,
        current_source=source,
        policy_verifier=case.verifier,
    )
    inputs = attempt_inputs()
    for field in (
        "source_commit",
        "source_tree",
        "qualified_source_identity",
        "production_preparation_identity",
    ):
        inputs.pop(field)
    attempt = bind_production_preparation(prepared, policy_verifier=case.verifier, **inputs)
    assert attempt.contract_version == "policy-qualified-runtime-protection-attempt-v2"
    assert attempt.inputs.acceptance == envelope
    assert validate_conditional_attempt(attempt, policy_verifier=case.verifier) == attempt

"""Publication labels must cover the taxonomy without changing admission."""

import pytest

import tradingagents.evidence as evidence_module
from tradingagents.decision_policy import (
    DecisionHorizon,
    DecisionPolicyEngine,
    StrategyRuleDefinition,
)
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    analysis_outcome_publication,
    evaluate_admission_gate,
)
from tradingagents.graph.evidence_gate import (
    create_admission_gate_node,
    route_after_admission,
)


def test_publication_labels_cover_the_complete_diagnostic_taxonomy():
    labels = evidence_module._ANALYSIS_DIAGNOSTIC_LABELS
    assert set(labels) == set(AnalysisDiagnosticCode)
    assert all(isinstance(label, str) and label.strip() for label in labels.values())


@pytest.mark.parametrize("code", list(AnalysisDiagnosticCode))
@pytest.mark.parametrize(
    "readiness", [EvidenceReadiness.INSUFFICIENT, EvidenceReadiness.CONFLICTED]
)
def test_each_diagnostic_can_publish_a_nondirectional_outcome(code, readiness):
    outcome = AnalysisOutcome(
        readiness=readiness,
        reason=AnalysisOutcomeReason.ADMISSION_BLOCKED,
        diagnostic_codes=(code,),
    )
    published = analysis_outcome_publication(outcome)
    rendered = published["analysis_outcome"]
    assert published["analysis_outcome_contract"] == outcome.model_dump(mode="json")
    assert f"`{code.value}`" in rendered
    assert "No Trading Decision was issued." in rendered
    for field in ("**Rating**", "**Price Target**", "**Action**", "**Position Size**"):
        assert field not in rendered


@pytest.mark.parametrize("required", [False, True])
def test_unavailable_source_and_missing_bank_rule_evidence_publish_blocked(required):
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="601328.SS", venue="XSHG", instrument_kind="equity", currency="CNY",
            provenance={"provider": "synthetic", "source_ref": "synthetic:identity",
                            "retrieved_at": "2026-10-16T00:00:00+00:00", "artifact_sha256": "a" * 64},
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="601328.SS", provider="synthetic",
            retrieved_at="2026-10-16T00:00:00+00:00", adjustment_basis="qfq",
            requested_date="2026-10-16", effective_trading_date="2026-10-16",
            history_rows=1212, frame_sha256="b" * 64, snapshot_id="snapshot:synthetic",
        ),
        sources=(EvidenceSource(
            source_id="synthetic:optional-gap", status=EvidenceStatus.UNAVAILABLE,
            required=required, detail="SYNTHETIC PRIVATE DETAIL MUST NOT BE PUBLISHED",
        ),),
    )
    before = evidence.model_dump(mode="json")
    preliminary = evaluate_admission_gate(evidence)
    assert preliminary.admitted is (not required)
    horizon = DecisionHorizon(count=20, unit="trading_days")
    rule = StrategyRuleDefinition(
        rule_id="synthetic.bank", version="1", applicable_instrument_kinds=("equity",),
        required_canonical_fields=("net_interest_margin", "nonperforming_loan_ratio"),
        target_rating="Buy", polarity="supports", horizon=horizon,
        predicate_id="synthetic.bank_predicate", comparator="greater_than", threshold=0,
        implementation_version="synthetic-1",
    )
    policy = DecisionPolicyEngine(rules=[rule])
    assert policy.candidate_applications(evidence, horizon=horizon) == ()
    update = create_admission_gate_node(decision_policy=policy, decision_horizon=horizon)({
        "evidence_state": before, "trade_date": "2026-10-16", "run_id": "run:" + "c" * 64,
    })
    assert update["admission_gate"]["admitted"] is False
    assert route_after_admission(update) == "blocked"
    assert "validated_decision_context" not in update
    assert "final_trade_decision" not in update
    assert evidence.model_dump(mode="json") == before
    assert evidence.source_facts == evidence.material_claims == ()
    outcome = AnalysisOutcome.model_validate(update["analysis_outcome_contract"])
    assert outcome.reason is AnalysisOutcomeReason.ADMISSION_BLOCKED
    assert "No Trading Decision was issued." in update["analysis_outcome"]
    assert "SYNTHETIC PRIVATE DETAIL" not in update["analysis_outcome"]
    if required:
        assert AnalysisDiagnosticCode.REQUIRED_EVIDENCE_UNAVAILABLE in outcome.diagnostic_codes
    else:
        assert "no_applicable_registered_strategy_rule" in update["admission_gate"]["diagnostics"]
        assert AnalysisDiagnosticCode.OPTIONAL_EVIDENCE_UNAVAILABLE in outcome.diagnostic_codes
        assert "optional" in update["analysis_outcome"].lower()

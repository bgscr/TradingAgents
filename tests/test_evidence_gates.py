import functools
import json
from hashlib import sha256
from unittest.mock import MagicMock

import pandas as pd
import pytest
from langchain_core.messages import ToolMessage

import tradingagents.evidence as evidence_module
from tradingagents.agents.managers import portfolio_manager as portfolio_manager_module
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.schemas import (
    DecisionAssertion,
    PortfolioDecisionRevision,
    PortfolioDecisionSelection,
    PortfolioRating,
)
from tradingagents.agents.utils import agent_utils
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows import (
    config as dataflow_config,
    market_snapshot as market_snapshot_module,
)
from tradingagents.dataflows.market_snapshot import (
    AuthoritativeMarketSnapshot,
    SnapshotProvider,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    AnalysisOutcome,
    ClaimValidation,
    ClaimValidationStatus,
    EvidenceReadiness,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    MaterialClaim,
    SourceArtifact,
    SourceFact,
    build_evidence_state,
    build_tool_evidence_state,
    decision_ready_material_claims,
    evaluate_admission_gate,
    merge_evidence_sources,
    merge_source_artifacts,
    render_analysis_outcome,
)
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.evidence_gate import (
    create_admission_gate_node,
    route_after_admission,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.trading_graph import TradingAgentsGraph


def _decision_assertions(*claim_ids: str) -> tuple[DecisionAssertion, ...]:
    return tuple(
        DecisionAssertion(claim_id=claim_id, fact_ids=(_test_fact_id(claim_id),))
        for claim_id in claim_ids
    )


def _test_fact_id(claim_id: str) -> str:
    return f"fact:test:{claim_id}"


def _evidence_with_supported_claims(
    *claims: MaterialClaim,
    **evidence_fields,
) -> EvidenceState:
    if "sources" not in evidence_fields:
        evidence_fields["sources"] = tuple(
            EvidenceSource(
                source_id=source_ref,
                status=EvidenceStatus.AVAILABLE,
                required=False,
            )
            for source_ref in dict.fromkeys(
                source_ref for claim in claims for source_ref in claim.source_refs
            )
        )
    enriched_claims = tuple(
        claim.model_copy(update={"fact_ids": (_test_fact_id(claim.claim_id),)})
        for claim in claims
    )
    facts = tuple(
        SourceFact(
            fact_id=_test_fact_id(claim.claim_id),
            source_ref=claim.source_refs[0],
            tool_call_id=f"test-call:{claim.claim_id}",
            tool_name="test_source",
            artifact_sha256=sha256(claim.source_quote.encode()).hexdigest(),
            raw_text=claim.source_quote,
            source_span_start=0,
            source_span_end=len(claim.source_quote),
        )
        for claim in claims
    )
    artifacts = tuple(
        SourceArtifact(
            artifact_sha256=sha256(claim.source_quote.encode()).hexdigest(),
            source_ref=claim.source_refs[0],
            tool_call_id=f"test-call:{claim.claim_id}",
            tool_name="test_source",
            raw_text=claim.source_quote,
        )
        for claim in claims
    )
    validations = tuple(
        ClaimValidation(
            claim_id=claim.claim_id,
            status=ClaimValidationStatus.SUPPORTED,
            fact_ids=(_test_fact_id(claim.claim_id),),
        )
        for claim in claims
    )
    return EvidenceState(
        **evidence_fields,
        material_claims=enriched_claims,
        source_facts=facts,
        source_artifacts=artifacts,
        claim_validations=validations,
    )


@pytest.mark.unit
def test_missing_optional_evidence_degrades_without_blocking_admission():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
            frame_sha256="f" * 64,
            snapshot_id="snapshot:test",
        ),
        material_claims=(
            MaterialClaim(
                claim_id="market.latest_close",
                analyst="market",
                statement="The effective-date close was CNY 4.31.",
                source_quote="The effective-date close was CNY 4.31.",
                source_refs=("snapshot:000725.SZ:2026-07-16",),
            ),
        ),
        sources=(
            EvidenceSource(
                source_id="reddit",
                status=EvidenceStatus.UNAVAILABLE,
                required=False,
                detail="rate limited",
            ),
        ),
    )

    result = evaluate_admission_gate(evidence, minimum_history_rows=200)

    assert result.admitted is True
    assert result.readiness is EvidenceReadiness.DEGRADED
    assert 0.0 < result.coverage < 1.0
    assert result.diagnostics == ("Optional evidence unavailable: reddit (rate limited).",)


@pytest.mark.unit
def test_later_available_observation_cannot_erase_a_source_conflict():
    conflicted = EvidenceState(
        sources=(
            EvidenceSource(
                source_id="get_indicators:NVDA:2026-01-15",
                status=EvidenceStatus.CONFLICTED,
                required=False,
                detail="source does not contain numeric claim(s): 72",
            ),
        )
    )

    merged = merge_evidence_sources(
        conflicted,
        (
            EvidenceSource(
                source_id="get_indicators:NVDA:2026-01-15",
                status=EvidenceStatus.AVAILABLE,
                required=False,
            ),
        ),
    )

    assert merged.sources == conflicted.sources


@pytest.mark.unit
def test_enforce_mode_requires_structured_output_without_plaintext_fallback():
    llm = MagicMock()
    llm.with_structured_output.side_effect = NotImplementedError("unsupported")
    manager = create_portfolio_manager(llm, evidence_gate_mode="enforce")

    result = manager(_portfolio_state(EvidenceState()))

    llm.invoke.assert_not_called()
    assert "final_trade_decision" not in result
    assert "**Analysis Outcome:** Insufficient Evidence" in result["analysis_outcome"]
    assert "Buy" not in result["analysis_outcome"]
    assert result["evidence_gate_mode"] == "enforce"


def _portfolio_state(evidence: EvidenceState) -> dict:
    return {
        "company_of_interest": "000725.SZ",
        "trade_date": "2026-07-16",
        "investment_plan": "Untrusted research plan.",
        "trader_investment_plan": "Untrusted trader proposal.",
        "past_context": "",
        "risk_debate_state": {
            "history": "Untrusted risk debate.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "Neutral",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": 1,
        },
        "evidence_state": evidence.model_dump(mode="json"),
    }


def _decision_ready_portfolio_evidence() -> EvidenceState:
    claim = MaterialClaim(
        claim_id="market.latest_close",
        analyst="market",
        statement="The effective-date close was CNY 4.31.",
        source_quote="The effective-date close was CNY 4.31.",
        source_refs=("snapshot:000725.SZ:2026-07-16",),
    )
    return _evidence_with_supported_claims(
        claim,
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ", name="BOE Technology Group"
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ", provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00", adjustment_basis="qfq",
            requested_date="2026-07-16", effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
        sources=(
            EvidenceSource(
                source_id="snapshot:000725.SZ:2026-07-16",
                status=EvidenceStatus.AVAILABLE, required=False,
            ),
        ),
    )


@pytest.mark.unit
def test_decision_ready_claim_requires_verifiable_source_artifact():
    evidence = _decision_ready_portfolio_evidence()

    without_artifact = evidence.model_copy(update={"source_artifacts": ()})
    tampered_artifact = evidence.source_artifacts[0].model_copy(
        update={"raw_text": "tampered tool output"}
    )
    with_tampered_artifact = evidence.model_copy(
        update={"source_artifacts": (tampered_artifact,)}
    )

    assert decision_ready_material_claims(without_artifact) == ()
    assert decision_ready_material_claims(with_tampered_artifact) == ()


@pytest.mark.unit
def test_source_artifact_call_identity_cannot_be_redefined():
    artifact = _decision_ready_portfolio_evidence().source_artifacts[0]
    conflicting = artifact.model_copy(
        update={
            "artifact_sha256": "f" * 64,
            "raw_text": "different content",
        }
    )

    with pytest.raises(ValueError, match="Source artifact"):
        merge_source_artifacts(
            EvidenceState(source_artifacts=(artifact,)),
            (conflicting,),
        )


@pytest.mark.unit
def test_chinese_numeric_claims_preserve_signed_decimal_tokens():
    ref = "snapshot:600895.SS:2026-07-18"
    claim = MaterialClaim(
        claim_id="market.snapshot_values",
        analyst="market",
        statement="RSI\u4e3a37.41\uff0cMACD\u4e3a-0.56\u3002",
        source_quote="| rsi | 37.41 |\n| macd | -0.56 |",
        source_refs=(ref,),
    )

    evidence = build_tool_evidence_state(
        [
            ToolMessage(
                content="| rsi | 37.41 |\n| macd | -0.56 |",
                name="get_verified_market_snapshot",
                tool_call_id="call-1",
            )
        ],
        (claim,),
        tool_call_ids_by_source={ref: ("call-1",)},
    )

    assert evidence.sources[0].status is EvidenceStatus.AVAILABLE
    assert evidence.claim_validations[0].status is ClaimValidationStatus.SUPPORTED


@pytest.mark.unit
def test_unsupported_claim_does_not_poison_supported_claim_sharing_source():
    ref = "snapshot:600895.SS:2026-07-18"
    supported = MaterialClaim(
        claim_id="market.close",
        analyst="market",
        statement="\u6536\u76d8\u4ef7\u4e3a30.27\u5143\u3002",
        source_quote="| Close | 30.27 |",
        source_refs=(ref,),
    )
    unsupported = MaterialClaim(
        claim_id="market.fabricated_rsi",
        analyst="market",
        statement="RSI\u4e3a55\u3002",
        source_quote="RSI was 55.",
        source_refs=(ref,),
    )
    evidence = build_tool_evidence_state(
        [
            ToolMessage(
                content="| Close | 30.27 |\n| rsi | 37.41 |",
                name="get_verified_market_snapshot",
                tool_call_id="call-1",
            )
        ],
        (supported, unsupported),
        tool_call_ids_by_source={ref: ("call-1",)},
    )
    validations = {
        validation.claim_id: validation.status
        for validation in evidence.claim_validations
    }

    assert evidence.sources[0].status is EvidenceStatus.AVAILABLE
    assert validations == {
        supported.claim_id: ClaimValidationStatus.SUPPORTED,
        unsupported.claim_id: ClaimValidationStatus.UNSUPPORTED,
    }

    supported_result = evidence_module.evaluate_decision_gate(
        evidence_module.DraftThesis(
            rating="Hold",
            narrative="\u6536\u76d8\u4ef7\u4e3a30.27\u5143\uff0c\u652f\u6301\u6301\u6709\u3002",
            material_claim_ids=(supported.claim_id,),
        ),
        evidence,
    )
    unsupported_result = evidence_module.evaluate_decision_gate(
        evidence_module.DraftThesis(
            rating="Hold",
            narrative="RSI\u4e3a55\uff0c\u652f\u6301\u6301\u6709\u3002",
            material_claim_ids=(unsupported.claim_id,),
        ),
        evidence,
    )

    assert supported_result.permitted is True
    assert unsupported_result.permitted is False
    assert unsupported_result.diagnostics == (
        "Draft premise market.fabricated_rsi is unsupported "
        "(source quote is absent from the cited tool result).",
    )


@pytest.mark.unit
def test_same_source_ref_binds_each_claim_to_its_exact_tool_call_artifact():
    ref = "snapshot:600895.SS:2026-07-18"
    first = "The effective-date close was CNY 30.27."
    second = "The effective-date RSI was 37.41."
    claims = (
        MaterialClaim(
            claim_id="market.close", analyst="market", statement="收盘价为30.27元。",
            source_quote=first, source_refs=(ref,),
        ),
        MaterialClaim(
            claim_id="market.rsi", analyst="market", statement="RSI为37.41。",
            source_quote=second, source_refs=(ref,),
        ),
        MaterialClaim(
            claim_id="market.fabricated", analyst="market", statement="MACD为99。",
            source_quote="The effective-date MACD was 99.", source_refs=(ref,),
        ),
    )
    evidence = build_tool_evidence_state(
        (
            ToolMessage(content=first, name="get_verified_market_snapshot", tool_call_id="call-close"),
            ToolMessage(content=second, name="get_verified_market_snapshot", tool_call_id="call-rsi"),
        ),
        claims,
        tool_call_ids_by_source={ref: ("call-close", "call-rsi")},
    )
    facts = {fact.raw_text: fact for fact in evidence.source_facts}
    validations = {item.claim_id: item for item in evidence.claim_validations}

    assert facts[first].tool_call_id == "call-close"
    assert facts[first].artifact_sha256 == sha256(first.encode("utf-8")).hexdigest()
    assert facts[second].tool_call_id == "call-rsi"
    assert facts[second].artifact_sha256 == sha256(second.encode("utf-8")).hexdigest()
    assert validations["market.close"].status is ClaimValidationStatus.SUPPORTED
    assert validations["market.rsi"].status is ClaimValidationStatus.SUPPORTED
    assert validations["market.fabricated"].status is ClaimValidationStatus.UNSUPPORTED
    assert evidence.sources[0].status is EvidenceStatus.AVAILABLE


@pytest.mark.unit
def test_localized_claim_validates_through_exact_source_quote():
    ref = "snapshot:600895.SS:2026-07-18"
    claim = MaterialClaim(
        claim_id="market.close_declined",
        analyst="market",
        statement="\u6700\u65b0\u6536\u76d8\u4ef7\u4e0b\u8dcc\u3002",
        source_quote="The latest close declined.",
        source_refs=(ref,),
    )

    evidence = build_tool_evidence_state(
        [
            ToolMessage(
                content="The latest close declined.",
                name="get_verified_market_snapshot",
                tool_call_id="call-1",
            )
        ],
        (claim,),
        tool_call_ids_by_source={ref: ("call-1",)},
    )

    assert evidence.claim_validations[0].status is ClaimValidationStatus.SUPPORTED
    assert evidence.source_facts[0].raw_text == claim.source_quote


@pytest.mark.unit
def test_missing_authoritative_market_snapshot_blocks_admission():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=None,
    )

    result = evaluate_admission_gate(evidence)

    assert result.admitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Required evidence missing: Authoritative Market Snapshot.",
    )


@pytest.mark.unit
def test_unresolved_instrument_identity_blocks_admission():
    evidence = EvidenceState(
        instrument_identity=None,
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
    )

    result = evaluate_admission_gate(evidence)

    assert result.admitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Required evidence missing: resolved instrument identity.",
    )


@pytest.mark.unit
def test_unknown_adjustment_basis_blocks_admission():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
    )

    result = evaluate_admission_gate(evidence)

    assert result.admitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Required evidence missing: known Adjustment Basis.",
    )


@pytest.mark.unit
def test_unknown_effective_trading_date_blocks_admission():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="",
            history_rows=1200,
        ),
    )

    result = evaluate_admission_gate(evidence)

    assert result.admitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Required evidence missing: Effective Trading Date.",
    )


@pytest.mark.unit
def test_insufficient_market_history_blocks_requested_calculations():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=50,
            frame_sha256="f" * 64,
            snapshot_id="snapshot:test",
        ),
    )

    result = evaluate_admission_gate(evidence, minimum_history_rows=200)

    assert result.admitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Insufficient market history: 50 rows available; 200 required.",
    )


@pytest.mark.unit
def test_unavailable_required_evidence_blocks_admission():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
            frame_sha256="f" * 64,
            snapshot_id="snapshot:test",
        ),
        sources=(
            EvidenceSource(
                source_id="issuer_filing",
                status=EvidenceStatus.UNAVAILABLE,
                required=True,
                detail="filing payload missing",
            ),
        ),
    )

    result = evaluate_admission_gate(evidence)

    assert result.admitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Required evidence unavailable: issuer_filing (filing payload missing).",
    )


@pytest.mark.unit
def test_conflicted_required_evidence_blocks_admission():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
            frame_sha256="f" * 64,
            snapshot_id="snapshot:test",
        ),
        sources=(
            EvidenceSource(
                source_id="market.latest_close",
                status=EvidenceStatus.CONFLICTED,
                required=True,
                detail="CNY 4.31 versus CNY 4.52 for the same basis and date",
            ),
        ),
    )

    result = evaluate_admission_gate(evidence)

    assert result.admitted is False
    assert result.readiness is EvidenceReadiness.CONFLICTED
    assert result.diagnostics == (
        "Required evidence conflicted: market.latest_close "
        "(CNY 4.31 versus CNY 4.52 for the same basis and date).",
    )


@pytest.mark.unit
def test_decision_gate_blocks_draft_with_missing_material_claim():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
    )
    draft = evidence_module.DraftThesis(
        rating="Buy",
        narrative="Momentum supports a Buy recommendation.",
        material_claim_ids=("market.rsi",),
    )

    result = evidence_module.evaluate_decision_gate(draft, evidence)

    assert result.permitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Draft premise missing from shared evidence: market.rsi.",
    )


@pytest.mark.unit
def test_decision_gate_blocks_directional_draft_without_material_claims():
    draft = evidence_module.DraftThesis(
        rating="Buy",
        narrative="A qualitative catalyst supports a Buy recommendation.",
        material_claim_ids=(),
    )

    result = evidence_module.evaluate_decision_gate(draft, EvidenceState())

    assert result.permitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Draft thesis cites no material claims.",
    )


@pytest.mark.unit
def test_decision_gate_blocks_draft_using_conflicted_material_source():
    claim = MaterialClaim(
        claim_id="fundamentals.revenue_growth",
        analyst="fundamentals",
        statement="Revenue grew 18% year over year.",
        source_quote="Revenue grew 18% year over year.",
        source_refs=("issuer_filing",),
    )
    evidence = _evidence_with_supported_claims(
        claim,
        sources=(
            EvidenceSource(
                source_id="issuer_filing",
                status=EvidenceStatus.CONFLICTED,
                required=False,
                detail="two filing revisions report different revenue",
            ),
        ),
    )
    draft = evidence_module.DraftThesis(
        rating="Buy",
        narrative="Revenue growth supports a Buy recommendation.",
        material_claim_ids=("fundamentals.revenue_growth",),
    )

    result = evidence_module.evaluate_decision_gate(draft, evidence)

    assert result.permitted is False
    assert result.readiness is EvidenceReadiness.CONFLICTED
    assert result.diagnostics == (
        "Draft premise fundamentals.revenue_growth uses conflicted source "
        "issuer_filing (two filing revisions report different revenue).",
    )


@pytest.mark.unit
def test_decision_gate_blocks_draft_using_unavailable_optional_source():
    claim = MaterialClaim(
        claim_id="sentiment.reddit_bullish_share",
        analyst="sentiment",
        statement="Reddit posts were 70% bullish.",
        source_quote="Reddit posts were 70% bullish.",
        source_refs=("reddit",),
    )
    evidence = _evidence_with_supported_claims(
        claim,
        sources=(
            EvidenceSource(
                source_id="reddit",
                status=EvidenceStatus.UNAVAILABLE,
                required=False,
                detail="rate limited",
            ),
        ),
    )
    draft = evidence_module.DraftThesis(
        rating="Buy",
        narrative="Reddit sentiment supports a Buy recommendation.",
        material_claim_ids=("sentiment.reddit_bullish_share",),
    )

    result = evidence_module.evaluate_decision_gate(draft, evidence)

    assert result.permitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Draft premise sentiment.reddit_bullish_share uses unavailable source "
        "reddit (rate limited).",
    )


@pytest.mark.unit
def test_decision_gate_blocks_claim_with_unregistered_source_reference():
    claim = MaterialClaim(
        claim_id="news.unverified_catalyst",
        analyst="news",
        statement="A catalyst is expected next week.",
        source_quote="A catalyst is expected next week.",
        source_refs=("invented_source",),
    )
    evidence = _evidence_with_supported_claims(
        claim,
        sources=(),
    )
    draft = evidence_module.DraftThesis(
        rating="Buy",
        narrative="The catalyst supports a Buy recommendation.",
        material_claim_ids=("news.unverified_catalyst",),
    )

    result = evidence_module.evaluate_decision_gate(draft, evidence)

    assert result.permitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Draft premise news.unverified_catalyst cites unregistered source "
        "invented_source.",
    )


@pytest.mark.unit
def test_decision_gate_rejects_snapshot_ref_for_different_instrument():
    claim = MaterialClaim(
        claim_id="market.latest_close",
        analyst="market",
        statement="The effective-date close was CNY 4.31.",
        source_quote="The effective-date close was CNY 4.31.",
        source_refs=("snapshot:OTHER:2026-07-16",),
    )
    evidence = _evidence_with_supported_claims(
        claim,
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
        sources=(),
    )
    draft = evidence_module.DraftThesis(
        rating="Buy",
        narrative="At CNY 4.31, the price evidence supports Buy.",
        material_claim_ids=("market.latest_close",),
    )

    result = evidence_module.evaluate_decision_gate(draft, evidence)

    assert result.permitted is False
    assert result.diagnostics == (
        "Draft premise market.latest_close cites unregistered source "
        "snapshot:OTHER:2026-07-16.",
    )


@pytest.mark.unit
def test_decision_gate_blocks_unsupported_numeric_claim_in_narrative():
    claim = MaterialClaim(
        claim_id="market.latest_close",
        analyst="market",
        statement="The effective-date close was CNY 4.31.",
        source_quote="The effective-date close was CNY 4.31.",
        source_refs=("snapshot:000725.SZ:2026-07-16",),
    )
    evidence = _evidence_with_supported_claims(
        claim,
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
    )
    draft = evidence_module.DraftThesis(
        rating="Buy",
        narrative="At CNY 4.31, an RSI of 72 supports a Buy recommendation.",
        material_claim_ids=("market.latest_close",),
    )

    result = evidence_module.evaluate_decision_gate(draft, evidence)

    assert result.permitted is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.diagnostics == (
        "Draft narrative contains unsupported numeric claim: 72.",
    )


@pytest.mark.unit
def test_decision_gate_permits_hold_with_degraded_coverage_and_constrained_confidence():
    claim = MaterialClaim(
        claim_id="market.latest_close",
        analyst="market",
        statement="The effective-date close was CNY 4.31.",
        source_quote="The effective-date close was CNY 4.31.",
        source_refs=("snapshot:000725.SZ:2026-07-16",),
    )
    evidence = _evidence_with_supported_claims(
        claim,
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
        sources=(
            EvidenceSource(
                source_id="snapshot:000725.SZ:2026-07-16",
                status=EvidenceStatus.AVAILABLE,
                required=False,
            ),
            EvidenceSource(
                source_id="reddit",
                status=EvidenceStatus.UNAVAILABLE,
                required=False,
                detail="rate limited",
            ),
        ),
    )
    draft = evidence_module.DraftThesis(
        rating="Hold",
        narrative="At CNY 4.31, balanced risks support a Hold recommendation.",
        material_claim_ids=("market.latest_close",),
    )

    result = evidence_module.evaluate_decision_gate(draft, evidence)

    assert result.permitted is True
    assert result.readiness is EvidenceReadiness.DEGRADED
    assert result.evidence_coverage == pytest.approx(2 / 3)
    assert result.confidence is evidence_module.DecisionConfidence.MEDIUM
    assert draft.rating == "Hold"


@pytest.mark.unit
def test_decision_gate_permits_removal_only_constrained_revision():
    claim = MaterialClaim(
        claim_id="market.latest_close",
        analyst="market",
        statement="The effective-date close was CNY 4.31.",
        source_quote="The effective-date close was CNY 4.31.",
        source_refs=("snapshot:000725.SZ:2026-07-16",),
    )
    evidence = _evidence_with_supported_claims(
        claim,
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
    )
    original = evidence_module.DraftThesis(
        rating="Buy",
        narrative="At CNY 4.31, an RSI of 72 supports a Buy recommendation.",
        material_claim_ids=("market.latest_close", "market.rsi"),
    )
    revised = evidence_module.DraftThesis(
        rating="Buy",
        narrative="At CNY 4.31, available price evidence supports a Buy recommendation.",
        material_claim_ids=("market.latest_close",),
    )

    result = evidence_module.evaluate_decision_gate(
        revised,
        evidence,
        original_draft=original,
    )

    assert result.permitted is True
    assert result.revision_applied is True


@pytest.mark.unit
def test_decision_gate_rejects_revision_that_changes_direction():
    original = evidence_module.DraftThesis(
        rating="Hold",
        narrative="The available evidence supports Hold.",
        material_claim_ids=(),
    )
    revised = evidence_module.DraftThesis(
        rating="Buy",
        narrative="The available evidence supports Buy.",
        material_claim_ids=(),
    )

    result = evidence_module.evaluate_decision_gate(
        revised,
        EvidenceState(),
        original_draft=original,
    )

    assert result.permitted is False
    assert result.revision_applied is True
    assert result.diagnostics == (
        "Constrained revision cannot change rating from Hold to Buy.",
    )


@pytest.mark.unit
def test_decision_gate_rejects_revision_that_adds_material_claim():
    snapshot_ref = "snapshot:000725.SZ:2026-07-16"
    evidence = EvidenceState(
        market_snapshot=MarketSnapshotEvidence(
            symbol="000725.SZ",
            provider="akshare",
            retrieved_at="2026-07-17T04:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-16",
            effective_trading_date="2026-07-16",
            history_rows=1200,
        ),
        material_claims=(
            MaterialClaim(
                claim_id="market.latest_close",
                analyst="market",
                statement="The effective-date close was CNY 4.31.",
                source_quote="The effective-date close was CNY 4.31.",
                source_refs=(snapshot_ref,),
            ),
            MaterialClaim(
                claim_id="market.rsi",
                analyst="market",
                statement="The effective-date RSI was 72.",
                source_quote="The effective-date RSI was 72.",
                source_refs=(snapshot_ref,),
            ),
        ),
    )
    original = evidence_module.DraftThesis(
        rating="Hold",
        narrative="At CNY 4.31, available price evidence supports Hold.",
        material_claim_ids=("market.latest_close",),
    )
    revised = evidence_module.DraftThesis(
        rating="Hold",
        narrative="At CNY 4.31 and RSI 72, available evidence supports Hold.",
        material_claim_ids=("market.latest_close", "market.rsi"),
    )

    result = evidence_module.evaluate_decision_gate(
        revised,
        evidence,
        original_draft=original,
    )

    assert result.permitted is False
    assert result.revision_applied is True
    assert result.diagnostics == (
        "Constrained revision cannot add material claim: market.rsi.",
    )


@pytest.mark.unit
def test_default_portfolio_gate_allows_only_one_failed_revision():
    first_draft = PortfolioDecisionSelection(
        rating=PortfolioRating.BUY,
        material_claim_ids=("market.rsi",),
        decision_assertions=_decision_assertions("market.rsi"),
    )
    failed_revision = PortfolioDecisionRevision(
        retained_material_claim_ids=("market.rsi",),
    )
    structured = MagicMock()
    structured.invoke.side_effect = [first_draft, failed_revision]
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    manager = create_portfolio_manager(llm)
    state = {
        "company_of_interest": "000725.SZ",
        "trade_date": "2026-07-16",
        "investment_plan": "Research plan.",
        "trader_investment_plan": "Trader plan.",
        "past_context": "",
        "risk_debate_state": {
            "history": "Risk debate.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "Neutral",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": 1,
        },
        "evidence_state": EvidenceState(
            instrument_identity=InstrumentIdentityEvidence(
                symbol="000725.SZ",
                name="BOE Technology Group",
            ),
            market_snapshot=MarketSnapshotEvidence(
                symbol="000725.SZ",
                provider="akshare",
                retrieved_at="2026-07-17T04:00:00+00:00",
                adjustment_basis="qfq",
                requested_date="2026-07-16",
                effective_trading_date="2026-07-16",
                history_rows=1200,
            ),
        ).model_dump(mode="json"),
    }

    result = manager(state)

    assert structured.invoke.call_count == 2
    assert "final_trade_decision" not in result
    assert "**Analysis Outcome:** Insufficient Evidence" in result["analysis_outcome"]
    assert "No Trading Decision was issued." in result["analysis_outcome"]
    for directional_field in (
        "**Rating**",
        "**Price Target**",
        "**Entry Price**",
        "**Stop Loss**",
        "**Position Size**",
    ):
        assert directional_field not in result["analysis_outcome"]


@pytest.mark.unit
def test_enforced_portfolio_gate_preserves_decision_markdown_with_evidence_metadata():
    decision = PortfolioDecisionSelection(
        rating=PortfolioRating.HOLD,
        material_claim_ids=("market.latest_close",),
        decision_assertions=_decision_assertions("market.latest_close"),
    )
    structured = MagicMock()
    structured.invoke.return_value = decision
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    manager = create_portfolio_manager(llm, evidence_gate_mode="enforce")
    state = {
        "company_of_interest": "000725.SZ",
        "trade_date": "2026-07-16",
        "investment_plan": "Research plan.",
        "trader_investment_plan": "Trader plan.",
        "past_context": "",
        "risk_debate_state": {
            "history": "Risk debate.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "Neutral",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": 1,
        },
        "evidence_state": _decision_ready_portfolio_evidence().model_dump(
            mode="json"
        ),
    }

    result = manager(state)

    assert structured.invoke.call_count == 1
    assert "analysis_outcome" not in result
    rendered = result["final_trade_decision"]
    assert "**Rating**: Hold" in rendered
    assert "**Executive Summary**: The Hold rating is based exclusively" in rendered
    assert "[market.latest_close] The effective-date close was CNY 4.31." in rendered
    assert "**Decision Confidence**: High" in rendered
    assert "**Evidence Coverage**: 100.0%" in rendered
    assert result["risk_debate_state"]["judge_decision"] == rendered


@pytest.mark.unit
def test_portfolio_manager_discards_generated_qualitative_premise_target_and_horizon():
    first_draft = PortfolioDecisionSelection(
        rating=PortfolioRating.BUY,
        material_claim_ids=("market.latest_close",),
        decision_assertions=_decision_assertions("market.latest_close"),
    )
    structured = MagicMock()
    structured.invoke.return_value = first_draft
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    manager = create_portfolio_manager(llm, evidence_gate_mode="enforce")
    state = {
        "company_of_interest": "000725.SZ",
        "trade_date": "2026-07-16",
        "investment_plan": "Research plan.",
        "trader_investment_plan": "Trader plan.",
        "past_context": "",
        "risk_debate_state": {
            "history": "Risk debate.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "Neutral",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": 1,
        },
        "evidence_state": _decision_ready_portfolio_evidence().model_dump(mode="json"),
    }

    result = manager(state)

    assert structured.invoke.call_count == 1
    assert "analysis_outcome" not in result
    assert "**Price Target**" not in result["final_trade_decision"]
    assert "**Time Horizon**" not in result["final_trade_decision"]
    assert "Build the position cautiously" not in result["final_trade_decision"]
    assert result["decision_gate"]["revision_applied"] is False


@pytest.mark.unit
def test_pm_revision_with_valid_removal_delta_preserves_rating_and_ledger_prose(
    monkeypatch,
):
    real_evaluate = portfolio_manager_module.evaluate_decision_gate

    def force_one_substantive_revision(draft, evidence, *, original_draft=None):
        if original_draft is None:
            return evidence_module.DecisionGateResult(
                permitted=False,
                readiness=EvidenceReadiness.INSUFFICIENT,
                evidence_coverage=0.0,
                confidence=evidence_module.DecisionConfidence.LOW,
                diagnostics=("forced substantive gate failure",),
            )
        return real_evaluate(draft, evidence, original_draft=original_draft)

    monkeypatch.setattr(
        portfolio_manager_module,
        "evaluate_decision_gate",
        force_one_substantive_revision,
    )
    structured = MagicMock()
    structured.invoke.side_effect = [
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("market.latest_close",),
            decision_assertions=_decision_assertions("market.latest_close"),
        ),
        PortfolioDecisionRevision(
            retained_material_claim_ids=("market.latest_close",),
        ),
    ]
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = create_portfolio_manager(llm, evidence_gate_mode="enforce")(
        _portfolio_state(_decision_ready_portfolio_evidence())
    )

    rendered = result["final_trade_decision"]
    assert result["draft_thesis"]["rating"] == "Buy"
    assert result["draft_thesis"]["material_claim_ids"] == ["market.latest_close"]
    assert "[market.latest_close] The effective-date close was CNY 4.31." in rendered
    assert "**Price Target**" not in rendered
    assert "**Time Horizon**" not in rendered


@pytest.mark.unit
def test_pm_revision_with_extra_fields_blocks_fail_closed(monkeypatch):
    real_evaluate = portfolio_manager_module.evaluate_decision_gate

    def force_one_substantive_revision(draft, evidence, *, original_draft=None):
        if original_draft is None:
            return evidence_module.DecisionGateResult(
                permitted=False,
                readiness=EvidenceReadiness.INSUFFICIENT,
                evidence_coverage=0.0,
                confidence=evidence_module.DecisionConfidence.LOW,
                diagnostics=("forced substantive gate failure",),
            )
        return real_evaluate(draft, evidence, original_draft=original_draft)

    monkeypatch.setattr(
        portfolio_manager_module,
        "evaluate_decision_gate",
        force_one_substantive_revision,
    )
    structured = MagicMock()
    structured.invoke.side_effect = [
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("market.latest_close",),
            decision_assertions=_decision_assertions("market.latest_close"),
        ),
        {
            "retained_material_claim_ids": ("market.latest_close",),
            "rating": "Sell",
            "material_claim_ids": ("market.fabricated",),
            "investment_thesis": "Fabricated qualitative premise.",
            "price_target": 0.01,
            "time_horizon": "tomorrow",
        },
    ]
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = create_portfolio_manager(llm, evidence_gate_mode="enforce")(
        _portfolio_state(_decision_ready_portfolio_evidence())
    )

    assert "final_trade_decision" not in result
    assert "**Analysis Outcome:** Insufficient Evidence" in result["analysis_outcome"]
    assert "Portfolio Manager removal-only revision was invalid" in result["analysis_outcome"]
    assert "Fabricated qualitative premise" not in result["analysis_outcome"]


@pytest.mark.unit
def test_pm_retries_out_of_ledger_selection_against_the_same_immutable_ledger():
    structured = MagicMock()
    structured.invoke.side_effect = [
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("C004",),
            decision_assertions=_decision_assertions("C004"),
        ),
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("market.latest_close",),
            decision_assertions=_decision_assertions("market.latest_close"),
        ),
    ]
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = create_portfolio_manager(llm, evidence_gate_mode="enforce")(
        _portfolio_state(_decision_ready_portfolio_evidence())
    )

    assert structured.invoke.call_count == 2
    assert result["pm_original_selection"]["material_claim_ids"] == ["C004"]
    assert result["pm_selection_retry"]["material_claim_ids"] == [
        "market.latest_close"
    ]
    assert result["draft_thesis"]["material_claim_ids"] == ["market.latest_close"]
    assert "final_trade_decision" in result
    retry_prompt = structured.invoke.call_args_list[1].args[0]
    assert "C004" not in retry_prompt
    assert "market.latest_close" in retry_prompt


@pytest.mark.unit
def test_pm_retries_selection_with_unbound_fact_ids():
    structured = MagicMock()
    structured.invoke.side_effect = [
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("market.latest_close",),
            decision_assertions=(
                DecisionAssertion(
                    claim_id="market.latest_close",
                    fact_ids=("fact:wrong",),
                ),
            ),
        ),
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("market.latest_close",),
            decision_assertions=_decision_assertions("market.latest_close"),
        ),
    ]
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = create_portfolio_manager(llm, evidence_gate_mode="enforce")(
        _portfolio_state(_decision_ready_portfolio_evidence())
    )

    assert structured.invoke.call_count == 2
    assert result["pm_selection_retry"]["decision_assertions"][0]["fact_ids"] == [
        _test_fact_id("market.latest_close")
    ]
    assert "final_trade_decision" in result


@pytest.mark.unit
def test_pm_retries_out_of_ledger_decision_assertion_against_same_ledger():
    structured = MagicMock()
    structured.invoke.side_effect = [
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("market.latest_close",),
            decision_assertions=_decision_assertions("C004"),
        ),
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("market.latest_close",),
            decision_assertions=_decision_assertions("market.latest_close"),
        ),
    ]
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = create_portfolio_manager(llm, evidence_gate_mode="enforce")(
        _portfolio_state(_decision_ready_portfolio_evidence())
    )

    assert structured.invoke.call_count == 2
    assert result["pm_original_selection"]["decision_assertions"][0]["claim_id"] == "C004"
    assert result["pm_selection_retry"]["decision_assertions"][0]["claim_id"] == (
        "market.latest_close"
    )
    assert "final_trade_decision" in result


@pytest.mark.unit
def test_pm_retries_revision_that_adds_an_id_before_building_a_draft(monkeypatch):
    real_evaluate = portfolio_manager_module.evaluate_decision_gate

    def force_one_substantive_revision(draft, evidence, *, original_draft=None):
        if original_draft is None:
            return evidence_module.DecisionGateResult(
                permitted=False,
                readiness=EvidenceReadiness.INSUFFICIENT,
                evidence_coverage=0.0,
                confidence=evidence_module.DecisionConfidence.LOW,
                diagnostics=("forced substantive gate failure",),
            )
        return real_evaluate(draft, evidence, original_draft=original_draft)

    monkeypatch.setattr(
        portfolio_manager_module,
        "evaluate_decision_gate",
        force_one_substantive_revision,
    )
    structured = MagicMock()
    structured.invoke.side_effect = [
        PortfolioDecisionSelection(
            rating=PortfolioRating.BUY,
            material_claim_ids=("market.latest_close",),
            decision_assertions=_decision_assertions("market.latest_close"),
        ),
        PortfolioDecisionRevision(retained_material_claim_ids=("C004",)),
        PortfolioDecisionRevision(
            retained_material_claim_ids=("market.latest_close",)
        ),
    ]
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = create_portfolio_manager(llm, evidence_gate_mode="enforce")(
        _portfolio_state(_decision_ready_portfolio_evidence())
    )

    assert structured.invoke.call_count == 3
    assert result["pm_revision"]["retained_material_claim_ids"] == ["C004"]
    assert result["pm_revision_retry"]["retained_material_claim_ids"] == [
        "market.latest_close"
    ]
    assert result["draft_thesis"]["material_claim_ids"] == ["market.latest_close"]
    assert "final_trade_decision" in result
    retry_prompt = structured.invoke.call_args_list[2].args[0]
    assert "C004" not in retry_prompt
    assert "market.latest_close" in retry_prompt


@pytest.mark.unit
def test_explicit_shadow_override_labels_directional_output_unenforced():
    llm = MagicMock()
    llm.with_structured_output.side_effect = NotImplementedError(
        "provider has no structured output"
    )
    llm.invoke.return_value = MagicMock(
        content="**Rating**: Buy\n\nLegacy free-text decision."
    )
    manager = create_portfolio_manager(llm, evidence_gate_mode="shadow")
    state = {
        "company_of_interest": "000725.SZ",
        "trade_date": "2026-07-16",
        "investment_plan": "Research plan.",
        "trader_investment_plan": "Trader plan.",
        "past_context": "",
        "risk_debate_state": {
            "history": "Risk debate.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "Neutral",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": 1,
        },
        "evidence_state": EvidenceState().model_dump(mode="json"),
    }

    result = manager(state)

    rendered = result["final_trade_decision"]
    assert rendered.startswith("> **Evidence Gate:** UNENFORCED (shadow mode)")
    assert "**Rating**: Buy" in rendered
    assert result["risk_debate_state"]["judge_decision"] == rendered


@pytest.mark.unit
def test_insufficient_outcome_renders_diagnostics_without_directional_fields():
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        summary="A trustworthy market snapshot was not available.",
        diagnostics=(
            "Required evidence missing: Authoritative Market Snapshot.",
        ),
        evidence_coverage=0.5,
    )

    rendered = render_analysis_outcome(outcome)

    assert "**Analysis Outcome:** Insufficient Evidence" in rendered
    assert "**Evidence Coverage:** 50.0%" in rendered
    assert "Required evidence missing: Authoritative Market Snapshot." in rendered
    assert "No Trading Decision was issued." in rendered
    for forbidden_field in (
        "**Rating**",
        "**Price Target**",
        "**Entry Price**",
        "**Stop Loss**",
        "**Position Size**",
        "**Action**",
    ):
        assert forbidden_field not in rendered
    assert "Hold" not in rendered


@pytest.mark.unit
def test_initial_graph_state_preserves_typed_material_claims():
    evidence = EvidenceState(
        material_claims=(
            MaterialClaim(
                claim_id="market.latest_close",
                analyst="market",
                statement="The effective-date close was CNY 4.31.",
                source_quote="The effective-date close was CNY 4.31.",
                source_refs=("snapshot:000725.SZ:2026-07-16",),
            ),
        ),
    )

    state = Propagator().create_initial_state(
        "000725.SZ",
        "2026-07-16",
        evidence_state=evidence,
    )

    restored = EvidenceState.model_validate(state["evidence_state"])
    assert restored.material_claims == evidence.material_claims


@pytest.mark.unit
def test_admission_node_emits_non_directional_outcome_when_blocked():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=None,
    )

    update = create_admission_gate_node()({
        "evidence_state": evidence.model_dump(mode="json"),
    })

    assert update["admission_gate"]["admitted"] is False
    assert update["admission_gate"]["readiness"] == "insufficient"
    assert "**Analysis Outcome:** Insufficient Evidence" in update["analysis_outcome"]
    assert "No Trading Decision was issued." in update["analysis_outcome"]


@pytest.mark.unit
def test_blocked_admission_routes_away_from_research_debate():
    route = route_after_admission({
        "admission_gate": {
            "admitted": False,
            "readiness": "insufficient",
        },
    })

    assert route == "blocked"


@pytest.mark.unit
def test_acquired_identity_and_snapshot_become_shared_evidence():
    snapshot = AuthoritativeMarketSnapshot(
        symbol="000725.SZ",
        frame=pd.DataFrame({
            "Date": pd.to_datetime(["2026-07-15", "2026-07-16"]),
            "Open": [4.20, 4.25],
            "High": [4.35, 4.36],
            "Low": [4.18, 4.22],
            "Close": [4.28, 4.31],
            "Volume": [1_000_000, 1_200_000],
        }),
        provider="akshare",
        retrieved_at="2026-07-17T04:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-07-16",
        effective_trading_date="2026-07-16",
    )

    evidence = build_evidence_state(
        symbol="000725.SZ",
        identity={"company_name": "BOE Technology Group"},
        snapshot=snapshot,
    )

    assert evidence.instrument_identity == InstrumentIdentityEvidence(
        symbol="000725.SZ",
        name="BOE Technology Group",
    )
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.symbol == "000725.SZ"
    assert evidence.market_snapshot.history_rows == 2
    assert evidence.market_snapshot.frame_sha256
    assert evidence.market_snapshot.snapshot_id.startswith("snapshot:")


@pytest.mark.unit
def test_run_evidence_is_acquired_from_identity_and_market_boundaries(monkeypatch):
    frame = pd.DataFrame({
        "Date": pd.to_datetime(["2026-07-15", "2026-07-16"]),
        "Open": [4.20, 4.25],
        "High": [4.35, 4.36],
        "Low": [4.18, 4.22],
        "Close": [4.28, 4.31],
        "Volume": [1_000_000, 1_200_000],
    })

    class FakeTicker:
        def __init__(self, symbol):
            assert symbol == "000725.SZ"

        @property
        def info(self):
            return {"longName": "BOE Technology Group"}

    monkeypatch.setattr(agent_utils.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(
        dataflow_config,
        "_config",
        {"market_data_vendors": {"cn_a": {"core_stock_apis": "akshare"}}},
    )
    monkeypatch.setitem(
        market_snapshot_module.SNAPSHOT_PROVIDERS,
        "akshare",
        SnapshotProvider(lambda *args: frame, "qfq"),
    )
    agent_utils.resolve_instrument_identity.cache_clear()

    evidence = TradingAgentsGraph.resolve_evidence_state(
        "000725.SZ",
        "2026-07-16",
    )

    assert evidence.instrument_identity.name == "BOE Technology Group"
    assert evidence.market_snapshot.provider == "akshare"
    assert evidence.market_snapshot.history_rows == 2


@pytest.mark.unit
def test_enforced_admission_gate_precedes_research_debate():
    llm = MagicMock()

    def tool_node(state):
        return state

    setup = GraphSetup(
        llm,
        llm,
        dict.fromkeys(("market", "social", "news", "fundamentals"), tool_node),
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        evidence_gate_mode="enforce",
    )

    workflow = setup.setup_graph(["market"])

    assert "Evidence Admission" in workflow.nodes
    assert ("Msg Clear Market", "Evidence Admission") in workflow.edges
    admission_branch = workflow.branches["Evidence Admission"]["route_after_admission"]
    assert admission_branch.ends == {
        "admitted": "Bull Researcher",
        "blocked": "__end__",
    }


@pytest.mark.unit
def test_graph_setup_rejects_unknown_evidence_gate_mode():
    llm = MagicMock()

    with pytest.raises(
        ValueError,
        match="evidence_gate_mode must be one of: enforce, shadow",
    ):
        GraphSetup(
            llm,
            llm,
            {},
            ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
            evidence_gate_mode="enfore",
        )


@pytest.mark.unit
def test_shadow_admission_records_block_without_terminal_outcome():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000725.SZ",
            name="BOE Technology Group",
        ),
        market_snapshot=None,
    )

    update = create_admission_gate_node(emit_blocked_outcome=False)({
        "evidence_state": evidence.model_dump(mode="json"),
    })

    assert update["admission_gate"]["admitted"] is False
    assert "analysis_outcome" not in update


@pytest.mark.unit
def test_evidence_gate_defaults_to_enforcement_after_replay_review():
    assert DEFAULT_CONFIG["evidence_gate_mode"] == "enforce"


@pytest.mark.unit
def test_programmatic_enforced_block_returns_outcome_without_decision(tmp_path):
    outcome = (
        "**Analysis Outcome:** Insufficient Evidence\n\n"
        "No Trading Decision was issued."
    )
    final_state = Propagator().create_initial_state("NVDA", "2026-01-10")
    final_state["analysis_outcome"] = outcome

    graph = MagicMock()
    graph.memory_log = TradingMemoryLog(
        {"memory_log_path": str(tmp_path / "trading_memory.md")}
    )
    graph.log_states_dict = {}
    graph.debug = False
    graph.config = {
        "checkpoint_enabled": False,
        "results_dir": str(tmp_path),
    }
    graph._checkpointer_ctx = None
    graph.graph.invoke.return_value = final_state
    graph.propagator.create_initial_state.return_value = final_state
    graph.propagator.get_graph_args.return_value = {}
    graph.resolve_instrument_context.return_value = "resolved identity"
    graph.resolve_evidence_state.return_value = EvidenceState()
    graph._run_signature.return_value = "test-graph-signature"
    graph._resolve_pending_entries = functools.partial(
        TradingAgentsGraph._resolve_pending_entries,
        graph,
    )
    graph._log_state = functools.partial(TradingAgentsGraph._log_state, graph)
    graph._run_graph = functools.partial(TradingAgentsGraph._run_graph, graph)

    returned_state, signal = TradingAgentsGraph.propagate(
        graph,
        "NVDA",
        "2026-01-10",
    )

    assert returned_state["analysis_outcome"] == outcome
    assert signal is None
    assert graph.memory_log.load_entries() == []
    graph.process_signal.assert_not_called()
    log_path = (
        tmp_path
        / "NVDA"
        / "TradingAgentsStrategy_logs"
        / "full_states_log_2026-01-10.json"
    )
    logged_state = json.loads(log_path.read_text(encoding="utf-8"))
    assert logged_state["analysis_outcome"] == outcome
    assert "final_trade_decision" not in logged_state

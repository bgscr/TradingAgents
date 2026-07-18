import functools
import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

import tradingagents.evidence as evidence_module
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating
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
    EvidenceReadiness,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    MaterialClaim,
    build_evidence_state,
    evaluate_admission_gate,
    merge_evidence_sources,
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
        ),
        material_claims=(
            MaterialClaim(
                claim_id="market.latest_close",
                analyst="market",
                statement="The effective-date close was CNY 4.31.",
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
    evidence = EvidenceState(
        material_claims=(
            MaterialClaim(
                claim_id="fundamentals.revenue_growth",
                analyst="fundamentals",
                statement="Revenue grew 18% year over year.",
                source_refs=("issuer_filing",),
            ),
        ),
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
    evidence = EvidenceState(
        material_claims=(
            MaterialClaim(
                claim_id="sentiment.reddit_bullish_share",
                analyst="sentiment",
                statement="Reddit posts were 70% bullish.",
                source_refs=("reddit",),
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
    evidence = EvidenceState(
        material_claims=(
            MaterialClaim(
                claim_id="news.unverified_catalyst",
                analyst="news",
                statement="A catalyst is expected next week.",
                source_refs=("invented_source",),
            ),
        ),
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
                source_refs=("snapshot:OTHER:2026-07-16",),
            ),
        ),
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
                source_refs=("snapshot:000725.SZ:2026-07-16",),
            ),
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
        material_claims=(
            MaterialClaim(
                claim_id="market.latest_close",
                analyst="market",
                statement="The effective-date close was CNY 4.31.",
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
                source_refs=("snapshot:000725.SZ:2026-07-16",),
            ),
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
                source_refs=(snapshot_ref,),
            ),
            MaterialClaim(
                claim_id="market.rsi",
                analyst="market",
                statement="The effective-date RSI was 72.",
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
    first_draft = PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="Build a position cautiously.",
        investment_thesis="An RSI of 72 supports buying.",
        price_target=5.20,
        material_claim_ids=("market.rsi",),
    )
    failed_revision = PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="Build a smaller position.",
        investment_thesis="The RSI still supports buying.",
        material_claim_ids=("market.rsi",),
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
    decision = PortfolioDecision(
        rating=PortfolioRating.HOLD,
        executive_summary="Maintain the current position.",
        investment_thesis=(
            "At CNY 4.31, balanced risks support maintaining the position."
        ),
        material_claim_ids=("market.latest_close",),
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
            material_claims=(
                MaterialClaim(
                    claim_id="market.latest_close",
                    analyst="market",
                    statement="The effective-date close was CNY 4.31.",
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
        ).model_dump(mode="json"),
    }

    result = manager(state)

    assert structured.invoke.call_count == 1
    assert "analysis_outcome" not in result
    rendered = result["final_trade_decision"]
    assert "**Rating**: Hold" in rendered
    assert "**Executive Summary**: Maintain the current position." in rendered
    assert "**Investment Thesis**: At CNY 4.31" in rendered
    assert "**Decision Confidence**: Medium" in rendered
    assert "**Evidence Coverage**: 66.7%" in rendered
    assert result["risk_debate_state"]["judge_decision"] == rendered


@pytest.mark.unit
def test_portfolio_gate_revises_unsupported_numeric_price_target():
    first_draft = PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="Build the position cautiously.",
        investment_thesis="At CNY 4.31, available price evidence supports buying.",
        material_claim_ids=("market.latest_close",),
        price_target=5.20,
    )
    revised_draft = first_draft.model_copy(update={"price_target": None})
    structured = MagicMock()
    structured.invoke.side_effect = [first_draft, revised_draft]
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
            material_claims=(
                MaterialClaim(
                    claim_id="market.latest_close",
                    analyst="market",
                    statement="The effective-date close was CNY 4.31.",
                    source_refs=("snapshot:000725.SZ:2026-07-16",),
                ),
            ),
            sources=(
                EvidenceSource(
                    source_id="snapshot:000725.SZ:2026-07-16",
                    status=EvidenceStatus.AVAILABLE,
                    required=False,
                ),
            ),
        ).model_dump(mode="json"),
    }

    result = manager(state)

    assert structured.invoke.call_count == 2
    assert "analysis_outcome" not in result
    assert "**Price Target**" not in result["final_trade_decision"]
    assert result["decision_gate"]["revision_applied"] is True


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
    assert evidence.market_snapshot == MarketSnapshotEvidence(
        symbol="000725.SZ",
        provider="akshare",
        retrieved_at="2026-07-17T04:00:00+00:00",
        adjustment_basis="qfq",
        requested_date="2026-07-16",
        effective_trading_date="2026-07-16",
        history_rows=2,
    )


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

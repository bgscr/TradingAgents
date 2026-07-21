"""Graph-level regressions for deterministic trust-boundary routing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

import tradingagents.evidence as evidence_module
import tradingagents.graph.analyst_execution as analyst_execution
from tradingagents.agents import create_msg_delete
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.evidence import (
    AnalysisOutcome,
    CapabilityProfile,
    EvidenceCapability,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MarketSnapshotEvidence,
    evaluate_admission_gate,
    render_analysis_outcome,
)
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.evidence_gate import (
    create_preflight_gate_node,
    route_after_preflight,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup


def _authoritative_baseline() -> EvidenceState:
    return EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="510500.SS",
            venue="XSHG",
            instrument_kind="fund",
            currency="CNY",
            provenance=IdentityProvenance(
                provider="mainland-security-master",
                source_ref="security-master:510500.SS",
                retrieved_at="2026-07-19T12:00:00+00:00",
                artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="510500.SS",
            provider="baostock",
            retrieved_at="2026-07-19T12:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-19",
            effective_trading_date="2026-07-18",
            history_rows=129,
            frame_sha256="a" * 64,
            snapshot_id="snapshot:authoritative",
        ),
    )


@pytest.mark.unit
def test_missing_identity_guard_is_unavailable_without_constructing_analyst():
    factory = MagicMock()
    node = analyst_execution.create_capability_guarded_analyst_node(
        analyst_execution.ANALYST_NODE_SPECS["fundamentals"],
        factory,
    )

    result = node({"evidence_state": EvidenceState().model_dump(mode="json")})

    factory.assert_not_called()
    assert result["fundamentals_report"].startswith("ANALYSIS_UNAVAILABLE:")
    assert result["messages"] == [AIMessage(content=result["fundamentals_report"])]
    assert result["messages"][0].tool_calls == []
    evidence = EvidenceState.model_validate(result["evidence_state"])
    source = next(
        item
        for item in evidence.sources
        if item.source_id == "analyst.fundamentals.submission"
    )
    assert source.status is EvidenceStatus.UNAVAILABLE
    assert source.required is True


@pytest.mark.unit
def test_authoritative_fund_skips_company_fundamentals_without_constructing_analyst():
    factory = MagicMock()
    node = analyst_execution.create_capability_guarded_analyst_node(
        analyst_execution.ANALYST_NODE_SPECS["fundamentals"],
        factory,
    )

    result = node(
        {"evidence_state": _authoritative_baseline().model_dump(mode="json")}
    )

    factory.assert_not_called()
    assert result["fundamentals_report"] == (
        "NOT_APPLICABLE: Fundamentals Analyst is not applicable under "
        "capability profile fund.v1."
    )
    evidence = EvidenceState.model_validate_json(
        EvidenceState.model_validate(result["evidence_state"]).model_dump_json()
    )
    assert evidence.sources[-1].status is EvidenceStatus.NOT_APPLICABLE
    assert evidence.sources[-1].required is False


@pytest.mark.unit
def test_runtime_profile_skip_replaces_stale_applicable_analyst_source():
    stale = _authoritative_baseline().model_copy(
        update={
            "sources": (
                EvidenceSource(
                    source_id="analyst.fundamentals.submission",
                    status=EvidenceStatus.AVAILABLE,
                    required=True,
                    detail="stale prior path",
                ),
            )
        }
    )
    node = analyst_execution.create_capability_guarded_analyst_node(
        analyst_execution.ANALYST_NODE_SPECS["fundamentals"],
        MagicMock(),
    )

    result = node({"evidence_state": stale.model_dump(mode="json")})

    evidence = EvidenceState.model_validate(result["evidence_state"])
    source = next(
        item
        for item in evidence.sources
        if item.source_id == "analyst.fundamentals.submission"
    )
    assert source.status is EvidenceStatus.NOT_APPLICABLE
    assert source.required is False


@pytest.mark.unit
def test_authoritative_equity_runs_company_fundamentals_even_with_unusual_symbol():
    equity_evidence = _authoritative_baseline().model_copy(
        update={
            "instrument_identity": _authoritative_baseline().instrument_identity.model_copy(
                update={
                    "symbol": "ODD-SYMBOL",
                    "instrument_kind": InstrumentKind.EQUITY,
                }
            )
        }
    )
    underlying = MagicMock(return_value={"fundamentals_report": "ran"})
    factory = MagicMock(return_value=underlying)
    node = analyst_execution.create_capability_guarded_analyst_node(
        analyst_execution.ANALYST_NODE_SPECS["fundamentals"],
        factory,
    )
    state = {"evidence_state": equity_evidence.model_dump(mode="json")}

    result = node(state)

    assert result == {"fundamentals_report": "ran"}
    factory.assert_called_once_with()
    underlying.assert_called_once_with(state)


@pytest.mark.unit
def test_optional_not_applicable_analyst_source_does_not_block_admission():
    evidence = _authoritative_baseline().model_copy(
        update={
            "sources": (
                EvidenceSource(
                    source_id="analyst.fundamentals.submission",
                    status=EvidenceStatus.NOT_APPLICABLE,
                    required=False,
                    detail="not applicable under capability profile fund.v1",
                ),
            )
        }
    )

    result = evaluate_admission_gate(evidence)

    assert result.admitted is True
    assert result.readiness.value == "decision_ready"
    assert result.diagnostics == ()


@pytest.mark.unit
def test_preflight_required_checks_are_derived_from_registered_profile(monkeypatch):
    monkeypatch.setitem(
        evidence_module._CAPABILITY_PROFILES,
        InstrumentKind.FUND,
        CapabilityProfile(
            profile_id="fund.test.unsupported-required.v1",
            instrument_kind=InstrumentKind.FUND,
            required_capabilities=(
                EvidenceCapability.MARKET_SNAPSHOT,
                EvidenceCapability.NAV_PREMIUM,
            ),
            applicable_analysts=("market", "social", "news"),
        ),
    )

    result = evidence_module.evaluate_preflight_gate(_authoritative_baseline())

    assert result.passed is False
    assert result.blockers == ("unsupported required capability: nav_premium",)


@pytest.mark.unit
def test_compiled_guarded_fundamentals_skip_advances_through_clear_without_tools():
    factory = MagicMock()
    tool_calls: list[str] = []
    logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
    workflow = StateGraph(AgentState)
    workflow.add_node(
        "Fundamentals Analyst",
        analyst_execution.create_capability_guarded_analyst_node(
            analyst_execution.ANALYST_NODE_SPECS["fundamentals"],
            factory,
        ),
    )
    workflow.add_node(
        "tools_fundamentals",
        lambda state: tool_calls.append("tool") or state,
    )
    workflow.add_node("Msg Clear Fundamentals", create_msg_delete())
    workflow.add_node(
        "Evidence Admission",
        lambda _state: {"admission_gate": {"reached": True}},
    )
    workflow.add_edge(START, "Fundamentals Analyst")
    workflow.add_conditional_edges(
        "Fundamentals Analyst",
        logic.should_continue_fundamentals,
        ["tools_fundamentals", "Msg Clear Fundamentals"],
    )
    workflow.add_edge("tools_fundamentals", "Fundamentals Analyst")
    workflow.add_edge("Msg Clear Fundamentals", "Evidence Admission")
    workflow.add_edge("Evidence Admission", END)
    graph = workflow.compile()

    result = graph.invoke(
        Propagator().create_initial_state(
            "510500.SS",
            "2026-07-19",
            evidence_state=_authoritative_baseline(),
        )
    )

    factory.assert_not_called()
    assert tool_calls == []
    assert result["admission_gate"] == {"reached": True}
    assert result["fundamentals_report"].startswith("NOT_APPLICABLE:")
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert evidence.sources[-1].status is EvidenceStatus.NOT_APPLICABLE


@pytest.mark.unit
def test_preflight_block_emits_non_directional_outcome_before_analysis():
    node = create_preflight_gate_node()

    result = node({"evidence_state": EvidenceState().model_dump(mode="json")})

    assert result["evidence_preflight"]["passed"] is False
    assert route_after_preflight(result) == "blocked"
    assert "Analysis Outcome" in result["analysis_outcome"]
    assert "Trading Decision was issued" in result["analysis_outcome"]
    outcome = AnalysisOutcome.model_validate(result["analysis_outcome_contract"])
    assert outcome.contract_version == "2.0"
    assert result["analysis_outcome"] == render_analysis_outcome(outcome)
    assert result.get("final_trade_decision") is None


@pytest.mark.unit
def test_preflight_blocks_authoritative_baseline_without_policy_configuration():
    node = create_preflight_gate_node()

    result = node(
        {"evidence_state": _authoritative_baseline().model_dump(mode="json")}
    )

    assert result["evidence_preflight"]["passed"] is False
    assert route_after_preflight(result) == "blocked"
    assert "decision_horizon_not_configured" in result["evidence_preflight"]["blockers"]
    assert "no_applicable_registered_strategy_rule" in result["evidence_preflight"]["blockers"]
    assert "analysis_outcome" in result


@pytest.mark.unit
def test_graph_starts_at_preflight_and_blocked_state_invokes_no_model():
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
    graph = workflow.compile()
    llm.reset_mock()

    initial_state = Propagator().create_initial_state(
        "510500.SS",
        "2026-07-19",
        evidence_state=EvidenceState(),
    )
    result = graph.invoke(initial_state)

    assert result["evidence_preflight"]["passed"] is False
    assert "analysis_outcome" in result
    assert "market_report" not in result or not result["market_report"]
    assert result["final_trade_decision"] is None
    assert llm.mock_calls == []


@pytest.mark.unit
def test_compiled_authoritative_fund_abstains_before_analysts_without_fund_policy():
    llm = MagicMock()
    tool_calls: list[str] = []

    def tool_node(state):
        tool_calls.append("tool")
        return state

    graph = GraphSetup(
        llm,
        llm,
        dict.fromkeys(
            ("market", "social", "news", "fundamentals"),
            tool_node,
        ),
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
    ).setup_graph(["market", "fundamentals"]).compile()
    llm.reset_mock()

    result = graph.invoke(
        Propagator().create_initial_state(
            "510500.SS",
            "2026-07-19",
            evidence_state=_authoritative_baseline(),
        )
    )

    assert result["evidence_preflight"]["passed"] is False
    assert "no_applicable_registered_strategy_rule" in result[
        "evidence_preflight"
    ]["blockers"]
    assert result["analysis_outcome_contract"] is not None
    assert not result.get("market_report")
    assert not result.get("fundamentals_report")
    assert result["trading_decision"] is None
    assert result["final_trade_decision"] is None
    assert llm.mock_calls == []
    assert tool_calls == []


@pytest.mark.unit
def test_graph_shape_routes_preflight_before_first_analyst():
    llm = MagicMock()

    def tool_node(state):
        return state

    workflow = GraphSetup(
        llm,
        llm,
        dict.fromkeys(("market", "social", "news", "fundamentals"), tool_node),
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        evidence_gate_mode="enforce",
    ).setup_graph(["market"])

    assert "Evidence Preflight" in workflow.nodes
    assert ("__start__", "Evidence Preflight") in workflow.edges
    branch = workflow.branches["Evidence Preflight"]["route_after_preflight"]
    assert branch.ends == {
        "admitted": "Market Analyst",
        "blocked": "__end__",
    }


@pytest.mark.unit
def test_graph_setup_keeps_analyst_model_binding_lazy_until_runtime():
    quick_llm = MagicMock()
    deep_llm = MagicMock()

    GraphSetup(
        quick_llm,
        deep_llm,
        dict.fromkeys(
            ("market", "social", "news", "fundamentals"),
            lambda state: state,
        ),
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
    ).setup_graph(["fundamentals"])

    quick_llm.bind_tools.assert_not_called()

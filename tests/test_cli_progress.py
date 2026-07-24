from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from cli.run_progress import StateProgressTracker, message_key
from tradingagents.agents.managers.direction_selector import render_trading_decision
from tradingagents.decision_policy import (
    DecisionAssertion,
    DecisionFact,
    DecisionGateResultV2,
    DecisionHorizon,
    DecisionInstrument,
    EvidenceIntegrityStatus,
    HorizonUnit,
    PortfolioRating,
    RulePolarity,
    TradingDecisionContract,
    ValidatedDecisionContext,
)


def _validated_decision_state(
    rating: PortfolioRating = PortfolioRating.BUY,
) -> dict:
    horizon = DecisionHorizon(count=20, unit=HorizonUnit.TRADING_DAYS)
    fact = DecisionFact(
        fact_id="fact:return-20d",
        canonical_field="market.return_20d",
        normalized_value=Decimal("0.08"),
        unit="ratio",
        instrument_symbol="601658.SS",
        effective_date="2026-07-18",
        source_ref="market:601658.SS",
        artifact_sha256="a" * 64,
        source_span_start=0,
        source_span_end=4,
    )
    assertion = DecisionAssertion(
        assertion_id="assertion:return-20d-positive",
        rule_id="market.return_20d.positive",
        rule_version="1",
        fact_ids=(fact.fact_id,),
        target_rating=rating,
        polarity=RulePolarity.SUPPORTS,
        horizon=horizon,
        predicate_id="decimal.greater_than",
        comparator="gt",
        threshold=Decimal("0"),
        evaluation_digest="evaluation:return-20d-positive",
    )
    context = ValidatedDecisionContext(
        context_id="context:return-20d-positive",
        evidence_contract_version="1.0",
        registry_digest="registry:decision-policy-v1",
        calculation_registry_digest="registry:calculations-v1",
        instrument=DecisionInstrument(
            symbol="601658.SS",
            venue="SSE",
            instrument_kind="equity",
            currency="CNY",
        ),
        capability_profile_id="equity.v1",
        as_of_date=date(2026, 7, 18),
        horizon=horizon,
        facts=(fact,),
        assertions=(assertion,),
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
    )
    decision = TradingDecisionContract(
        decision_id="decision:return-20d-positive",
        context_id=context.context_id,
        registry_digest=context.registry_digest,
        calculation_registry_digest=context.calculation_registry_digest,
        instrument=context.instrument,
        as_of_date=context.as_of_date,
        horizon=context.horizon,
        rating=rating,
        facts=context.facts,
        assertions=context.assertions,
        integrity_status=context.integrity_status,
    )
    gate = DecisionGateResultV2(
        permitted=True,
        integrity_status=decision.integrity_status,
        diagnostics=(),
        decision=decision,
    )
    return {
        "validated_decision_context": context.model_dump(mode="json"),
        "decision_gate_v2": gate.model_dump(mode="json"),
        "trading_decision": decision.model_dump(mode="json"),
        "final_trade_decision": render_trading_decision(decision),
    }


@pytest.mark.unit
def test_progress_tracker_reads_state_changes_when_messages_are_cumulative():
    tracker = StateProgressTracker()
    messages = [SimpleNamespace(id="msg-1", content="analyst output")]

    chunks = [
        {
            "messages": messages,
            "market_report": "Market report body",
        },
        {
            "messages": messages,
            "market_report": "Market report body",
            "investment_debate_state": {
                "current_response": "Bull Analyst: upside case",
            },
        },
        {
            "messages": messages,
            "market_report": "Market report body",
            "investment_debate_state": {
                "judge_decision": "**Recommendation**: Underweight",
            },
            "investment_plan": "**Recommendation**: Underweight",
        },
        {
            "messages": messages,
            "market_report": "Market report body",
            "investment_plan": "**Recommendation**: Underweight",
            "trader_investment_plan": "**Action**: Sell",
            "risk_debate_state": {
                "latest_speaker": "Aggressive",
                "current_aggressive_response": "Aggressive Analyst: size up",
            },
        },
        {
            "messages": messages,
            "market_report": "Market report body",
            "investment_plan": "**Recommendation**: Underweight",
            "trader_investment_plan": "**Action**: Sell",
            "risk_debate_state": {
                "judge_decision": "**Rating**: Underweight",
            },
            **_validated_decision_state(PortfolioRating.UNDERWEIGHT),
        },
    ]

    events = [event for chunk in chunks for event in tracker.events_for(chunk)]

    assert [(event.message_type, event.content) for event in events] == [
        ("Analysis", "Market Analyst produced market report"),
        ("Research", "Bull Researcher updated investment debate"),
        ("Research", "Research Manager produced investment plan"),
        ("Trading", "Trader produced transaction plan"),
        ("Risk", "Aggressive Analyst updated risk debate"),
        ("Portfolio", "Final decision ready: Underweight"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "decision",
    [
        "**评级：买入**",
        "**最终交易决策: 增持**",
        "**评级： 持有**",
        "**最终交易决策：卖出**\n\n降低敞口。",
        "**评级: 减持**\n\n控制仓位。",
    ],
)
def test_progress_tracker_does_not_infer_decisions_from_localized_prose(decision):
    tracker = StateProgressTracker()

    events = tracker.events_for({"final_trade_decision": decision})

    assert events == []


@pytest.mark.unit
def test_progress_tracker_deduplicates_repeated_full_state():
    tracker = StateProgressTracker()
    chunk = {
        "messages": [SimpleNamespace(id="msg-1", content="done")],
        "risk_debate_state": {
            "latest_speaker": "Neutral",
            "current_neutral_response": "Neutral Analyst: wait",
        },
    }

    assert len(tracker.events_for(chunk)) == 1
    assert tracker.events_for(chunk) == []

    updated = {
        **chunk,
        "risk_debate_state": {
            "latest_speaker": "Neutral",
            "current_neutral_response": "Neutral Analyst: reduce size",
        },
    }
    assert len(tracker.events_for(updated)) == 1


@pytest.mark.unit
def test_progress_tracker_ignores_malformed_or_empty_optional_state():
    tracker = StateProgressTracker()

    assert tracker.events_for(None) == []
    assert tracker.events_for({"investment_debate_state": None}) == []
    assert tracker.events_for({"risk_debate_state": "bad-state"}) == []
    assert tracker.events_for({"final_trade_decision": ""}) == []


@pytest.mark.unit
def test_progress_tracker_does_not_announce_unvalidated_decision_prose():
    tracker = StateProgressTracker()

    events = tracker.events_for(
        {
            "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **SELL**",
            "final_trade_decision": "**Rating**: Buy\n\nUnvalidated prose.",
        }
    )

    assert [(event.message_type, event.content) for event in events] == [
        ("Trading", "Trader produced transaction plan"),
    ]


@pytest.mark.unit
def test_progress_tracker_announces_only_validated_buy_after_advisory_sell():
    tracker = StateProgressTracker()
    state = _validated_decision_state()

    advisory_events = tracker.events_for(
        {
            "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **SELL**",
            "validated_decision_context": state["validated_decision_context"],
        }
    )
    decision_events = tracker.events_for(
        {
            "decision_gate_v2": state["decision_gate_v2"],
            "trading_decision": state["trading_decision"],
            "final_trade_decision": state["final_trade_decision"],
        }
    )

    assert [
        (event.message_type, event.content)
        for event in advisory_events + decision_events
    ] == [
        ("Trading", "Trader produced transaction plan"),
        ("Portfolio", "Final decision ready: Buy"),
    ]


@pytest.mark.unit
def test_message_key_prefers_id_and_fingerprints_idless_messages():
    with_id = SimpleNamespace(id="abc", content="same", tool_calls=[])
    first = SimpleNamespace(id=None, content="same", tool_calls=[])
    second = SimpleNamespace(id=None, content="same", tool_calls=[])
    changed = SimpleNamespace(id=None, content="changed", tool_calls=[])

    assert message_key(with_id) == "id:abc"
    assert message_key(first) == message_key(second)
    assert message_key(first).startswith("fingerprint:")
    assert message_key(first) != message_key(changed)

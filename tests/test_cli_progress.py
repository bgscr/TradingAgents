from types import SimpleNamespace

import pytest

from cli.run_progress import StateProgressTracker, message_key


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
            "final_trade_decision": "**Rating**: Underweight\n\nReduce exposure.",
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
    ("decision", "rating"),
    [
        ("**评级：买入**", "Buy"),
        ("**最终交易决策: 增持**", "Overweight"),
        ("**评级： 持有**", "Hold"),
        ("**最终交易决策：卖出**\n\n降低敞口。", "Sell"),
        ("**评级: 减持**\n\n控制仓位。", "Underweight"),
    ],
)
def test_progress_tracker_maps_anchored_chinese_final_decisions(decision, rating):
    tracker = StateProgressTracker()

    events = tracker.events_for({"final_trade_decision": decision})

    assert len(events) == 1
    assert events[0].content == f"Final decision ready: {rating}"


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
def test_message_key_prefers_id_and_fingerprints_idless_messages():
    with_id = SimpleNamespace(id="abc", content="same", tool_calls=[])
    first = SimpleNamespace(id=None, content="same", tool_calls=[])
    second = SimpleNamespace(id=None, content="same", tool_calls=[])
    changed = SimpleNamespace(id=None, content="changed", tool_calls=[])

    assert message_key(with_id) == "id:abc"
    assert message_key(first) == message_key(second)
    assert message_key(first).startswith("fingerprint:")
    assert message_key(first) != message_key(changed)

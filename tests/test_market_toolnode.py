"""The market analyst is bound (and prompt-instructed) to call
get_verified_market_snapshot; if the executor ToolNode doesn't register it, the
call fails and the model reports the tool "unavailable" and skips verification.

Regression guard for that wiring gap (snapshot bound to the LLM but missing from
the market ToolNode).
"""
import inspect

import pytest
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.utils.technical_indicators_tools import get_indicators
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.mark.unit
def test_market_toolnode_can_execute_verified_snapshot():
    # _create_tool_nodes does not use self -> call unbound (avoids building LLMs).
    nodes = TradingAgentsGraph._create_tool_nodes(None)
    market_tools = set(nodes["market"].tools_by_name)
    assert "get_verified_market_snapshot" in market_tools, (
        "get_verified_market_snapshot is bound to the market analyst but not "
        "registered in the market ToolNode, so the model's call fails."
    )
    # the other core market tools must remain too
    assert {"get_stock_data", "get_indicators"} <= market_tools


@pytest.mark.unit
def test_market_prompt_batches_indicator_tool_calls():
    prompt_source = inspect.getsource(create_market_analyst)
    tool_source = inspect.getsource(get_indicators.func)

    assert "comma-separated" in prompt_source
    assert "single get_indicators call" in prompt_source
    assert "comma-separated" in tool_source
    assert "Call this tool once per indicator" not in tool_source


@pytest.mark.unit
def test_market_toolnode_turns_typed_vendor_failure_into_data_unavailable():
    node = TradingAgentsGraph._create_tool_nodes(None)["market"]
    tool = node.tools_by_name["get_stock_data"]
    original = tool.func

    def unavailable(*args, **kwargs):
        raise NoMarketDataError("510500.SS", detail="no usable rows")

    tool.func = unavailable
    try:
        result = node.func(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "get_stock_data",
                                "args": {
                                    "symbol": "510500.SS",
                                    "start_date": "2026-06-01",
                                    "end_date": "2026-06-29",
                                },
                                "id": "market-data-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            config={},
            runtime=Runtime(),
        )
    finally:
        tool.func = original

    message = result["messages"][0]
    assert message.tool_call_id == "market-data-call"
    assert message.status == "error"
    assert message.content.startswith("DATA_UNAVAILABLE:")


@pytest.mark.unit
def test_market_toolnode_does_not_mask_programming_errors():
    node = TradingAgentsGraph._create_tool_nodes(None)["market"]
    tool = node.tools_by_name["get_stock_data"]
    original = tool.func

    def broken(*args, **kwargs):
        raise AssertionError("programming bug")

    tool.func = broken
    try:
        with pytest.raises(AssertionError, match="programming bug"):
            node.func(
                {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "get_stock_data",
                                    "args": {
                                        "symbol": "510500.SS",
                                        "start_date": "2026-06-01",
                                        "end_date": "2026-06-29",
                                    },
                                    "id": "broken-call",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                },
                config={},
                runtime=Runtime(),
            )
    finally:
        tool.func = original

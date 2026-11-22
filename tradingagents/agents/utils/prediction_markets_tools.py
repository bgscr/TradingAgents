from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from tradingagents.budget_data import policy_disabled_tool_result
from tradingagents.dataflows.interface import route_to_vendor


@tool(response_format="content_and_artifact")
def get_prediction_markets(
    topic: Annotated[
        str,
        "Event topic/keyword, e.g. 'Fed rate cut', 'recession 2026', "
        "'US election', or a sector/company event.",
    ],
    limit: Annotated[int | None, "Max markets to return; omit for a default of 6"] = None,
    runtime: ToolRuntime = None,
) -> tuple[str, dict[str, object] | None]:
    """
    Retrieve live, market-implied probabilities for forward-looking events from
    prediction markets (Polymarket): Fed decisions, recession, elections,
    geopolitics, crypto. Returns the most-traded open markets matching the
    topic, each with its implied probability, traded volume, resolution date,
    and recent move. Uses the configured prediction_markets vendor.

    Args:
        topic (str): Event keyword(s) to search
        limit (int): Max markets to return; omit for a default of 6

    Returns:
        str: A formatted markdown report of matching prediction markets
    """
    result = route_to_vendor("get_prediction_markets", topic, limit)
    return policy_disabled_tool_result(result, method="get_prediction_markets",
                                       tool_call_id=runtime.tool_call_id if runtime is not None else None)

from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool

from tradingagents.dataflows.interface import route_to_vendor, route_to_vendor_acquired
from tradingagents.evidence import (
    ToolExecutionEvidenceEnvelope,
    stable_acquisition_source_ref,
)


def get_news_legacy(ticker: str, start_date: str, end_date: str) -> str:
    """Legacy plain-content path for direct, non-ToolNode sentiment collection."""
    return route_to_vendor("get_news", ticker, start_date, end_date)


def acquire_news(
    ticker: str,
    start_date: str,
    end_date: str,
    *,
    tool_call_id: str,
    source_ref: str,
    capability: str,
):
    """Acquire news through the run-owned controller for non-ToolNode callers."""
    return route_to_vendor_acquired(
        "get_news",
        ticker,
        start_date,
        end_date,
        tool_call_id=tool_call_id,
        source_ref=source_ref,
        capability=capability,
    )


@tool(response_format="content_and_artifact")
def get_news(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    tool_call_id: Annotated[str, InjectedToolCallId()],
) -> tuple[str, dict[str, object]]:
    """
    Retrieve news data for a given ticker symbol.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data
    """
    source_ref = stable_acquisition_source_ref(
        "news",
        ticker,
        start_date,
        end_date,
    )
    result = acquire_news(
        ticker,
        start_date,
        end_date,
        tool_call_id=tool_call_id,
        source_ref=source_ref,
        capability="get_news",
    )
    content = result.value
    if content is None:
        reason = result.outcomes[-1].reason.value if result.outcomes else "provider_error"
        content = f"DATA_UNAVAILABLE: news acquisition unavailable ({reason})."
    envelope = ToolExecutionEvidenceEnvelope(
        tool_call_id=tool_call_id,
        tool_name="get_news",
        source_ref=source_ref,
        capability="get_news",
        acquisition_outcomes=result.outcomes,
        selected_artifact=result.artifact,
    )
    return content, envelope.model_dump(mode="json")

@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int | None, "Days to look back; omit to use the configured default"] = None,
    limit: Annotated[int | None, "Max articles to return; omit to use the configured default"] = None,
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor. Defaults for look_back_days and
    limit come from DEFAULT_CONFIG (global_news_lookback_days,
    global_news_article_limit); pass explicit values to override.

    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back; omit to inherit config
        limit (int): Maximum number of articles to return; omit to inherit config

    Returns:
        str: A formatted string containing global news data
    """
    return route_to_vendor("get_global_news", curr_date, look_back_days, limit)

@tool
def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """
    Retrieve insider transaction information about a company.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
    Returns:
        str: A report of insider transaction data
    """
    return route_to_vendor("get_insider_transactions", ticker)

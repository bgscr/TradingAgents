from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool

from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.market_snapshot import render_authoritative_market_data
from tradingagents.dataflows.symbol_utils import resolve_china_a_symbol


@tool
def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve stock price data (OHLCV) for a given ticker symbol.
    Uses the configured core_stock_apis vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
    """
    if resolve_china_a_symbol(symbol) is not None:
        return render_authoritative_market_data(symbol, start_date, end_date)
    return route_to_vendor("get_stock_data", symbol, start_date, end_date)


@tool("get_stock_data", response_format="content_and_artifact")
def get_stock_data_with_evidence(
    symbol: str, start_date: str, end_date: str,
    tool_call_id: Annotated[str, InjectedToolCallId()],
) -> tuple[str, dict[str, object] | None]:
    """Return OHLCV with call-bound provenance for a run-owned mainland snapshot.

    The legacy string API and non-mainland routing remain unchanged. Rendered
    rows are excerpts; this envelope does not create canonical decision facts.
    """
    from tradingagents.agents.utils.market_data_validation_tools import market_projection_envelope

    content = get_stock_data.func(symbol, start_date, end_date)
    if resolve_china_a_symbol(symbol) is None:
        return content, None
    return content, market_projection_envelope(
        content, symbol=symbol, curr_date=end_date, tool_name="get_stock_data",
        tool_call_id=tool_call_id, capability="get_stock_data", metadata_prefix="# ",
    )

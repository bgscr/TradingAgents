from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool

from tradingagents.agents.utils.market_data_validation_tools import market_projection_envelope
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.market_snapshot import build_authoritative_indicator_window
from tradingagents.dataflows.symbol_utils import resolve_china_a_symbol


@tool
def get_indicators(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
) -> str:
    """
    Retrieve one or more technical indicators for a given ticker symbol.
    Uses the configured technical_indicators vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        indicator (str): A technical indicator name, e.g. 'rsi', or a comma-separated list such as 'rsi,macd,boll'.
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back, default is 30
    Returns:
        str: Formatted indicator windows for the specified ticker symbol and indicator(s).
    """
    # LLMs sometimes pass multiple indicators as a comma-separated string;
    # split and process each individually.
    indicators = [i.strip().lower() for i in indicator.split(",") if i.strip()]
    results = []
    for ind in indicators:
        try:
            if resolve_china_a_symbol(symbol) is not None:
                results.append(
                    build_authoritative_indicator_window(
                        symbol, ind, curr_date, look_back_days
                    )
                )
            else:
                results.append(
                    route_to_vendor(
                        "get_indicators", symbol, ind, curr_date, look_back_days
                    )
                )
        except ValueError as e:
            results.append(str(e))
    return "\n\n".join(results)


@tool("get_indicators", response_format="content_and_artifact")
def get_indicators_with_evidence(
    symbol: str, indicator: str, curr_date: str,
    tool_call_id: Annotated[str, InjectedToolCallId()], look_back_days: int = 30,
) -> tuple[str, dict[str, object]]:
    """Return comma-separated indicator projections with run-owned provenance."""
    from tradingagents.dataflows.akshare_data import INDICATOR_DESCRIPTIONS

    requested = [item.strip().lower() for item in indicator.split(",") if item.strip()]
    # The legacy string API includes user input in unsupported-indicator errors.
    # Never mistake copied provenance markers in an error for a projection.
    complete = bool(requested) and all(item in INDICATOR_DESCRIPTIONS for item in requested)
    content = get_indicators.func(symbol, indicator, curr_date, look_back_days)
    return content, market_projection_envelope(
        content, symbol=symbol, curr_date=curr_date, tool_name="get_indicators",
        tool_call_id=tool_call_id, capability="get_indicators",
        segment_count=len(requested) if complete else 0,
    )

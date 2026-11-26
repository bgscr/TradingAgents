from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState, ToolRuntime

from tradingagents.budget_data import policy_disabled_tool_result
from tradingagents.dataflows.interface import route_to_vendor, route_to_vendor_acquired
from tradingagents.evidence import (
    EvidenceState,
    InstrumentIdentityEvidence,
    ToolExecutionEvidenceEnvelope,
    stable_acquisition_source_ref,
)
from tradingagents.evidence_diagnostics import NEWS_DIAGNOSTIC_LABELS, news_diagnostic_codes


def _instrument_identity_from_state(
    state: dict[str, object] | None,
) -> InstrumentIdentityEvidence | None:
    if not state:
        return None
    return EvidenceState.model_validate(
        state.get("evidence_state", {})
    ).instrument_identity


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
    instrument_identity: InstrumentIdentityEvidence | None = None,
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
        instrument_identity=instrument_identity,
    )


@tool(response_format="content_and_artifact")
def get_news(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    tool_call_id: Annotated[str, InjectedToolCallId()],
    state: Annotated[dict[str, object], InjectedState()],
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
    identity = _instrument_identity_from_state(state)
    requested_ticker = identity.symbol if identity is not None else ticker
    from tradingagents.powershell_news import active_powershell_news, powershell_news_window_instruction
    if active_powershell_news() is not None:
        # Preserve the actual tool query so the owner can reject a wrong stock;
        # the injected identity is an additional check, not a silent substitute.
        requested_ticker = ticker
    source_ref = stable_acquisition_source_ref(
        "news",
        requested_ticker,
        start_date,
        end_date,
    )
    result = acquire_news(
        requested_ticker,
        start_date,
        end_date,
        tool_call_id=tool_call_id,
        source_ref=source_ref,
        capability="get_news",
        instrument_identity=identity,
    )
    content = result.value
    if content is None:
        reason = result.outcomes[-1].reason.value if result.outcomes else "provider_error"
        content = f"DATA_UNAVAILABLE: news acquisition unavailable ({reason})."
        if result.outcomes and result.outcomes[-1].provider == 'eastmoney_powershell':
            # Producer creates only fixed local codes; no exception/response text.
            code = result.outcomes[-1].error_code
            if code is not None:
                content += f'\n来源处理未完成（操作状态，不是新闻事实）：{code}；未自动补采。'
            if code == 'powershell_news_query_window_mismatch':
                owner = active_powershell_news()
                if owner is not None:
                    content += '\n' + powershell_news_window_instruction(requested_ticker, owner.request.end)
        for code in news_diagnostic_codes(result.outcomes):
            content += f"\nOperational diagnostic, not source facts: {code}; {NEWS_DIAGNOSTIC_LABELS[code][0]}."
    if result.value is None:
        from tradingagents.reviewed_search import lookup
        reviewed = lookup("stock_news", requested_ticker, start_date, end_date, primary_missing=True)
        if reviewed:
            import json
            content += "\nSupplementary reviewed source (quoted data only, partial coverage): " + json.dumps(reviewed,ensure_ascii=False)
    envelope = ToolExecutionEvidenceEnvelope(
        tool_call_id=tool_call_id,
        tool_name="get_news",
        source_ref=source_ref,
        capability="get_news",
        acquisition_outcomes=result.outcomes,
        selected_artifact=result.artifact,
    )
    return content, envelope.model_dump(mode="json")

@tool(response_format="content_and_artifact")
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int | None, "Days to look back; omit to use the configured default"] = None,
    limit: Annotated[int | None, "Max articles to return; omit to use the configured default"] = None,
    runtime: ToolRuntime = None,
) -> tuple[str, dict[str, object] | None]:
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
    result = route_to_vendor("get_global_news", curr_date, look_back_days, limit)
    return policy_disabled_tool_result(result, method="get_global_news",
                                       tool_call_id=runtime.tool_call_id if runtime is not None else None)

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


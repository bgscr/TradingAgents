"""Compiled-graph boundary for deterministic company-financial tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from tradingagents.asset_configuration import RunAssetConfigurationProjection
from tradingagents.dataflows.acquisition import RetryPolicy
from tradingagents.dataflows.financial_dispatch import (
    FinancialReportingFrequency,
    FinancialStatementType,
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.evidence import EvidenceState

_FINANCIAL_TOOL_CONTRACTS = {
    "get_fundamentals": (
        FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        FinancialReportingFrequency.NOT_APPLICABLE,
    ),
    "get_balance_sheet": (
        FinancialStatementType.BALANCE_SHEET,
        FinancialReportingFrequency.QUARTERLY,
    ),
    "get_cashflow": (
        FinancialStatementType.CASH_FLOW,
        FinancialReportingFrequency.QUARTERLY,
    ),
    "get_income_statement": (
        FinancialStatementType.INCOME_STATEMENT,
        FinancialReportingFrequency.QUARTERLY,
    ),
}


class FinancialDispatchToolNode:
    """Execute one fundamentals tool turn through a checkpoint-aware dispatcher."""

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        vendor_methods: Mapping[str, Mapping[str, object]] | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._config = dict(config) if config is not None else None
        self._vendor_methods = vendor_methods
        self._retry_policy = retry_policy
        self._clock = clock
        self._sleeper = sleeper

    def __call__(self, state: Mapping[str, Any]) -> dict[str, Any]:
        messages = state.get("messages") or ()
        if not messages:
            raise ValueError("financial tool node requires a model tool-call message")
        model_message = messages[-1]
        tool_calls = getattr(model_message, "tool_calls", None) or ()
        if not tool_calls:
            raise ValueError("financial tool node requires at least one tool call")

        evidence = EvidenceState.model_validate(state.get("evidence_state") or {})
        identity = evidence.instrument_identity
        if identity is None or not identity.is_authoritative:
            raise ValueError("financial tool node requires authoritative Instrument Identity")
        asset_payload = state.get("asset_configuration")
        if not isinstance(asset_payload, Mapping):
            raise ValueError("financial tool node requires RunAssetConfiguration")
        asset_configuration = RunAssetConfigurationProjection.model_validate(asset_payload)
        effective_config = self._config
        if effective_config is None:
            from tradingagents.dataflows.config import get_config

            effective_config = get_config()
        retry_policy = self._retry_policy
        if retry_policy is None:
            configured_retry_policy = effective_config.get("financial_dispatch_retry_policy")
            if configured_retry_policy is not None:
                retry_policy = RetryPolicy.model_validate(configured_retry_policy)
        dispatcher = FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=identity,
            run_asset_configuration=asset_configuration,
            config=effective_config,
            vendor_methods=self._vendor_methods,
            retry_policy=retry_policy,
            clock=self._clock,
            sleeper=self._sleeper,
            checkpoint_ledger=state.get("financial_dispatch_ledger"),
        )
        graph_message_id = getattr(model_message, "id", None)
        tool_messages = [
            dispatcher.dispatch_tool_message(
                _financial_request_from_tool_call(
                    tool_call,
                    canonical_symbol=identity.symbol,
                    trade_date=str(state.get("trade_date") or ""),
                    graph_message_id=(
                        str(graph_message_id) if graph_message_id is not None else None
                    ),
                )
            )
            for tool_call in tool_calls
        ]
        from tradingagents.dataflows.market_snapshot import (
            refresh_active_evidence_physical_attempts,
        )

        evidence = refresh_active_evidence_physical_attempts(evidence)
        return {
            "messages": tool_messages,
            "evidence_state": evidence.model_dump(mode="json"),
            "financial_dispatch_ledger": dispatcher.checkpoint_ledger(),
        }


def _financial_request_from_tool_call(
    tool_call: Mapping[str, Any],
    *,
    canonical_symbol: str,
    trade_date: str,
    graph_message_id: str | None,
) -> FinancialToolRequest:
    tool_name = str(tool_call.get("name") or "")
    try:
        statement_type, default_frequency = _FINANCIAL_TOOL_CONTRACTS[tool_name]
    except KeyError as exc:
        raise ValueError("unsupported financial tool call") from exc
    raw_arguments = tool_call.get("args") or {}
    if not isinstance(raw_arguments, Mapping):
        raise ValueError("financial tool arguments must be a mapping")
    material_arguments = dict(raw_arguments)
    requested_symbol = str(
        material_arguments.pop("ticker", material_arguments.pop("symbol", ""))
    ).strip()
    if requested_symbol.upper() != canonical_symbol.strip().upper():
        raise ValueError("financial tool symbol contradicts Instrument Identity")
    raw_frequency = material_arguments.pop(
        "freq",
        material_arguments.pop("frequency", default_frequency.value),
    )
    frequency = (
        FinancialReportingFrequency.NOT_APPLICABLE
        if statement_type is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
        else FinancialReportingFrequency(str(raw_frequency).strip().casefold())
    )
    raw_as_of_date = material_arguments.pop(
        "curr_date",
        material_arguments.pop("as_of_date", trade_date),
    )
    return FinancialToolRequest(
        tool_name=tool_name,
        statement_type=statement_type,
        frequency=frequency,
        as_of_date=date.fromisoformat(str(raw_as_of_date)),
        material_arguments=material_arguments,
        tool_call_id=str(tool_call.get("id") or ""),
        graph_message_id=graph_message_id,
    )


__all__ = ["FinancialDispatchToolNode"]

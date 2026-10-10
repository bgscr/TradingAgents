"""Publication-safe explanations; never copy source refs or exception text."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from tradingagents.evidence import AcquisitionUnavailableReason


class EvidenceDiagnosticCode(str, Enum):
    SOURCE_NOT_REGISTERED = "source_not_registered"
    TOOL_RESULT_MISSING = "tool_result_missing"
    TRUSTED_METADATA_MISSING = "trusted_metadata_missing"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_QUOTE_MISSING = "source_quote_missing"
    SOURCE_QUOTE_AMBIGUOUS = "source_quote_ambiguous"
    QUOTE_IS_METADATA = "quote_is_metadata"
    NUMERIC_CLAIM_UNSUPPORTED = "numeric_claim_unsupported"
    FACT_BINDING_MISSING = "fact_binding_missing"
    DETAILS_NOT_RETAINED = "details_not_retained"


class PublicSourceDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: Literal[
        "Authoritative market snapshot",
        "Market price history",
        "Technical indicators",
        "Company news",
        "Global news",
        "Company fundamentals",
        "Balance sheet",
        "Income statement",
        "Cash flow",
        "Instrument identity",
        "Unidentified source",
        "Unregistered source reference",
    ]
    capability: Literal[
        "market_snapshot",
        "market_history",
        "technical_indicators",
        "news",
        "global_news",
        "fundamentals",
        "balance_sheet",
        "income_statement",
        "cash_flow",
        "instrument_identity",
        "unknown",
    ]
    reason: EvidenceDiagnosticCode
    acquisition_reason: AcquisitionUnavailableReason | None = None


# Only application-owned names are publishable. Unknown references remain opaque.
_SOURCE_NAMES = {
    "snapshot": ("Authoritative market snapshot", "market_snapshot"),
    "market_snapshot": ("Authoritative market snapshot", "market_snapshot"),
    "get_verified_market_snapshot": ("Authoritative market snapshot", "market_snapshot"),
    "get_stock_data": ("Market price history", "market_history"),
    "get_indicators": ("Technical indicators", "technical_indicators"),
    "get_news": ("Company news", "news"),
    "get_global_news": ("Global news", "global_news"),
    "get_fundamentals": ("Company fundamentals", "fundamentals"),
    "get_balance_sheet": ("Balance sheet", "balance_sheet"),
    "get_income_statement": ("Income statement", "income_statement"),
    "get_cashflow": ("Cash flow", "cash_flow"),
    "instrument_identity": ("Instrument identity", "instrument_identity"),
}


def source_diagnostic(source, outcomes=()) -> PublicSourceDiagnostic:
    reason = {
        "source ref is not in allowed catalog": EvidenceDiagnosticCode.SOURCE_NOT_REGISTERED,
        "no matching tool result": EvidenceDiagnosticCode.TOOL_RESULT_MISSING,
        "trusted acquisition metadata not exposed": EvidenceDiagnosticCode.TRUSTED_METADATA_MISSING,
        "tool returned unavailable": EvidenceDiagnosticCode.SOURCE_UNAVAILABLE,
        "source returned unavailable": EvidenceDiagnosticCode.SOURCE_UNAVAILABLE,
    }.get(source.detail, EvidenceDiagnosticCode.DETAILS_NOT_RETAINED)
    name, capability = _SOURCE_NAMES.get(
        source.source_id.partition(":")[0], ("Unidentified source", "unknown")
    )
    acquisition_reason = None
    matching = sorted(
        (outcome for outcome in outcomes if outcome.source_ref == source.source_id),
        key=lambda outcome: (outcome.provider_order, outcome.attempt, outcome.provider),
    )
    for outcome in matching:
        if outcome.capability in _SOURCE_NAMES:
            name, capability = _SOURCE_NAMES[outcome.capability]
        if outcome.outcome == "unavailable":
            acquisition_reason = outcome.reason
            reason = EvidenceDiagnosticCode.SOURCE_UNAVAILABLE
    if reason is EvidenceDiagnosticCode.SOURCE_NOT_REGISTERED:
        name, capability = "Unregistered source reference", "unknown"
    return PublicSourceDiagnostic(
        display_name=name,
        capability=capability,
        reason=reason,
        acquisition_reason=acquisition_reason,
    )


def claim_diagnostic(validation) -> EvidenceDiagnosticCode:
    detail = validation.detail
    if (
        detail.startswith("cited source unavailable:")
        or detail == "source quote reports unavailable evidence"
    ):
        return EvidenceDiagnosticCode.SOURCE_UNAVAILABLE
    if detail in {
        "source quote is ambiguous within the cited tool result",
        "source quote is ambiguous within the cited source block",
    }:
        return EvidenceDiagnosticCode.SOURCE_QUOTE_AMBIGUOUS
    if detail in {
        "source quote is absent from the cited tool result",
        "source quote is absent from the cited source block",
    }:
        return EvidenceDiagnosticCode.SOURCE_QUOTE_MISSING
    if detail == "source quote contains provenance metadata, not a material fact":
        return EvidenceDiagnosticCode.QUOTE_IS_METADATA
    if detail.startswith("source does not contain numeric claim(s):"):
        return EvidenceDiagnosticCode.NUMERIC_CLAIM_UNSUPPORTED
    if not validation.fact_ids:
        return EvidenceDiagnosticCode.FACT_BINDING_MISSING
    return EvidenceDiagnosticCode.DETAILS_NOT_RETAINED


DIAGNOSTIC_LABELS = {
    "source_not_registered": ("Source reference is not registered", "来源引用未在允许目录中登记"),
    "tool_result_missing": ("No matching tool result", "缺少匹配的工具结果"),
    "trusted_metadata_missing": ("Trusted acquisition metadata is missing", "缺少可信采集元数据"),
    "source_unavailable": ("Cited source is unavailable", "引用来源不可用"),
    "source_quote_missing": ("Exact source quote is absent", "找不到完全匹配的原文引用"),
    "source_quote_ambiguous": ("Source quote is ambiguous", "原文引用存在歧义"),
    "quote_is_metadata": (
        "Quote is metadata, not a material fact",
        "引用内容是元数据，不是实质事实",
    ),
    "numeric_claim_unsupported": (
        "Numeric claim is not supported by the source",
        "来源不支持该数值主张",
    ),
    "fact_binding_missing": ("No validated fact binding", "没有经验证的事实绑定"),
    "details_not_retained": ("Detailed reason was not retained", "详细原因未保留，无法还原"),
}

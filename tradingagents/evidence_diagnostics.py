"""Publication-safe explanations; never copy source refs or exception text."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from tradingagents.evidence import AcquisitionUnavailableReason

# Operational presentation only; these labels do not create evidence or change
# availability/decision coverage. Scope each note to an attempt, since another
# attempt or provider may have succeeded in the same run.
NEWS_DIAGNOSTIC_LABELS = {
    "news_window_coverage_unknown": (
        "An AKShare news response supplied usable articles, but full historical coverage of the requested period is unverified. The adapter reads one response page; hitsTotal does not prove period coverage",
        "一次 AKShare 新闻响应提供了可用条目，但请求期间的完整历史覆盖未经证实。适配器只读取一个响应页；hitsTotal 不能证明期间覆盖完整",
    ),
    "news_page_no_eligible_articles": (
        "No eligible articles remained on this response page from AKShare after validation, date and stock filtering. This does not establish that the requested period had no news",
        "一次 AKShare 采集中，本响应页经校验、日期和股票筛选后没有合格条目。这不表示请求期间确实没有新闻",
    ),
    "news_unavailable": (
        "Usable news was unavailable in an AKShare acquisition attempt. This does not establish that the requested period had no news",
        "一次 AKShare 新闻采集未能提供可用条目，来源在该次尝试中不可用。这不表示请求期间确实没有新闻",
    ),
}


def news_diagnostic_codes(outcomes):
    """Derive fixed notes from trusted typed outcomes, never raw error text."""
    codes = []
    for outcome in outcomes:
        if outcome.provider != "akshare" or outcome.capability != "get_news":
            continue
        if outcome.outcome == "available":
            code = "news_window_coverage_unknown"
        elif (
            outcome.reason is AcquisitionUnavailableReason.NO_DATA
            and outcome.error_code == "news_no_usable_articles"
        ):
            code = "news_page_no_eligible_articles"
        else:
            code = "news_unavailable"
        if code not in codes:
            codes.append(code)
    return tuple(codes)


class EvidenceDiagnosticCode(str, Enum):
    SOURCE_NOT_REGISTERED = "source_not_registered"
    TOOL_RESULT_MISSING = "tool_result_missing"
    TRUSTED_METADATA_MISSING = "trusted_metadata_missing"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_QUOTE_MISSING = "source_quote_missing"
    SOURCE_QUOTE_AMBIGUOUS = "source_quote_ambiguous"
    QUOTE_IS_METADATA = "quote_is_metadata"
    NUMERIC_CLAIM_UNSUPPORTED = "numeric_claim_unsupported"
    INDICATOR_IDENTITY_CONFLICT = "indicator_identity_conflict"
    INDICATOR_BINDING_UNVERIFIED = "indicator_binding_unverified"
    INDICATOR_DATE_CONFLICT = "indicator_date_conflict"
    INDICATOR_UNITS_UNVERIFIED = "indicator_units_unverified"
    INDICATOR_VALUE_MISSING = "indicator_value_missing"
    INDICATOR_LATEST_BINDING_MISSING = "indicator_latest_binding_missing"
    INDICATOR_DATE_INVALID = "indicator_date_invalid"
    SNAPSHOT_DATE_CONFLICT = "snapshot_date_conflict"
    SNAPSHOT_DATE_UNBOUND = "snapshot_date_unbound"
    SNAPSHOT_VALUE_MISSING = "snapshot_value_missing"
    FACT_BINDING_MISSING = "fact_binding_missing"
    DETAILS_NOT_RETAINED = "details_not_retained"
    ANALYST_OUTPUT_LIMIT = "analyst_output_limit"
    FINANCIAL_ROWS_INELIGIBLE = "financial_rows_ineligible"


class PublicSourceDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: Literal[
        "Authoritative market snapshot",
        "Market price history",
        "Technical indicators",
        "Company news",
        "Global news",
        "Macroeconomic indicators",
        "Prediction markets",
        "Company fundamentals",
        "Company financials",
        "Balance sheet",
        "Income statement",
        "Cash flow",
        "Instrument identity",
        "Market analyst submission",
        "Sentiment analyst submission",
        "News analyst submission",
        "Fundamentals analyst submission",
        "Unidentified source",
        "Unregistered source reference",
    ]
    capability: Literal[
        "market_snapshot",
        "market_history",
        "technical_indicators",
        "news",
        "global_news",
        "macro_indicators",
        "prediction_markets",
        "fundamentals",
        "company_financials",
        "balance_sheet",
        "income_statement",
        "cash_flow",
        "instrument_identity",
        "analyst_submission",
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

# Capability labels, not provider provenance or acquired Source Facts. Unlike
# legacy prefixes, these require a matching trusted acquisition outcome below.
_ACQUISITION_CAPABILITY_NAMES = {
    "company_financials": ("Company financials", "company_financials"),
    "get_macro_indicators": ("Macroeconomic indicators", "macro_indicators"),
    "get_prediction_markets": ("Prediction markets", "prediction_markets"),
}

# Exact application-owned identifiers; never interpret arbitrary reference suffixes.
_ANALYST_SUBMISSION_NAMES = {
    "analyst.market.submission": "Market analyst submission",
    "analyst.sentiment.submission": "Sentiment analyst submission",
    "analyst.news.submission": "News analyst submission",
    "analyst.fundamentals.submission": "Fundamentals analyst submission",
}


def source_diagnostic(source, outcomes=()) -> PublicSourceDiagnostic:
    if source.source_id in _ANALYST_SUBMISSION_NAMES:
        return PublicSourceDiagnostic(
            display_name=_ANALYST_SUBMISSION_NAMES[source.source_id],
            capability="analyst_submission",
            reason=(
                EvidenceDiagnosticCode.ANALYST_OUTPUT_LIMIT
                if source.detail == "output_limit"
                else EvidenceDiagnosticCode.DETAILS_NOT_RETAINED
            ),
        )
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
            if (
                outcome.provider == "tushare"
                and outcome.capability in {"get_balance_sheet", "get_income_statement", "get_cashflow"}
                and outcome.reason is AcquisitionUnavailableReason.EMPTY_FRAME
                and outcome.error_code == "current_financial_ineligible_rows"
            ):
                reason = EvidenceDiagnosticCode.FINANCIAL_ROWS_INELIGIBLE
    matched_capabilities = {outcome.capability for outcome in matching}
    # A conflicting tool capability must not override the dispatch-level name.
    if "company_financials" in matched_capabilities and len(matched_capabilities) != 1:
        name, capability = "Unidentified source", "unknown"
    if (
        source.detail != "source ref is not in allowed catalog"
        and len(matched_capabilities) == 1
    ):
        matched_capability = next(iter(matched_capabilities))
        if matched_capability in _ACQUISITION_CAPABILITY_NAMES:
            name, capability = _ACQUISITION_CAPABILITY_NAMES[matched_capability]
    if reason is EvidenceDiagnosticCode.SOURCE_NOT_REGISTERED:
        name, capability = "Unregistered source reference", "unknown"
    return PublicSourceDiagnostic(
        display_name=name,
        capability=capability,
        reason=reason,
        acquisition_reason=acquisition_reason,
    )


# Exact fixed messages emitted by the deterministic validator. Never copy its
# free text or parse values from arbitrary detail into the publication contract.
_FIXED_CLAIM_REASONS = {
    "indicator name metadata conflicts with the cited indicator": EvidenceDiagnosticCode.INDICATOR_IDENTITY_CONFLICT,
    "indicator name metadata is not bound to one unambiguous observation": EvidenceDiagnosticCode.INDICATOR_BINDING_UNVERIFIED,
    "indicator name metadata is not bound to one unambiguous unit-free observation": EvidenceDiagnosticCode.INDICATOR_BINDING_UNVERIFIED,
    "indicator observation date conflicts with the cited row": EvidenceDiagnosticCode.INDICATOR_DATE_CONFLICT,
    "indicator observation units are not established by the cited row": EvidenceDiagnosticCode.INDICATOR_UNITS_UNVERIFIED,
    "indicator observation value is absent from the dated claim": EvidenceDiagnosticCode.INDICATOR_VALUE_MISSING,
    "latest indicator observation requires a bound latest snapshot row": EvidenceDiagnosticCode.INDICATOR_LATEST_BINDING_MISSING,
    "indicator observation date is invalid": EvidenceDiagnosticCode.INDICATOR_DATE_INVALID,
    "snapshot observation date conflicts with the cited row": EvidenceDiagnosticCode.SNAPSHOT_DATE_CONFLICT,
    "snapshot date is not an explicit observation-date span": EvidenceDiagnosticCode.SNAPSHOT_DATE_UNBOUND,
    "snapshot observation value is absent from the dated claim": EvidenceDiagnosticCode.SNAPSHOT_VALUE_MISSING,
}


def claim_diagnostic(validation) -> EvidenceDiagnosticCode:
    detail = validation.detail
    if detail in _FIXED_CLAIM_REASONS:
        return _FIXED_CLAIM_REASONS[detail]
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
    "indicator_binding_unverified": (
        "Indicator identity is not bound to one unambiguous unit-free observation",
        "指标身份未绑定到唯一明确且不附加单位的观测",
    ),
    "indicator_date_conflict": (
        "Indicator observation date conflicts with the cited row",
        "指标观测日期与引用记录不一致",
    ),
    "indicator_units_unverified": (
        "Indicator units are not established by the cited row",
        "引用记录未确立主张所用的指标单位",
    ),
    "indicator_value_missing": (
        "Dated indicator claim contains no observation value",
        "带日期的指标主张缺少观测数值",
    ),
    "indicator_latest_binding_missing": (
        "Latest indicator claim lacks a bound latest snapshot row",
        "最新指标主张缺少最新快照记录的绑定",
    ),
    "indicator_date_invalid": ("Indicator observation date is invalid", "指标观测日期不合法"),
    "snapshot_date_conflict": (
        "Snapshot observation date conflicts with the cited row",
        "快照观测日期与引用记录不一致",
    ),
    "snapshot_date_unbound": (
        "Snapshot date is not expressed as an explicit observation date",
        "快照中的日期未被明确表述为观测日期",
    ),
    "snapshot_value_missing": (
        "Dated snapshot claim contains no observation value",
        "带日期的快照主张缺少观测数值",
    ),
    "indicator_identity_conflict": (
        "Indicator name or period conflicts with the cited indicator",
        "指标名称或周期与引用指标不一致",
    ),
    "financial_rows_ineligible": (
        "Statement rows were returned, but no period passed eligibility checks; this does not mean an empty upstream table. Missing fields remain unavailable, not zero",
        "财报有返回记录，但没有期间通过资格检查；这不表示上游返回空表。缺失字段仍不可用，不能按零处理",
    ),
    "analyst_output_limit": (
        "Analyst submission exceeded the output limit; truncated output was rejected",
        "分析师提交触及输出上限；截断结果已拒绝",
    ),
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


def finite_cache_read_diagnostics(evidence, *, state=None, config=None):
    """Project retained cache reads without describing a new acquisition."""
    from tradingagents.dataflows.finite_disclosure_output import cache_read_projection
    return [
        {"kind": "finite_cache_read", "tool_name": item["tool_name"],
         "new_physical_requests": 0, "historical_observation_count": len(item["historical_observations"]),
         "strict_pit_eligible": False, "full_announcement_window_coverage": False}
        for item in cache_read_projection(evidence, state, config)
    ]

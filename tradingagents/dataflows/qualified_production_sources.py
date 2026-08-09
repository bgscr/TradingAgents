"""Typed normalization for run-owned qualified production provider facades.

This module never owns routing, retries, cache state, coordinator state, or provider
sessions.  It only performs one facade invocation and projects its safe result into
the existing provider-neutral contracts.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256

import pandas as pd

from tradingagents.capability_routing import MainlandCapability
from tradingagents.dataflows.factor_name_contracts import (
    AdjustmentFactorCandidate,
    IssuerNameEventCandidate,
    ProviderArtifactIdentity,
    ProviderDatasetIdentity,
    ProviderRowArtifactIdentity,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityProviderResponse,
    FinancialCapabilityRoutingRequest,
    FinancialCompanyTypeEvidenceProviderResponse,
    FinancialCompanyTypeEvidenceRoutingRequest,
    FinancialCompanyTypeEvidenceSourceProjectionKind,
    FinancialIndicatorProviderGapRequest,
    FinancialProviderGapRequest,
    FinancialProviderPeriodResponse,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_V1,
    FinancialCapability,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialPeriodCandidate,
    FinancialPeriodIdentity,
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeAcquisitionEvidenceBinding,
)
from tradingagents.dataflows.financial_indicator_metadata import (
    evaluate_financial_indicator_candidates,
)
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestResult
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    RateLimitScope,
)

YFINANCE_STATEMENT_NORMALIZER_VERSION = "yfinance-financial-statements-v1"
AKSHARE_INDICATOR_NORMALIZER_VERSION_V1 = "akshare-financial-indicators-v1"
AKSHARE_INDICATOR_NORMALIZER_VERSION = "akshare-financial-indicators-v2"
YFINANCE_PROFILE_NORMALIZER_VERSION = "yfinance-current-financial-profile-v1"
AKSHARE_FACTOR_NORMALIZER_VERSION = "akshare-adjustment-factors-v1"
YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION = "yfinance-derived-adjustment-factors-v1"
AKSHARE_NAME_NORMALIZER_VERSION = "akshare-issuer-name-events-v1"

_YAHOO_STATEMENT_SURFACES = {
    FinancialStatementType.BALANCE_SHEET: (
        "balance_sheet",
        "quarterly_balance_sheet",
    ),
    FinancialStatementType.INCOME_STATEMENT: (
        "income_stmt",
        "quarterly_income_stmt",
    ),
    FinancialStatementType.CASH_FLOW: (
        "cashflow",
        "quarterly_cashflow",
    ),
}
_YAHOO_STATEMENT_FIELDS: dict[
    FinancialStatementType,
    dict[str, str],
] = {
    FinancialStatementType.BALANCE_SHEET: {
        "accounts payable": "accounts_payable",
        "accounts receivable": "accounts_receivable",
        "cash and cash equivalents": "cash_and_equivalents",
        "cash cash equivalents and short term investments": "cash_and_equivalents",
        "current debt": "short_term_debt",
        "inventory": "inventory",
        "long term debt": "long_term_debt",
        "net ppe": "property_plant_equipment",
        "stockholders equity": "total_equity",
        "total assets": "total_assets",
        "total debt": "long_term_debt",
        "total liabilities net minority interest": "total_liabilities",
    },
    FinancialStatementType.INCOME_STATEMENT: {
        "cost of revenue": "cost_of_revenue",
        "gross profit": "gross_profit",
        "net income": "net_income",
        "net income common stockholders": "net_income",
        "operating expense": "operating_expense",
        "operating income": "operating_profit",
        "pretax income": "profit_before_tax",
        "tax provision": "income_tax_expense",
        "total revenue": "operating_revenue",
    },
    FinancialStatementType.CASH_FLOW: {
        "beginning cash position": "opening_cash",
        "changes in cash": "net_change_in_cash",
        "effect of exchange rate changes": "effect_of_exchange_rates",
        "end cash position": "closing_cash",
        "financing cash flow": "cash_from_financing",
        "free cash flow": "free_cash_flow",
        "investing cash flow": "cash_from_investing",
        "operating cash flow": "cash_from_operations",
    },
}
_YAHOO_STATEMENT_FIELD_PRIORITIES = {
    statement_type: {
        provider_field: priority
        for priority, provider_field in enumerate(field_mapping)
    }
    for statement_type, field_mapping in _YAHOO_STATEMENT_FIELDS.items()
}
_AKSHARE_INDICATOR_FIELDS: dict[str, tuple[FinancialRatioFamily, str, Decimal]] = {
    "净资产收益率(%)": (FinancialRatioFamily.PROFIT, "return_on_equity", Decimal("100")),
    "净资产收益率": (FinancialRatioFamily.PROFIT, "return_on_equity", Decimal("100")),
    "销售净利率(%)": (FinancialRatioFamily.PROFIT, "net_margin", Decimal("100")),
    "销售净利率": (FinancialRatioFamily.PROFIT, "net_margin", Decimal("100")),
    "总资产报酬率(%)": (FinancialRatioFamily.PROFIT, "return_on_assets", Decimal("100")),
    "总资产周转率(次)": (FinancialRatioFamily.OPERATION, "asset_turnover", Decimal("1")),
    "应收账款周转率(次)": (FinancialRatioFamily.OPERATION, "receivables_turnover", Decimal("1")),
    "资产负债率(%)": (FinancialRatioFamily.BALANCE, "debt_to_assets", Decimal("100")),
    "流动比率": (FinancialRatioFamily.BALANCE, "current_ratio", Decimal("1")),
    "速动比率": (FinancialRatioFamily.BALANCE, "quick_ratio", Decimal("1")),
    "净利润增长率(%)": (FinancialRatioFamily.GROWTH, "net_income_growth", Decimal("100")),
    "营业收入增长率(%)": (FinancialRatioFamily.GROWTH, "revenue_growth", Decimal("100")),
    "经营现金净流量对负债比率(%)": (
        FinancialRatioFamily.CASH_FLOW,
        "cash_flow_to_debt",
        Decimal("100"),
    ),
}
_AKSHARE_INDICATOR_FIELD_PRIORITIES = {
    provider_field: priority
    for priority, provider_field in enumerate(_AKSHARE_INDICATOR_FIELDS)
}
_PROFILE_CLASSIFIER_KEYS = frozenset(
    {"industry", "industry_name", "provider_industry", "sector", "sector_name"}
)
_SECRET_MARKERS = (
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("qualified provider observation time is not timezone-aware")
    return value.astimezone(timezone.utc)


def _date_value(value: object) -> date:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError("qualified provider date is missing")
    return parsed.date()


def _decimal_text(value: object) -> str:
    if value is None or pd.isna(value):
        raise ValueError("qualified provider numeric value is missing")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("qualified provider numeric value is malformed") from None
    if not numeric.is_finite():
        raise ValueError("qualified provider numeric value is not finite")
    return format(numeric, "f")


def _retry_after_seconds(error: Exception) -> float | None:
    value = getattr(error, "retry_after_seconds", None)
    if value is None:
        value = getattr(error, "retry_after", None)
    try:
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


def sanitized_provider_failure(provider_id: str, error: Exception) -> PhysicalAttemptFailure:
    """Map an SDK failure to a bounded coordinator outcome without retaining text."""

    type_name = type(error).__name__.casefold()
    message = str(error)[:512].casefold()
    status = getattr(error, "status_code", getattr(error, "status", None))
    status_code = status if isinstance(status, int) and 100 <= status <= 599 else None
    retry_after = _retry_after_seconds(error)
    prefix = "akshare" if provider_id == "akshare" else "yfinance"
    if isinstance(error, TimeoutError) or "timeout" in type_name:
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.TIMEOUT,
            retryable=True,
            error_code=f"{prefix}_timeout",
        )
    if status_code == 429 or any(
        marker in message for marker in ("rate limit", "too many", "频率", "每分钟")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            status_code=status_code,
            error_code=f"{prefix}_rate_limited",
            retry_after_seconds=retry_after,
            rate_limit_scope=RateLimitScope.CAPACITY,
        )
    if status_code in {401, 407} or any(
        marker in message for marker in ("authentication", "unauthorized", "invalid token")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            status_code=status_code,
            error_code=f"{prefix}_authentication",
        )
    if status_code == 403 or any(
        marker in message for marker in ("permission", "forbidden", "access denied", "权限")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            status_code=status_code,
            error_code=f"{prefix}_permission_denied",
        )
    if isinstance(error, (ConnectionError, OSError)) or any(
        marker in type_name for marker in ("connection", "disconnect", "socket")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.DISCONNECT,
            retryable=True,
            error_code=f"{prefix}_disconnect",
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=False,
        status_code=status_code,
        error_code=f"{prefix}_provider_error",
    )


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="provider_response_not_serializable",
        ) from None


def _malformed(code: str) -> PhysicalAttemptFailure:
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
        retryable=False,
        error_code=code,
    )


def _empty(code: str) -> PhysicalAttemptFailure:
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
        retryable=False,
        error_code=code,
    )


def _mainland_symbols(identity: object) -> tuple[str, str, str]:
    symbol = getattr(identity, "symbol", None)
    resolved = resolve_mainland_instrument(symbol) if isinstance(symbol, str) else None
    if resolved is None:
        raise _malformed("provider_instrument_incompatible")
    akshare_market_symbol = (
        f"sh{resolved.akshare_code}"
        if resolved.exchange == "shanghai"
        else f"sz{resolved.akshare_code}"
    )
    return resolved.yahoo_symbol, resolved.akshare_code, akshare_market_symbol


def _frame_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for row in frame.to_dict(orient="records"):
        if not isinstance(row, dict):
            raise ValueError("provider frame row is malformed")
        records.append(row)
    return records


def _profile_hint(value: object) -> str | None:
    normalized = str(value).strip().casefold().replace("&", " ")
    if not normalized:
        return None
    if "insurance" in normalized or "insurer" in normalized or "保险" in normalized:
        return "insurance"
    if any(marker in normalized for marker in ("securities", "broker", "证券")):
        return "securities"
    if "bank" in normalized or "银行" in normalized:
        return "bank"
    if any(marker in normalized for marker in ("manufactur", "industrial", "工业")):
        return "industrial"
    if any(marker in normalized for marker in ("financial service", "finance company")):
        return "financial_services"
    return None


def _safe_descriptive_text(value: object, *, optional: bool = False) -> str | None:
    if value is None or pd.isna(value):
        if optional:
            return None
        raise ValueError("descriptive provider value is missing")
    text = str(value).strip()
    folded = text.casefold()
    if (
        not text
        or len(text) > 256
        or any(marker in folded for marker in _SECRET_MARKERS)
        or "://" in text
        or "?" in text
        or "\\" in text
    ):
        raise ValueError("descriptive provider value is unsafe")
    return text


def acquire_yahoo_statement_payload(
    *,
    facade: object,
    request: FinancialProviderGapRequest,
    now: Callable[[], datetime],
) -> bytes:
    """Execute exactly one annual or quarterly Yahoo statement surface."""

    resolved = resolve_mainland_instrument(request.instrument_identity.symbol)
    if resolved is None:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="yfinance_instrument_incompatible",
        )
    quarterly = bool(
        request.missing_reporting_period_ends
        or request.conflicted_reporting_period_ends
    )
    endpoint_id = _YAHOO_STATEMENT_SURFACES[request.statement_type][int(quarterly)]
    try:
        ticker_factory = facade.Ticker
        if not callable(ticker_factory):
            raise TypeError("Ticker surface is unavailable")
        ticker = ticker_factory(resolved.yahoo_symbol)
        frame = getattr(ticker, endpoint_id)
        if callable(frame):
            frame = frame()
    except Exception as error:
        raise sanitized_provider_failure("yfinance", error) from None
    if not isinstance(frame, pd.DataFrame):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="yfinance_statement_not_frame",
        )
    if frame.empty:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
            retryable=False,
            error_code="yfinance_statement_empty",
        )
    target_periods = {
        *request.missing_annual_period_ends,
        *request.missing_reporting_period_ends,
        *request.conflicted_annual_period_ends,
        *request.conflicted_reporting_period_ends,
    }
    field_mapping = _YAHOO_STATEMENT_FIELDS[request.statement_type]
    field_priorities = _YAHOO_STATEMENT_FIELD_PRIORITIES[request.statement_type]
    records: list[dict[str, object]] = []
    try:
        for column in frame.columns:
            period_end = _date_value(column)
            if period_end not in target_periods:
                continue
            fields_by_normalized: dict[str, tuple[int, dict[str, str]]] = {}
            for provider_field, value in frame[column].items():
                provider_key = str(provider_field).strip().casefold()
                normalized_field = field_mapping.get(provider_key)
                if normalized_field is None or value is None or pd.isna(value):
                    continue
                numeric = _decimal_text(value)
                priority = field_priorities[provider_key]
                existing = fields_by_normalized.get(normalized_field)
                if existing is None or priority < existing[0]:
                    fields_by_normalized[normalized_field] = (
                        priority,
                        {
                            "provider_field": normalized_field,
                            "original_value": numeric,
                            "normalized_field": normalized_field,
                            "normalized_value": numeric,
                        },
                    )
            if fields_by_normalized:
                records.append(
                    {
                        "period_end": period_end.isoformat(),
                        "fields": [
                            item[1]
                            for item in sorted(
                                fields_by_normalized.values(),
                                key=lambda item: item[1]["normalized_field"],
                            )
                        ],
                    }
                )
    except (TypeError, ValueError, OverflowError):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="yfinance_statement_malformed",
        ) from None
    if not records:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
            retryable=False,
            error_code="yfinance_statement_no_target_rows",
        )
    observed_at = _utc(now())
    return _json_bytes(
        {
            "endpoint_id": endpoint_id,
            "frequency": "quarterly" if quarterly else "annual",
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "records": records,
            "schema_identity": YFINANCE_STATEMENT_NORMALIZER_VERSION,
            "statement_type": request.statement_type.value,
        }
    )


def decode_yahoo_statement_response(
    request: FinancialProviderGapRequest,
    payload: bytes,
    result: ProviderSubrequestResult,
) -> FinancialProviderPeriodResponse:
    """Project safe Yahoo rows without inventing publication or revision metadata."""

    try:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or not isinstance(decoded.get("records"), list):
            raise ValueError("Yahoo statement payload is malformed")
        observed_at = _utc(datetime.fromisoformat(str(decoded["observed_at"]).replace("Z", "+00:00")))
        dataset = FinancialProviderDatasetIdentity.create(
            provider_id="yfinance",
            endpoint_id=str(decoded["endpoint_id"]),
            dataset_id="current_financials",
            schema_identity=str(decoded["schema_identity"]),
        )
        artifact = FinancialProviderArtifactIdentity.create(
            dataset=dataset,
            canonical_request=result.key.model_dump(mode="json"),
            provider_metadata={
                "provider_id": "yfinance",
                "endpoint_id": decoded["endpoint_id"],
                "statement_type": decoded["statement_type"],
                "frequency": decoded["frequency"],
                "pit_limit": "publication_metadata_unavailable",
            },
            retrieved_at=observed_at,
            observed_at=observed_at,
            payload=decoded["records"],
        )
        frequency = FinancialReportingFrequency(str(decoded["frequency"]))
        candidates: list[FinancialPeriodCandidate] = []
        for record in decoded["records"]:
            if not isinstance(record, dict) or not isinstance(record.get("fields"), list):
                raise ValueError("Yahoo statement record is malformed")
            fields = tuple(
                FinancialFieldValue(
                    **field,
                    original_unit="CNY_BASE_UNIT",
                    normalized_unit="CNY_BASE_UNIT",
                )
                for field in record["fields"]
            )
            resolution = resolve_financial_company_type(
                provider_declared_type=None,
                provider_declaration_qualified=False,
                classifier_metadata={},
                present_fields=tuple(field.normalized_field for field in fields),
            )
            period_identity = FinancialPeriodIdentity.create(
                instrument_identity=request.instrument_identity,
                capability=FinancialCapability.STATEMENT,
                statement_type=request.statement_type,
                ratio_family=None,
                period_end=date.fromisoformat(str(record["period_end"])),
                frequency=frequency,
                company_type=resolution.company_type,
                consolidation_scope=FinancialConsolidationScope.UNKNOWN,
                currency=request.instrument_identity.currency,
                provider_revision_binding=artifact.artifact_identity,
            )
            candidates.append(
                FinancialPeriodCandidate.create(
                    artifact=artifact,
                    period_identity=period_identity,
                    filing_metadata=FinancialFilingMetadata(
                        ann_date=None,
                        f_ann_date=None,
                        report_type=None,
                        comp_type=None,
                        update_flag=None,
                        retrieved_at=observed_at,
                        observed_at=observed_at,
                        local_provider_revision_identity=artifact.artifact_identity,
                    ),
                    company_type_resolution=resolution,
                    fields=fields,
                )
            )
        return FinancialProviderPeriodResponse.available(
            provider_id="yfinance",
            response_artifact=artifact,
            retained_artifacts=(artifact,),
            candidates=candidates,
            subrequest_keys=(),
            physical_attempt_ids=(),
        )
    except (KeyError, TypeError, ValueError):
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="yfinance_statement_decode_malformed",
        ) from None


def acquire_akshare_indicator_payload(
    *,
    facade: object,
    request: FinancialIndicatorProviderGapRequest,
    now: Callable[[], datetime],
) -> bytes:
    """Acquire AKShare ratios once; this surface never accepts statement requests."""

    _yahoo_symbol, akshare_code, _market_symbol = _mainland_symbols(
        request.instrument_identity
    )
    try:
        method = facade.stock_financial_analysis_indicator
        if not callable(method):
            raise TypeError("AKShare indicator surface is unavailable")
        frame = method(symbol=akshare_code)
    except Exception as error:
        raise sanitized_provider_failure("akshare", error) from None
    if not isinstance(frame, pd.DataFrame):
        raise _malformed("akshare_indicator_not_frame")
    if frame.empty:
        raise _empty("akshare_indicator_empty")
    targets = {
        (family, period)
        for family, periods in request.missing_periods_by_family
        for period in periods
    }
    records: list[dict[str, object]] = []
    try:
        for row in _frame_records(frame):
            raw_date = next(
                (row[key] for key in ("日期", "报告期", "date", "报告日期") if key in row),
                None,
            )
            period_end = _date_value(raw_date)
            fields_by_target: dict[
                tuple[FinancialRatioFamily, str],
                tuple[int, dict[str, str]],
            ] = {}
            for provider_field, value in row.items():
                provider_key = str(provider_field).strip()
                mapping = _AKSHARE_INDICATOR_FIELDS.get(provider_key)
                if mapping is None or value is None or pd.isna(value):
                    continue
                family, normalized_field, divisor = mapping
                if (family, period_end) not in targets:
                    continue
                original = Decimal(_decimal_text(value))
                normalized = original / divisor
                target = (family, normalized_field)
                priority = _AKSHARE_INDICATOR_FIELD_PRIORITIES[provider_key]
                existing = fields_by_target.get(target)
                if existing is None or priority < existing[0]:
                    fields_by_target[target] = (
                        priority,
                        {
                            "family": family.value,
                            "provider_field": normalized_field,
                            "original_value": format(original, "f"),
                            "original_unit": (
                                "PERCENT" if divisor == 100 else "RATIO"
                            ),
                            "normalized_field": normalized_field,
                            "normalized_value": format(normalized, "f"),
                            "normalized_unit": "RATIO",
                        },
                    )
            if fields_by_target:
                records.append(
                    {
                        "period_end": period_end.isoformat(),
                        "fields": [
                            item[1]
                            for item in sorted(
                                fields_by_target.values(),
                                key=lambda item: (
                                    item[1]["family"],
                                    item[1]["normalized_field"],
                                ),
                            )
                        ],
                    }
                )
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise _malformed("akshare_indicator_malformed") from None
    if not records:
        raise _empty("akshare_indicator_no_target_rows")
    observed_at = _utc(now())
    return _json_bytes(
        {
            "endpoint_id": "stock_financial_analysis_indicator",
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "records": records,
            "schema_identity": AKSHARE_INDICATOR_NORMALIZER_VERSION,
        }
    )


def decode_akshare_indicator_response(
    request: FinancialIndicatorProviderGapRequest,
    payload: bytes,
    result: ProviderSubrequestResult,
) -> FinancialProviderPeriodResponse:
    try:
        decoded = json.loads(payload)
        records = decoded["records"]
        if not isinstance(records, list) or request.company_type_resolution is None:
            raise ValueError("AKShare indicator metadata is incompatible")
        observed_at = _utc(
            datetime.fromisoformat(str(decoded["observed_at"]).replace("Z", "+00:00"))
        )
        dataset = FinancialProviderDatasetIdentity.create(
            provider_id="akshare",
            endpoint_id=str(decoded["endpoint_id"]),
            dataset_id="financial_analysis_indicators",
            schema_identity=str(decoded["schema_identity"]),
        )
        artifact = FinancialProviderArtifactIdentity.create(
            dataset=dataset,
            canonical_request=result.key.model_dump(mode="json"),
            provider_metadata={
                "provider_id": "akshare",
                "endpoint_id": decoded["endpoint_id"],
                "pit_limit": "publication_metadata_unavailable",
                "statement_capability": False,
            },
            retrieved_at=observed_at,
            observed_at=observed_at,
            payload=records,
        )
        candidates: list[FinancialPeriodCandidate] = []
        schema_identity = str(decoded["schema_identity"])
        if schema_identity not in {
            AKSHARE_INDICATOR_NORMALIZER_VERSION_V1,
            AKSHARE_INDICATOR_NORMALIZER_VERSION,
        }:
            raise ValueError("AKShare indicator schema identity is unsupported")
        truthful_metadata = schema_identity == AKSHARE_INDICATOR_NORMALIZER_VERSION
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("fields"), list):
                raise ValueError("AKShare indicator row is malformed")
            by_family: dict[FinancialRatioFamily, list[FinancialFieldValue]] = {}
            for raw_field in record["fields"]:
                if not isinstance(raw_field, dict):
                    raise ValueError("AKShare indicator field is malformed")
                field_payload = dict(raw_field)
                family = FinancialRatioFamily(field_payload.pop("family"))
                by_family.setdefault(family, []).append(
                    FinancialFieldValue(**field_payload)
                )
            for family in FinancialRatioFamily:
                fields = by_family.get(family)
                if not fields:
                    continue
                period_identity = FinancialPeriodIdentity.create(
                    instrument_identity=request.instrument_identity,
                    capability=FinancialCapability.RATIO_FAMILY,
                    statement_type=None,
                    ratio_family=family,
                    period_end=date.fromisoformat(str(record["period_end"])),
                    frequency=FinancialReportingFrequency.QUARTERLY,
                    company_type=request.company_type,
                    consolidation_scope=(
                        FinancialConsolidationScope.UNKNOWN
                        if truthful_metadata
                        else request.consolidation_scope
                    ),
                    currency="XXX" if truthful_metadata else request.currency,
                    provider_revision_binding=artifact.artifact_identity,
                )
                candidates.append(
                    FinancialPeriodCandidate.create(
                        artifact=artifact,
                        period_identity=period_identity,
                        filing_metadata=FinancialFilingMetadata(
                            ann_date=None,
                            f_ann_date=None,
                            report_type=None,
                            comp_type=None,
                            update_flag=None,
                            retrieved_at=observed_at,
                            observed_at=observed_at,
                            local_provider_revision_identity=artifact.artifact_identity,
                        ),
                        company_type_resolution=request.company_type_resolution,
                        fields=fields,
                    )
                )
        if not candidates:
            raise ValueError("AKShare indicator response has no candidates")
        metadata_compatibility = (
            evaluate_financial_indicator_candidates(
                candidates=candidates,
                instrument_identity=request.instrument_identity,
                requested_value_currency=request.currency,
                requested_filing_consolidation_scope=(
                    request.consolidation_scope
                ),
                pit_as_of_date=request.as_of_date,
                declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
                normalizer_contract_version=(
                    AKSHARE_INDICATOR_NORMALIZER_VERSION
                ),
            )
            if truthful_metadata
            else ()
        )
        return FinancialProviderPeriodResponse.available(
            provider_id="akshare",
            response_artifact=artifact,
            retained_artifacts=(artifact,),
            candidates=candidates,
            subrequest_keys=(),
            physical_attempt_ids=(),
            indicator_metadata_compatibility=metadata_compatibility,
        )
    except (KeyError, TypeError, ValueError):
        raise _malformed("akshare_indicator_decode_malformed") from None


def acquire_yahoo_profile_payload(
    *,
    facade: object,
    request: object,
    now: Callable[[], datetime],
) -> bytes:
    identity = (
        request.instrument_identity
        if isinstance(request, FinancialIndicatorProviderGapRequest)
        else request.metadata_binding.instrument_identity
    )
    yahoo_symbol, _akshare_code, _market_symbol = _mainland_symbols(identity)
    try:
        ticker_factory = facade.Ticker
        if not callable(ticker_factory):
            raise TypeError("Ticker surface is unavailable")
        ticker = ticker_factory(yahoo_symbol)
        get_info = getattr(ticker, "get_info", None)
        if callable(get_info):
            endpoint_id = "get_info"
            info = get_info()
        else:
            endpoint_id = "info"
            info = ticker.info
    except Exception as error:
        raise sanitized_provider_failure("yfinance", error) from None
    if not isinstance(info, Mapping):
        raise _malformed("yfinance_profile_not_mapping")
    classifier_inputs = {
        str(key).strip().casefold(): hint
        for key, value in info.items()
        if str(key).strip().casefold() in _PROFILE_CLASSIFIER_KEYS
        and (hint := _profile_hint(value)) is not None
    }
    if not classifier_inputs:
        raise _empty("yfinance_profile_no_classifier_inputs")
    observed_at = _utc(now())
    return _json_bytes(
        {
            "classifier_inputs": classifier_inputs,
            "endpoint_id": endpoint_id,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "schema_identity": YFINANCE_PROFILE_NORMALIZER_VERSION,
        }
    )


def _yahoo_profile_artifact(
    *,
    payload: bytes,
    result: ProviderSubrequestResult,
) -> tuple[dict[str, object], FinancialProviderArtifactIdentity, datetime]:
    decoded = json.loads(payload)
    classifier_inputs = decoded["classifier_inputs"]
    if not isinstance(classifier_inputs, dict) or not classifier_inputs:
        raise ValueError("Yahoo profile classifier inputs are malformed")
    if not set(classifier_inputs) <= _PROFILE_CLASSIFIER_KEYS:
        raise ValueError("Yahoo profile classifier inputs are not whitelisted")
    observed_at = _utc(
        datetime.fromisoformat(str(decoded["observed_at"]).replace("Z", "+00:00"))
    )
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id="yfinance",
        endpoint_id=str(decoded["endpoint_id"]),
        dataset_id="current_financial_profile",
        schema_identity=str(decoded["schema_identity"]),
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request=result.key.model_dump(mode="json"),
        provider_metadata={
            "provider_id": "yfinance",
            "endpoint_id": decoded["endpoint_id"],
            "authority": "classifier_only_current_profile",
        },
        retrieved_at=observed_at,
        observed_at=observed_at,
        payload=classifier_inputs,
    )
    return decoded, artifact, observed_at


def decode_yahoo_profile_response(
    request: FinancialIndicatorProviderGapRequest,
    payload: bytes,
    result: ProviderSubrequestResult,
) -> FinancialProviderPeriodResponse:
    try:
        _decoded, artifact, _observed_at = _yahoo_profile_artifact(
            payload=payload,
            result=result,
        )
        metadata_compatibility = evaluate_financial_indicator_candidates(
            candidates=(),
            instrument_identity=request.instrument_identity,
            requested_value_currency=request.currency,
            requested_filing_consolidation_scope=request.consolidation_scope,
            pit_as_of_date=request.as_of_date,
            declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
            normalizer_contract_version=YFINANCE_PROFILE_NORMALIZER_VERSION,
        )
        return FinancialProviderPeriodResponse.available(
            provider_id="yfinance",
            response_artifact=artifact,
            retained_artifacts=(artifact,),
            candidates=(),
            subrequest_keys=(),
            physical_attempt_ids=(),
            degraded_current_profile=True,
            indicator_metadata_compatibility=metadata_compatibility,
        )
    except (KeyError, TypeError, ValueError):
        raise _malformed("yfinance_profile_decode_malformed") from None


def decode_yahoo_company_type_evidence_response(
    request: FinancialCompanyTypeEvidenceRoutingRequest,
    payload: bytes,
    result: ProviderSubrequestResult,
) -> FinancialCompanyTypeEvidenceProviderResponse:
    """Project only whitelisted classifier evidence from Yahoo's current profile."""

    try:
        decoded, artifact, observed_at = _yahoo_profile_artifact(
            payload=payload,
            result=result,
        )
        classifier_inputs = decoded["classifier_inputs"]
        if not isinstance(classifier_inputs, dict):
            raise ValueError("Yahoo profile classifier inputs are malformed")
        resolution = resolve_financial_company_type(
            provider_declared_type=None,
            provider_declaration_qualified=False,
            classifier_metadata=classifier_inputs,
            present_fields=(),
        )
        evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
            instrument_identity=request.metadata_binding.instrument_identity,
            as_of_date=request.metadata_binding.as_of_date,
            provider_id="yfinance",
            artifact_identity=artifact.artifact_identity,
            source_ref=artifact.artifact_identity,
            observed_at=observed_at,
            retrieved_at=observed_at,
            provider_declared_type=None,
            provider_declaration_qualified=False,
            normalized_resolver_inputs=resolution.classifier_inputs,
        )
        return FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="yfinance",
            metadata_binding=request.metadata_binding,
            source_projection_kind=(
                FinancialCompanyTypeEvidenceSourceProjectionKind.YFINANCE_CURRENT_PROFILE
            ),
            evidence=evidence,
            subrequest_keys=(),
            physical_attempt_ids=(),
        )
    except (KeyError, TypeError, ValueError):
        raise _malformed("yfinance_company_type_evidence_malformed") from None


def acquire_akshare_factor_payload(
    *,
    facade: object,
    request: FinancialCapabilityRoutingRequest,
    now: Callable[[], datetime],
) -> bytes:
    _yahoo_symbol, _akshare_code, market_symbol = _mainland_symbols(
        request.instrument_identity
    )
    try:
        method = facade.stock_zh_a_daily
        if not callable(method):
            raise TypeError("AKShare factor surface is unavailable")
        frame = method(
            symbol=market_symbol,
            start_date=request.range_start.strftime("%Y%m%d"),
            end_date=request.as_of_date.strftime("%Y%m%d"),
            adjust="qfq-factor",
        )
    except Exception as error:
        raise sanitized_provider_failure("akshare", error) from None
    if not isinstance(frame, pd.DataFrame):
        raise _malformed("akshare_factor_not_frame")
    if frame.empty:
        raise _empty("akshare_factor_empty")
    rows: list[dict[str, str]] = []
    try:
        for row in _frame_records(frame):
            raw_date = next(
                (row[key] for key in ("date", "日期", "trade_date") if key in row),
                None,
            )
            raw_factor = next(
                (
                    row[key]
                    for key in ("qfq_factor", "factor", "前复权因子", "adjust_factor")
                    if key in row
                ),
                None,
            )
            trade_date = _date_value(raw_date)
            factor = Decimal(_decimal_text(raw_factor))
            if not request.range_start <= trade_date <= request.as_of_date:
                continue
            if not factor.is_finite() or factor <= 0:
                raise ValueError("AKShare factor is invalid")
            rows.append(
                {
                    "trade_date": trade_date.isoformat(),
                    "adj_factor": format(factor, "f"),
                }
            )
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise _malformed("akshare_factor_malformed") from None
    if not rows:
        raise _empty("akshare_factor_no_target_rows")
    observed_at = _utc(now())
    return _json_bytes(
        {
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "rows": rows,
            "schema_identity": AKSHARE_FACTOR_NORMALIZER_VERSION,
        }
    )


def acquire_yahoo_derived_factor_payload(
    *,
    facade: object,
    request: FinancialCapabilityRoutingRequest,
    now: Callable[[], datetime],
) -> bytes:
    yahoo_symbol, _akshare_code, _market_symbol = _mainland_symbols(
        request.instrument_identity
    )
    try:
        ticker_factory = facade.Ticker
        if not callable(ticker_factory):
            raise TypeError("Ticker surface is unavailable")
        ticker = ticker_factory(yahoo_symbol)
        history = ticker.history
        if not callable(history):
            raise TypeError("Yahoo history surface is unavailable")
        frame = history(
            auto_adjust=False,
            start=request.range_start.isoformat(),
            end=(request.as_of_date + timedelta(days=1)).isoformat(),
        )
    except Exception as error:
        raise sanitized_provider_failure("yfinance", error) from None
    if not isinstance(frame, pd.DataFrame):
        raise _malformed("yfinance_history_not_frame")
    if frame.empty:
        raise _empty("yfinance_history_empty")
    if not {"Close", "Adj Close"} <= set(frame.columns):
        raise _malformed("yfinance_history_columns_missing")
    rows: list[dict[str, str]] = []
    try:
        for raw_date, row in frame.iterrows():
            trade_date = _date_value(raw_date)
            if not request.range_start <= trade_date <= request.as_of_date:
                continue
            close = Decimal(_decimal_text(row["Close"]))
            adjusted_close = Decimal(_decimal_text(row["Adj Close"]))
            if close == 0:
                raise ValueError("Yahoo Close is zero")
            factor = adjusted_close / close
            if not factor.is_finite() or factor <= 0:
                raise ValueError("Yahoo derived factor is invalid")
            rows.append(
                {
                    "trade_date": trade_date.isoformat(),
                    "derived_factor": format(factor, "f"),
                }
            )
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise _malformed("yfinance_derived_factor_invalid") from None
    if not rows:
        raise _empty("yfinance_derived_factor_no_target_rows")
    observed_at = _utc(now())
    return _json_bytes(
        {
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "rows": rows,
            "schema_identity": YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION,
        }
    )


def _factor_artifact(
    *,
    provider_id: str,
    endpoint_id: str,
    dataset_id: str,
    normalizer_version: str,
    qualification_profile: str,
    decoded: Mapping[str, object],
    result: ProviderSubrequestResult,
) -> ProviderArtifactIdentity:
    if result.artifact is None:
        raise ValueError("provider factor artifact is missing")
    rows = decoded["rows"]
    if not isinstance(rows, list):
        raise ValueError("provider factor rows are malformed")
    observed_at = _utc(
        datetime.fromisoformat(str(decoded["observed_at"]).replace("Z", "+00:00"))
    )
    return ProviderArtifactIdentity.create(
        dataset=ProviderDatasetIdentity.create(
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            dataset_id=dataset_id,
            schema_identity=str(decoded["schema_identity"]),
        ),
        subrequest_key=result.key.subrequest_key,
        raw_artifact=result.artifact,
        payload=rows,
        retrieved_at=observed_at,
        observed_at=observed_at,
        qualification_profile=qualification_profile,
        normalizer_version=normalizer_version,
    )


def _row_identity(
    artifact: ProviderArtifactIdentity,
    row: Mapping[str, object],
    occurrence: int,
) -> ProviderRowArtifactIdentity:
    encoded = json.dumps(
        row,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ProviderRowArtifactIdentity.create(
        response_artifact_identity=artifact.artifact_identity,
        row_payload_sha256=sha256(encoded).hexdigest(),
        duplicate_occurrence=occurrence,
    )


def decode_akshare_factor_response(
    request: FinancialCapabilityRoutingRequest,
    payload: bytes,
    result: ProviderSubrequestResult,
    *,
    qualification_profile: str,
) -> FinancialCapabilityProviderResponse:
    try:
        decoded = json.loads(payload)
        artifact = _factor_artifact(
            provider_id="akshare",
            endpoint_id="stock_zh_a_daily",
            dataset_id="qfq_adjustment_factors",
            normalizer_version=AKSHARE_FACTOR_NORMALIZER_VERSION,
            qualification_profile=qualification_profile,
            decoded=decoded,
            result=result,
        )
        candidates: list[AdjustmentFactorCandidate] = []
        seen: dict[str, int] = {}
        for row in decoded["rows"]:
            canonical = json.dumps(row, ensure_ascii=False, sort_keys=True)
            occurrence = seen.get(canonical, 0)
            seen[canonical] = occurrence + 1
            row_identity = _row_identity(artifact, row, occurrence)
            candidates.append(
                AdjustmentFactorCandidate.create(
                    instrument_identity=request.instrument_identity,
                    trade_date=date.fromisoformat(str(row["trade_date"])),
                    adj_factor=Decimal(str(row["adj_factor"])),
                    original_adj_factor=str(row["adj_factor"]),
                    artifact=artifact,
                    occurrence_artifact_identities=(
                        row_identity.row_artifact_identity,
                    ),
                )
            )
        return FinancialCapabilityProviderResponse.available(
            provider_id="akshare",
            capability=MainlandCapability.ADJUSTMENT_FACTORS,
            retained_artifact_identities=(artifact.artifact_identity,),
            candidate_count=len(candidates),
            subrequest_keys=(),
            physical_attempt_ids=(),
        )
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise _malformed("akshare_factor_decode_malformed") from None


def decode_yahoo_derived_factor_response(
    request: FinancialCapabilityRoutingRequest,
    payload: bytes,
    result: ProviderSubrequestResult,
    *,
    qualification_profile: str,
) -> FinancialCapabilityProviderResponse:
    try:
        decoded = json.loads(payload)
        artifact = _factor_artifact(
            provider_id="yfinance_derived",
            endpoint_id="history",
            dataset_id="derived_adjustment_factors",
            normalizer_version=YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION,
            qualification_profile=qualification_profile,
            decoded=decoded,
            result=result,
        )
        rows = decoded["rows"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("Yahoo derived factor rows are malformed")
        for row in rows:
            factor = Decimal(str(row["derived_factor"]))
            if not factor.is_finite() or factor <= 0:
                raise ValueError("Yahoo derived factor is invalid")
            date.fromisoformat(str(row["trade_date"]))
        return FinancialCapabilityProviderResponse.available(
            provider_id="yfinance_derived",
            capability=MainlandCapability.ADJUSTMENT_FACTORS,
            retained_artifact_identities=(artifact.artifact_identity,),
            candidate_count=len(rows),
            subrequest_keys=(),
            physical_attempt_ids=(),
            derived=True,
            degraded=True,
        )
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise _malformed("yfinance_derived_factor_decode_malformed") from None


def acquire_akshare_name_payload(
    *,
    facade: object,
    request: FinancialCapabilityRoutingRequest,
    now: Callable[[], datetime],
) -> bytes:
    _yahoo_symbol, akshare_code, _market_symbol = _mainland_symbols(
        request.instrument_identity
    )
    try:
        method = facade.stock_info_change_name
        if not callable(method):
            raise TypeError("AKShare name surface is unavailable")
        frame = method(symbol=akshare_code)
    except Exception as error:
        raise sanitized_provider_failure("akshare", error) from None
    if not isinstance(frame, pd.DataFrame):
        raise _malformed("akshare_name_not_frame")
    if frame.empty:
        raise _empty("akshare_name_empty")
    rows: list[dict[str, object]] = []
    try:
        for row in _frame_records(frame):
            raw_start = next(
                (
                    row[key]
                    for key in ("变更日期", "起始日期", "开始日期", "start_date", "date")
                    if key in row
                ),
                None,
            )
            start_date = _date_value(raw_start)
            if not request.range_start <= start_date <= request.as_of_date:
                continue
            raw_end = next(
                (row[key] for key in ("截止日期", "结束日期", "end_date") if key in row),
                None,
            )
            end_date = (
                None
                if raw_end is None or pd.isna(raw_end) or not str(raw_end).strip()
                else _date_value(raw_end)
            )
            if end_date is not None and end_date < start_date:
                raise ValueError("AKShare name interval is reversed")
            raw_name = next(
                (row[key] for key in ("证券简称", "名称", "name") if key in row),
                None,
            )
            raw_reason = next(
                (row[key] for key in ("变更原因", "change_reason", "原因") if key in row),
                None,
            )
            rows.append(
                {
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat() if end_date is not None else None,
                    "name": _safe_descriptive_text(raw_name),
                    "change_reason": _safe_descriptive_text(raw_reason, optional=True),
                }
            )
    except (TypeError, ValueError, OverflowError):
        raise _malformed("akshare_name_malformed") from None
    if not rows:
        raise _empty("akshare_name_no_target_rows")
    observed_at = _utc(now())
    return _json_bytes(
        {
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "rows": rows,
            "schema_identity": AKSHARE_NAME_NORMALIZER_VERSION,
        }
    )


def decode_akshare_name_response(
    request: FinancialCapabilityRoutingRequest,
    payload: bytes,
    result: ProviderSubrequestResult,
    *,
    qualification_profile: str,
) -> FinancialCapabilityProviderResponse:
    try:
        decoded = json.loads(payload)
        artifact = _factor_artifact(
            provider_id="akshare",
            endpoint_id="stock_info_change_name",
            dataset_id="issuer_name_events",
            normalizer_version=AKSHARE_NAME_NORMALIZER_VERSION,
            qualification_profile=qualification_profile,
            decoded=decoded,
            result=result,
        )
        candidates: list[IssuerNameEventCandidate] = []
        seen: dict[str, int] = {}
        for row in decoded["rows"]:
            canonical = json.dumps(row, ensure_ascii=False, sort_keys=True)
            occurrence = seen.get(canonical, 0)
            seen[canonical] = occurrence + 1
            row_identity = _row_identity(artifact, row, occurrence)
            candidates.append(
                IssuerNameEventCandidate.create(
                    instrument_identity=request.instrument_identity,
                    start_date=date.fromisoformat(str(row["start_date"])),
                    end_date=(
                        date.fromisoformat(str(row["end_date"]))
                        if row.get("end_date") is not None
                        else None
                    ),
                    name=str(row["name"]),
                    change_reason=(
                        str(row["change_reason"])
                        if row.get("change_reason") is not None
                        else None
                    ),
                    artifact=artifact,
                    row_artifact_identity=row_identity.row_artifact_identity,
                )
            )
        return FinancialCapabilityProviderResponse.available(
            provider_id="akshare",
            capability=MainlandCapability.NAME_EVENTS,
            retained_artifact_identities=(artifact.artifact_identity,),
            candidate_count=len(candidates),
            subrequest_keys=(),
            physical_attempt_ids=(),
        )
    except (KeyError, TypeError, ValueError):
        raise _malformed("akshare_name_decode_malformed") from None


__all__ = [
    "AKSHARE_FACTOR_NORMALIZER_VERSION",
    "AKSHARE_INDICATOR_NORMALIZER_VERSION",
    "AKSHARE_INDICATOR_NORMALIZER_VERSION_V1",
    "AKSHARE_NAME_NORMALIZER_VERSION",
    "YFINANCE_STATEMENT_NORMALIZER_VERSION",
    "YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION",
    "YFINANCE_PROFILE_NORMALIZER_VERSION",
    "acquire_akshare_factor_payload",
    "acquire_akshare_indicator_payload",
    "acquire_akshare_name_payload",
    "acquire_yahoo_derived_factor_payload",
    "acquire_yahoo_profile_payload",
    "acquire_yahoo_statement_payload",
    "decode_akshare_factor_response",
    "decode_akshare_indicator_response",
    "decode_akshare_name_response",
    "decode_yahoo_derived_factor_response",
    "decode_yahoo_company_type_evidence_response",
    "decode_yahoo_profile_response",
    "decode_yahoo_statement_response",
    "sanitized_provider_failure",
]

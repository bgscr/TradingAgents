"""Provider-neutral projection for immutable Tushare indicator artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_V2,
    FinancialCapability,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialPeriodCandidate,
    FinancialPeriodCompletenessAssessment,
    FinancialPeriodIdentity,
    FinancialPeriodRejectionReason,
    FinancialProviderArtifactIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    assess_financial_period_candidate,
)
from tradingagents.evidence import InstrumentIdentityEvidence

TUSHARE_FINANCIAL_INDICATOR_ADAPTER_VERSION = (
    "tushare-financial-indicator-adapter-v1"
)

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_QUARTER_ENDS = frozenset({(3, 31), (6, 30), (9, 30), (12, 31)})
_CURRENT_ONLY_REASONS = frozenset(
    {
        FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE,
        FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED,
    }
)


@dataclass(frozen=True)
class _SourceField:
    provider_field: str
    original_unit: str
    divisor: Decimal = Decimal(1)


@dataclass(frozen=True)
class _NormalizedField:
    normalized_field: str
    sources: tuple[_SourceField, ...]


class _IncompatibleUnitError(ValueError):
    pass


def _field(
    normalized_field: str,
    *sources: tuple[str, str, str],
) -> _NormalizedField:
    return _NormalizedField(
        normalized_field=normalized_field,
        sources=tuple(
            _SourceField(
                provider_field=provider_field,
                original_unit=original_unit,
                divisor=Decimal(divisor),
            )
            for provider_field, original_unit, divisor in sources
        ),
    )


_NON_BANK_FIELDS: dict[FinancialRatioFamily, tuple[_NormalizedField, ...]] = {
    FinancialRatioFamily.PROFIT: (
        _field("ebitda_margin", ("ebitda_margin", "PERCENT", "100")),
        _field("gross_margin", ("grossprofit_margin", "PERCENT", "100")),
        _field("net_margin", ("netprofit_margin", "PERCENT", "100")),
        _field("operating_margin", ("op_of_gr", "PERCENT", "100")),
        _field("pre_tax_margin", ("profit_to_gr", "PERCENT", "100")),
        _field("profit_to_cost", ("profit_to_op", "PERCENT", "100")),
        _field("return_on_assets", ("roa", "PERCENT", "100")),
        _field("return_on_equity", ("roe", "PERCENT", "100")),
        _field("return_on_invested_capital", ("roic", "PERCENT", "100")),
    ),
    FinancialRatioFamily.BALANCE: (
        _field("cash_ratio", ("cash_ratio", "RATIO", "1")),
        _field("current_ratio", ("current_ratio", "RATIO", "1")),
        _field("debt_to_assets", ("debt_to_assets", "PERCENT", "100")),
        _field("debt_to_equity", ("debt_to_eqt", "RATIO", "1")),
        _field("equity_multiplier", ("assets_to_eqt", "RATIO", "1")),
        _field("interest_coverage", ("ebit_to_interest", "RATIO", "1")),
        _field("long_term_debt_ratio", ("longdeb_to_debt", "PERCENT", "100")),
        _field("quick_ratio", ("quick_ratio", "RATIO", "1")),
        _field(
            "tangible_asset_ratio",
            ("tbassets_to_totalassets", "PERCENT", "100"),
        ),
        _field(
            "working_capital_ratio",
            ("working_capital_ratio", "RATIO", "1"),
        ),
    ),
    FinancialRatioFamily.OPERATION: (
        _field("accounts_payable_turnover", ("ap_turn", "RATIO", "1")),
        _field("asset_turnover", ("assets_turn", "RATIO", "1")),
        _field("current_asset_turnover", ("ca_turn", "RATIO", "1")),
        _field("fixed_asset_turnover", ("fa_turn", "RATIO", "1")),
        _field("inventory_turnover", ("inv_turn", "RATIO", "1")),
        _field("receivables_turnover", ("ar_turn", "RATIO", "1")),
    ),
    FinancialRatioFamily.GROWTH: (
        _field("asset_growth", ("assets_yoy", "PERCENT", "100")),
        _field("cash_flow_growth", ("ocf_yoy", "PERCENT", "100")),
        _field(
            "equity_growth",
            ("eqt_yoy", "PERCENT", "100"),
            ("equity_yoy", "PERCENT", "100"),
        ),
        _field("gross_profit_growth", ("grossprofit_yoy", "PERCENT", "100")),
        _field("net_income_growth", ("netprofit_yoy", "PERCENT", "100")),
        _field("operating_profit_growth", ("op_yoy", "PERCENT", "100")),
        _field(
            "revenue_growth",
            ("tr_yoy", "PERCENT", "100"),
            ("or_yoy", "PERCENT", "100"),
        ),
        _field("return_on_equity_growth", ("roe_yoy", "PERCENT", "100")),
        _field("total_profit_growth", ("ebt_yoy", "PERCENT", "100")),
        _field(
            "working_capital_growth",
            ("working_capital_yoy", "PERCENT", "100"),
        ),
    ),
    FinancialRatioFamily.CASH_FLOW: (
        _field("cash_flow_adequacy", ("cash_flow_adequacy", "RATIO", "1")),
        _field("cash_flow_to_debt", ("ocf_to_debt", "RATIO", "1")),
        _field(
            "cash_flow_to_liabilities",
            ("ocf_to_liabilities", "RATIO", "1"),
        ),
        _field("cash_flow_to_revenue", ("ocf_to_or", "RATIO", "1")),
        _field(
            "cash_return_on_assets",
            ("cash_return_on_assets", "RATIO", "1"),
        ),
        _field("free_cash_flow_margin", ("fcff_to_or", "RATIO", "1")),
        _field(
            "operating_cash_flow_ratio",
            ("ocf_to_opincome", "RATIO", "1"),
        ),
        _field("quality_of_income", ("ocf_to_profit", "RATIO", "1")),
        _field("reinvestment_ratio", ("reinvestment_ratio", "RATIO", "1")),
        _field(
            "self_financing_ratio",
            ("self_financing_ratio", "RATIO", "1"),
        ),
    ),
    FinancialRatioFamily.DUPONT: (
        _field(
            "asset_turnover_component",
            ("assets_turn", "RATIO", "1"),
        ),
        _field("debt_burden", ("debt_burden", "RATIO", "1")),
        _field(
            "equity_multiplier_component",
            ("assets_to_eqt", "RATIO", "1"),
        ),
        _field("financial_leverage", ("financial_leverage", "RATIO", "1")),
        _field("interest_burden", ("interest_burden", "RATIO", "1")),
        _field(
            "net_profit_margin_component",
            ("netprofit_margin", "PERCENT", "100"),
        ),
        _field(
            "operating_margin_component",
            ("op_of_gr", "PERCENT", "100"),
        ),
        _field("return_on_assets_component", ("roa", "PERCENT", "100")),
        _field("return_on_equity_component", ("roe", "PERCENT", "100")),
        _field("tax_burden", ("tax_burden", "RATIO", "1")),
    ),
}

_BANK_FIELDS: dict[FinancialRatioFamily, tuple[_NormalizedField, ...]] = {
    **_NON_BANK_FIELDS,
    FinancialRatioFamily.PROFIT: (
        _field("cost_income_ratio", ("cost_income_ratio", "PERCENT", "100")),
        _field("net_interest_margin", ("net_interest_margin", "PERCENT", "100")),
        _field("net_interest_spread", ("net_interest_spread", "PERCENT", "100")),
        _field("net_margin", ("netprofit_margin", "PERCENT", "100")),
        _field(
            "non_performing_loan_ratio",
            ("npl_ratio", "PERCENT", "100"),
        ),
        _field("pre_tax_margin", ("profit_to_gr", "PERCENT", "100")),
        _field(
            "provision_coverage_ratio",
            ("provision_coverage_ratio", "PERCENT", "100"),
        ),
        _field("return_on_assets", ("roa", "PERCENT", "100")),
        _field("return_on_equity", ("roe", "PERCENT", "100")),
        _field("return_on_invested_capital", ("roic", "PERCENT", "100")),
    ),
}

_COMPANY_TYPE_FIELDS = {
    FinancialCompanyType.BANK: _BANK_FIELDS,
    FinancialCompanyType.INDUSTRIAL_NON_BANK: _NON_BANK_FIELDS,
}


class TushareIndicatorRowRejection(BaseModel):
    """Payload-free rejection of one retained row/family projection."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["tushare-financial-indicator-adapter-v1"] = (
        TUSHARE_FINANCIAL_INDICATOR_ADAPTER_VERSION
    )
    row_identity: str = Field(pattern=r"^tushare-indicator-row:v1:[0-9a-f]{64}$")
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    ratio_family: FinancialRatioFamily | None = None
    candidate_identity: str | None = Field(
        default=None,
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$",
    )
    reasons: tuple[FinancialPeriodRejectionReason, ...]

    @model_validator(mode="after")
    def _validate_rejection(self) -> TushareIndicatorRowRejection:
        canonical = tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in self.reasons
        )
        if not canonical or canonical != self.reasons:
            raise ValueError("Tushare indicator rejection reasons must be canonical")
        if self.ratio_family is None and self.candidate_identity is not None:
            raise ValueError("Tushare indicator rejection binding is incomplete")
        return self


def project_indicator_candidates(
    *,
    payload: Mapping[str, object],
    row_artifacts: Sequence[FinancialProviderArtifactIdentity],
    instrument_identity: InstrumentIdentityEvidence,
    company_type_resolution: FinancialCompanyTypeResolution,
    expected_provider_symbol: str,
    pit_as_of_date: date,
) -> tuple[
    tuple[FinancialPeriodCandidate, ...],
    tuple[FinancialPeriodCompletenessAssessment, ...],
    tuple[TushareIndicatorRowRejection, ...],
]:
    """Project independent ratio-family candidates from each retained row."""

    rows = payload["rows"]
    assert isinstance(rows, list)
    if len(rows) != len(row_artifacts):
        raise ValueError("Tushare indicator row artifacts are incomplete")
    mappings = _COMPANY_TYPE_FIELDS.get(company_type_resolution.company_type, {})
    families = tuple(FinancialRatioFamily)
    candidates: list[FinancialPeriodCandidate] = []
    assessments: list[FinancialPeriodCompletenessAssessment] = []
    rejections: list[TushareIndicatorRowRejection] = []
    for row_index, row in enumerate(rows):
        assert isinstance(row, dict)
        artifact = row_artifacts[row_index]
        try:
            if _optional_text(row.get("ts_code")) != expected_provider_symbol:
                raise ValueError("Tushare indicator row symbol does not match request")
            period_end = _provider_date(row.get("end_date"), required=True)
            assert period_end is not None
            if (period_end.month, period_end.day) not in _QUARTER_ENDS:
                raise ValueError("Tushare indicator period is not quarterly")
        except (TypeError, ValueError):
            rejections.append(
                _row_rejection(
                    artifact=artifact,
                    ratio_family=None,
                    candidate_identity=None,
                    reasons=(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,),
                )
            )
            continue
        for ratio_family in families:
            try:
                candidate = _candidate(
                    row=row,
                    artifact=artifact,
                    ratio_family=ratio_family,
                    field_mapping=mappings.get(ratio_family, ()),
                    instrument_identity=instrument_identity,
                    company_type_resolution=company_type_resolution,
                    period_end=period_end,
                )
            except _IncompatibleUnitError:
                rejections.append(
                    _row_rejection(
                        artifact=artifact,
                        ratio_family=ratio_family,
                        candidate_identity=None,
                        reasons=(FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT,),
                    )
                )
                continue
            except (TypeError, ValueError):
                rejections.append(
                    _row_rejection(
                        artifact=artifact,
                        ratio_family=ratio_family,
                        candidate_identity=None,
                        reasons=(
                            FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,
                        ),
                    )
                )
                continue
            assessment = assess_financial_period_candidate(
                candidate,
                requested_capability=FinancialCapability.RATIO_FAMILY,
                requested_statement_type=None,
                requested_ratio_family=ratio_family,
                expected_instrument_identity=instrument_identity,
                expected_frequency=FinancialReportingFrequency.QUARTERLY,
                expected_currency="XXX",
                expected_consolidation_scope=FinancialConsolidationScope.UNKNOWN,
                pit_as_of_date=pit_as_of_date,
                ratio_declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
            )
            candidates.append(candidate)
            assessments.append(assessment)
            hard_reasons = tuple(
                reason
                for reason in assessment.rejection_reasons
                if reason not in _CURRENT_ONLY_REASONS
            )
            if hard_reasons:
                rejections.append(
                    _row_rejection(
                        artifact=artifact,
                        ratio_family=ratio_family,
                        candidate_identity=candidate.candidate_identity,
                        reasons=hard_reasons,
                    )
                )

    def candidate_key(item: FinancialPeriodCandidate) -> tuple[date, str, str]:
        family = item.period_identity.ratio_family
        assert family is not None
        return item.period_identity.period_end, family.value, item.candidate_identity

    assessment_by_candidate = {
        item.candidate_identity: item for item in assessments
    }
    sorted_candidates = tuple(sorted(candidates, key=candidate_key))
    return (
        sorted_candidates,
        tuple(assessment_by_candidate[item.candidate_identity] for item in sorted_candidates),
        tuple(sorted(rejections, key=lambda item: item.row_identity)),
    )


def qualified_indicator_families(
    company_type: FinancialCompanyType,
) -> tuple[FinancialRatioFamily, ...]:
    if company_type not in _COMPANY_TYPE_FIELDS:
        return ()
    return tuple(FinancialRatioFamily)


def _candidate(
    *,
    row: Mapping[str, object],
    artifact: FinancialProviderArtifactIdentity,
    ratio_family: FinancialRatioFamily,
    field_mapping: Sequence[_NormalizedField],
    instrument_identity: InstrumentIdentityEvidence,
    company_type_resolution: FinancialCompanyTypeResolution,
    period_end: date,
) -> FinancialPeriodCandidate:
    fields = _normalized_fields(row=row, field_mapping=field_mapping)
    raw_currency = _optional_text(row.get("currency"))
    currency = (
        raw_currency.upper()
        if raw_currency is not None
        and len(raw_currency) == 3
        and raw_currency.isalpha()
        else "XXX"
    )
    period_identity = FinancialPeriodIdentity.create(
        instrument_identity=instrument_identity,
        capability=FinancialCapability.RATIO_FAMILY,
        statement_type=None,
        ratio_family=ratio_family,
        period_end=period_end,
        frequency=FinancialReportingFrequency.QUARTERLY,
        company_type=company_type_resolution.company_type,
        consolidation_scope=FinancialConsolidationScope.UNKNOWN,
        currency=currency,
        provider_revision_binding=artifact.artifact_identity,
    )
    filing = FinancialFilingMetadata(
        ann_date=_provider_date(row.get("ann_date"), required=False),
        f_ann_date=None,
        report_type=None,
        comp_type=None,
        update_flag=None,
        retrieved_at=artifact.retrieved_at,
        observed_at=artifact.observed_at,
        local_provider_revision_identity=artifact.artifact_identity,
        provider_filing_revision_id=None,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=period_identity,
        filing_metadata=filing,
        company_type_resolution=company_type_resolution,
        fields=fields,
    )


def _normalized_fields(
    *,
    row: Mapping[str, object],
    field_mapping: Sequence[_NormalizedField],
) -> tuple[FinancialFieldValue, ...]:
    explicit_unit_text = _optional_text(row.get("unit"))
    explicit_unit = (
        explicit_unit_text.upper() if explicit_unit_text is not None else None
    )
    fields: list[FinancialFieldValue] = []
    for field in field_mapping:
        populated = next(
            (
                (source, row.get(source.provider_field))
                for source in field.sources
                if _has_value(row.get(source.provider_field))
            ),
            None,
        )
        if populated is None:
            continue
        source, original = populated
        if explicit_unit is not None and explicit_unit != source.original_unit:
            raise _IncompatibleUnitError("Tushare indicator unit is incompatible")
        numeric = _decimal(original)
        normalized = numeric / source.divisor
        fields.append(
            FinancialFieldValue(
                provider_field=source.provider_field,
                original_value=str(original),
                original_unit=source.original_unit,
                normalized_field=field.normalized_field,
                normalized_value=format(normalized, "f"),
                normalized_unit="RATIO",
            )
        )
    return tuple(fields)


def _row_rejection(
    *,
    artifact: FinancialProviderArtifactIdentity,
    ratio_family: FinancialRatioFamily | None,
    candidate_identity: str | None,
    reasons: Sequence[FinancialPeriodRejectionReason],
) -> TushareIndicatorRowRejection:
    encoded = json.dumps(
        {
            "artifact_identity": artifact.artifact_identity,
            "candidate_identity": candidate_identity,
            "ratio_family": ratio_family.value if ratio_family is not None else None,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return TushareIndicatorRowRejection(
        row_identity=f"tushare-indicator-row:v1:{sha256(encoded).hexdigest()}",
        artifact_identity=artifact.artifact_identity,
        ratio_family=ratio_family,
        candidate_identity=candidate_identity,
        reasons=tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in reasons
        ),
    )


def _decimal(value: object) -> Decimal:
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Tushare indicator value is malformed") from exc
    if not numeric.is_finite():
        raise ValueError("Tushare indicator value is malformed")
    return numeric


def _has_value(value: object) -> bool:
    if value is None:
        return False
    try:
        return not bool(pd.isna(value))
    except (TypeError, ValueError):
        return True


def _optional_text(value: object) -> str | None:
    if not _has_value(value):
        return None
    text = str(value).strip()
    return text or None


def _provider_date(value: object, *, required: bool) -> date | None:
    text = _optional_text(value)
    if text is None:
        if required:
            raise ValueError("Tushare indicator period end is missing")
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Tushare indicator date is malformed") from exc


__all__ = [
    "TUSHARE_FINANCIAL_INDICATOR_ADAPTER_VERSION",
    "TushareIndicatorRowRejection",
    "project_indicator_candidates",
    "qualified_indicator_families",
]

"""Provider-neutral normalization for immutable Tushare statement artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.dataflows._tushare_statement_artifacts import (
    TUSHARE_STATEMENT_ADAPTER_VERSION,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialPeriodCandidate,
    FinancialPeriodIdentity,
    FinancialPeriodRejectionReason,
    FinancialProviderArtifactIdentity,
    FinancialReportingFrequency,
    FinancialStatementType,
    resolve_financial_company_type,
)
from tradingagents.evidence import InstrumentIdentityEvidence

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_COMPANY_TYPES = {
    "1": FinancialCompanyType.INDUSTRIAL_NON_BANK,
    "2": FinancialCompanyType.BANK,
    "3": FinancialCompanyType.INSURANCE,
    "4": FinancialCompanyType.SECURITIES,
}
_CONSOLIDATED_REPORT_TYPES = frozenset({"1", "2", "3", "4", "5"})
_PARENT_REPORT_TYPES = frozenset({"6", "7", "8", "9", "10"})
_QUARTER_ENDS = frozenset({(3, 31), (6, 30), (9, 30), (12, 31)})

_BALANCE_FIELDS: dict[FinancialCompanyType, dict[str, tuple[str, ...]]] = {
    FinancialCompanyType.INDUSTRIAL_NON_BANK: {
        "accounts_payable": ("acct_payable", "accounts_payable"),
        "accounts_receivable": ("accounts_receiv", "accounts_receivable"),
        "cash_and_equivalents": ("money_cap", "cash_and_equivalents"),
        "inventory": ("inventories", "inventory"),
        "long_term_debt": ("lt_borr", "long_term_debt"),
        "property_plant_equipment": ("fix_assets", "property_plant_equipment"),
        "short_term_debt": ("st_borr", "short_term_debt"),
        "total_assets": ("total_assets",),
        "total_equity": (
            "total_hldr_eqy_inc_min_int",
            "total_hldr_eqy_exc_min_int",
            "total_equity",
        ),
        "total_liabilities": ("total_liab", "total_liabilities"),
    },
    FinancialCompanyType.BANK: {
        "cash_and_central_bank": ("cash_reser_cb", "cash_and_central_bank"),
        "customer_deposits": ("depos", "customer_deposits"),
        "financial_investments": ("fin_assets", "financial_investments"),
        "interbank_assets": ("depos_in_oth_bfi", "interbank_assets"),
        "interbank_liabilities": ("depos_oth_bfi", "interbank_liabilities"),
        "loan_loss_allowance": ("loan_loss_reser", "loan_loss_allowance"),
        "loans_and_advances": ("loans", "loans_and_advances"),
        "total_assets": ("total_assets",),
        "total_equity": (
            "total_hldr_eqy_inc_min_int",
            "total_hldr_eqy_exc_min_int",
            "total_equity",
        ),
        "total_liabilities": ("total_liab", "total_liabilities"),
    },
}
_INCOME_FIELDS: dict[FinancialCompanyType, dict[str, tuple[str, ...]]] = {
    FinancialCompanyType.INDUSTRIAL_NON_BANK: {
        "cost_of_revenue": ("oper_cost", "cost_of_revenue"),
        "finance_costs": ("fin_exp", "finance_costs"),
        "gross_profit": ("gross_profit",),
        "income_tax_expense": ("income_tax", "income_tax_expense"),
        "net_income": ("n_income_attr_p", "n_income", "net_income"),
        "operating_expense": ("total_cogs", "operating_expense"),
        "operating_profit": ("operate_profit", "operating_profit"),
        "operating_revenue": ("total_revenue", "revenue", "operating_revenue"),
        "other_income": ("oth_income", "other_income"),
        "research_development_expense": (
            "rd_exp",
            "research_development_expense",
        ),
    },
    FinancialCompanyType.BANK: {
        "credit_impairment_loss": ("credit_impa_loss", "credit_impairment_loss"),
        "fee_commission_income": ("comm_income", "fee_commission_income"),
        "income_tax_expense": ("income_tax", "income_tax_expense"),
        "interest_expense": ("int_exp", "interest_expense"),
        "interest_income": ("int_income", "interest_income"),
        "investment_income": ("invest_income", "investment_income"),
        "net_income": ("n_income_attr_p", "n_income", "net_income"),
        "net_interest_income": ("n_int_income", "net_interest_income"),
        "operating_expense": ("oper_cost", "operating_expense"),
        "operating_profit": ("operate_profit", "operating_profit"),
    },
}
_CASH_FLOW_FIELDS = {
    "cash_from_financing": ("n_cash_flows_fnc_act", "cash_from_financing"),
    "cash_from_investing": ("n_cashflow_inv_act", "cash_from_investing"),
    "cash_from_operations": ("n_cashflow_act", "cash_from_operations"),
    "cash_paid_for_interest": ("c_pay_interest", "cash_paid_for_interest"),
    "cash_paid_for_taxes": ("c_paid_for_taxes", "cash_paid_for_taxes"),
    "cash_received_from_customers": (
        "c_fr_sale_sg",
        "cash_received_from_customers",
    ),
    "closing_cash": ("c_cash_equ_end_period", "closing_cash"),
    "effect_of_exchange_rates": ("eff_fx_flu_cash", "effect_of_exchange_rates"),
    "net_change_in_cash": ("n_incr_cash_cash_equ", "net_change_in_cash"),
    "opening_cash": ("c_cash_equ_beg_period", "opening_cash"),
}


class TushareStatementRowRejection(BaseModel):
    """Payload-free typed disposition for one retained provider row."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["tushare-statement-adapter-v1"] = (
        TUSHARE_STATEMENT_ADAPTER_VERSION
    )
    row_identity: str = Field(pattern=r"^tushare-statement-row:v1:[0-9a-f]{64}$")
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    candidate_identities: tuple[str, ...] = ()
    reasons: tuple[FinancialPeriodRejectionReason, ...]

    @model_validator(mode="after")
    def _validate_rejection(self) -> TushareStatementRowRejection:
        if self.candidate_identities != tuple(sorted(set(self.candidate_identities))):
            raise ValueError("Tushare rejected candidate identities must be canonical")
        canonical_reasons = tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in self.reasons
        )
        if not canonical_reasons or self.reasons != canonical_reasons:
            raise ValueError("Tushare statement rejection reasons must be canonical")
        return self


def project_candidates(
    *,
    payload: Mapping[str, object],
    row_artifacts: Sequence[FinancialProviderArtifactIdentity],
    statement_type: FinancialStatementType,
    instrument_identity: InstrumentIdentityEvidence,
    expected_provider_symbol: str,
) -> tuple[
    tuple[FinancialPeriodCandidate, ...],
    tuple[FinancialPeriodCandidate, ...],
    tuple[TushareStatementRowRejection, ...],
]:
    """Project annual and reporting views from one already-persisted artifact."""

    annual: list[FinancialPeriodCandidate] = []
    reporting: list[FinancialPeriodCandidate] = []
    rejections: list[TushareStatementRowRejection] = []
    rows = payload["rows"]
    assert isinstance(rows, list)
    if len(rows) != len(row_artifacts):
        raise ValueError("Tushare statement row artifacts are incomplete")
    for row_index, row in enumerate(rows):
        assert isinstance(row, dict)
        row_artifact = row_artifacts[row_index]
        row_candidates: list[FinancialPeriodCandidate] = []
        try:
            if _optional_text(row.get("ts_code")) != expected_provider_symbol:
                raise ValueError("Tushare statement row symbol does not match request")
            period_end = _provider_date(row.get("end_date"), required=True)
            assert period_end is not None
            if (period_end.month, period_end.day) not in _QUARTER_ENDS:
                raise ValueError("Tushare statement period is not quarterly")
            if period_end.month == 12 and period_end.day == 31:
                annual_candidate = _candidate(
                    row=row,
                    artifact=row_artifact,
                    statement_type=statement_type,
                    instrument_identity=instrument_identity,
                    period_end=period_end,
                    frequency=FinancialReportingFrequency.ANNUAL,
                )
                annual.append(annual_candidate)
                row_candidates.append(annual_candidate)
            reporting_candidate = _candidate(
                row=row,
                artifact=row_artifact,
                statement_type=statement_type,
                instrument_identity=instrument_identity,
                period_end=period_end,
                frequency=FinancialReportingFrequency.QUARTERLY,
            )
            reporting.append(reporting_candidate)
            row_candidates.append(reporting_candidate)
        except (TypeError, ValueError):
            rejections.append(
                _row_rejection(
                    artifact=row_artifact,
                    row_index=row_index,
                    candidate_identities=(),
                    reasons=(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,),
                )
            )
            continue
        reasons = _candidate_rejection_reasons(row_candidates[0])
        if reasons:
            rejections.append(
                _row_rejection(
                    artifact=row_artifact,
                    row_index=row_index,
                    candidate_identities=tuple(
                        candidate.candidate_identity for candidate in row_candidates
                    ),
                    reasons=reasons,
                )
            )

    def candidate_key(item: FinancialPeriodCandidate) -> tuple[date, str]:
        return item.period_identity.period_end, item.candidate_identity

    return (
        tuple(sorted(annual, key=candidate_key)),
        tuple(sorted(reporting, key=candidate_key)),
        tuple(rejections),
    )


def _candidate_rejection_reasons(
    candidate: FinancialPeriodCandidate,
) -> tuple[FinancialPeriodRejectionReason, ...]:
    reasons: set[FinancialPeriodRejectionReason] = set()
    resolution = candidate.company_type_resolution
    if (
        resolution.company_type is FinancialCompanyType.UNKNOWN
        or resolution.ambiguous
        or resolution.contradictory
    ):
        reasons.add(FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE)
    if candidate.period_identity.consolidation_scope is FinancialConsolidationScope.UNKNOWN:
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE)
    if candidate.period_identity.currency != "CNY":
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY)
    if any(
        field.normalized_value is not None
        and field.normalized_unit != "CNY_BASE_UNIT"
        for field in candidate.fields
    ):
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT)
    if candidate.filing_metadata.ann_date is None:
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)
    return tuple(
        reason for reason in FinancialPeriodRejectionReason if reason in reasons
    )


def _row_rejection(
    *,
    artifact: FinancialProviderArtifactIdentity,
    row_index: int,
    candidate_identities: Sequence[str],
    reasons: Sequence[FinancialPeriodRejectionReason],
) -> TushareStatementRowRejection:
    encoded = json.dumps(
        {
            "artifact_identity": artifact.artifact_identity,
            "row_index": row_index,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return TushareStatementRowRejection(
        row_identity=f"tushare-statement-row:v1:{sha256(encoded).hexdigest()}",
        artifact_identity=artifact.artifact_identity,
        candidate_identities=tuple(sorted(set(candidate_identities))),
        reasons=tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in reasons
        ),
    )


def _candidate(
    *,
    row: Mapping[str, object],
    artifact: FinancialProviderArtifactIdentity,
    statement_type: FinancialStatementType,
    instrument_identity: InstrumentIdentityEvidence,
    period_end: date,
    frequency: FinancialReportingFrequency,
) -> FinancialPeriodCandidate:
    raw_comp_type = _optional_text(row.get("comp_type"))
    company_type = _COMPANY_TYPES.get(raw_comp_type or "", FinancialCompanyType.UNKNOWN)
    resolution = resolve_financial_company_type(
        provider_declared_type=company_type,
        provider_declaration_qualified=company_type is not FinancialCompanyType.UNKNOWN,
        classifier_metadata={},
        present_fields=(
            _classifier_present_fields(
                row=row,
                statement_type=statement_type,
            )
            if company_type is not FinancialCompanyType.UNKNOWN
            else ()
        ),
    )
    fields = _normalized_fields(
        row=row,
        statement_type=statement_type,
        company_type=resolution.company_type,
    )
    raw_report_type = _optional_text(row.get("report_type"))
    scope = (
        FinancialConsolidationScope.CONSOLIDATED
        if raw_report_type in _CONSOLIDATED_REPORT_TYPES
        else (
            FinancialConsolidationScope.PARENT
            if raw_report_type in _PARENT_REPORT_TYPES
            else FinancialConsolidationScope.UNKNOWN
        )
    )
    currency = _optional_text(row.get("currency")) or "CNY"
    period_identity = FinancialPeriodIdentity.create(
        instrument_identity=instrument_identity,
        capability=FinancialCapability.STATEMENT,
        statement_type=statement_type,
        ratio_family=None,
        period_end=period_end,
        frequency=frequency,
        company_type=resolution.company_type,
        consolidation_scope=scope,
        currency=currency.upper(),
        provider_revision_binding=artifact.artifact_identity,
    )
    filing = FinancialFilingMetadata(
        ann_date=_provider_date(row.get("ann_date"), required=False),
        f_ann_date=_provider_date(row.get("f_ann_date"), required=False),
        report_type=raw_report_type,
        comp_type=raw_comp_type,
        update_flag=_optional_text(row.get("update_flag")),
        retrieved_at=artifact.retrieved_at,
        observed_at=artifact.observed_at,
        local_provider_revision_identity=artifact.artifact_identity,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=period_identity,
        filing_metadata=filing,
        company_type_resolution=resolution,
        fields=fields,
    )


def _classifier_present_fields(
    *,
    row: Mapping[str, object],
    statement_type: FinancialStatementType,
) -> tuple[str, ...]:
    """Collect signatures independently of the provider-declared company type."""

    if statement_type is FinancialStatementType.CASH_FLOW:
        mappings = (_CASH_FLOW_FIELDS,)
    elif statement_type is FinancialStatementType.BALANCE_SHEET:
        mappings = tuple(_BALANCE_FIELDS.values())
    else:
        mappings = tuple(_INCOME_FIELDS.values())
    present = {
        normalized_field
        for mapping in mappings
        for normalized_field, aliases in mapping.items()
        if any(_has_value(row.get(provider_field)) for provider_field in aliases)
    }
    return tuple(sorted(present))


def _normalized_fields(
    *,
    row: Mapping[str, object],
    statement_type: FinancialStatementType,
    company_type: FinancialCompanyType,
) -> tuple[FinancialFieldValue, ...]:
    if statement_type is FinancialStatementType.CASH_FLOW:
        mappings = _CASH_FLOW_FIELDS
    else:
        mappings = {
            FinancialStatementType.BALANCE_SHEET: _BALANCE_FIELDS,
            FinancialStatementType.INCOME_STATEMENT: _INCOME_FIELDS,
        }.get(statement_type, {}).get(company_type, {})
    original_unit, normalized_unit = _statement_units(row)
    fields: list[FinancialFieldValue] = []
    for normalized_field, aliases in mappings.items():
        populated = [
            (provider_field, row.get(provider_field))
            for provider_field in aliases
            if _has_value(row.get(provider_field))
        ]
        if not populated:
            continue
        provider_field, original = populated[0]
        normalized_value = _decimal_text(original)
        fields.append(
            FinancialFieldValue(
                provider_field=provider_field,
                original_value=str(original),
                original_unit=original_unit,
                normalized_field=normalized_field,
                normalized_value=normalized_value,
                normalized_unit=normalized_unit,
            )
        )
    return tuple(fields)


def _statement_units(row: Mapping[str, object]) -> tuple[str, str]:
    currency = (_optional_text(row.get("currency")) or "CNY").upper()
    explicit_unit = _optional_text(row.get("unit"))
    if currency != "CNY" and explicit_unit is None:
        unknown = f"{currency}_UNKNOWN_SCALE"
        return unknown, unknown
    if explicit_unit is None:
        return "CNY_BASE_UNIT", "CNY_BASE_UNIT"
    if currency == "CNY" and explicit_unit.casefold() in {
        "cny_base_unit",
        "cny_yuan",
        "yuan",
        "元",
    }:
        return explicit_unit, "CNY_BASE_UNIT"
    return explicit_unit, explicit_unit


def _has_value(value: object) -> bool:
    if value is None:
        return False
    try:
        return not bool(pd.isna(value))
    except (TypeError, ValueError):
        return True


def _decimal_text(value: object) -> str:
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Tushare statement monetary value is malformed") from exc
    if not numeric.is_finite():
        raise ValueError("Tushare statement monetary value is malformed")
    return format(numeric, "f")


def _optional_text(value: object) -> str | None:
    if not _has_value(value):
        return None
    text = str(value).strip()
    return text or None


def _provider_date(value: object, *, required: bool) -> date | None:
    text = _optional_text(value)
    if text is None:
        if required:
            raise ValueError("Tushare statement period end is missing")
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Tushare statement date is malformed") from exc


__all__ = ["TushareStatementRowRejection", "project_candidates"]

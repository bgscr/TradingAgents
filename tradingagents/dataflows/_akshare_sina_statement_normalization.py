"""Ticket 02 candidate projection for immutable AKShare-Sina statements."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.dataflows._akshare_sina_statement_artifacts import (
    AKSHARE_SINA_MONETARY_UNIT_CONTRACT,
    AKSHARE_SINA_STATEMENT_ADAPTER_VERSION,
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
    assess_financial_period_candidate,
    resolve_financial_company_type,
)
from tradingagents.evidence import InstrumentIdentityEvidence

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_PROVIDER_FIELD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_QUARTER_ENDS = frozenset({(3, 31), (6, 30), (9, 30), (12, 31)})
_CNY_ALIASES = frozenset({"CNY", "RMB", "人民币", "人民币元"})
_CONSOLIDATED_TYPES = frozenset(
    {"合并", "合并报表", "合并口径", "合并期末", "consolidated", "group"}
)
_PARENT_TYPES = frozenset({"母公司", "母公司报表", "parent", "standalone"})
_COMPANY_TYPES = {
    "bank": FinancialCompanyType.BANK,
    "commercial_bank": FinancialCompanyType.BANK,
    "银行": FinancialCompanyType.BANK,
    "industrial": FinancialCompanyType.INDUSTRIAL_NON_BANK,
    "non_bank": FinancialCompanyType.INDUSTRIAL_NON_BANK,
    "non-bank": FinancialCompanyType.INDUSTRIAL_NON_BANK,
    "工业": FinancialCompanyType.INDUSTRIAL_NON_BANK,
    "insurance": FinancialCompanyType.INSURANCE,
    "保险": FinancialCompanyType.INSURANCE,
    "securities": FinancialCompanyType.SECURITIES,
    "证券": FinancialCompanyType.SECURITIES,
    "other_financial": FinancialCompanyType.OTHER_FINANCIAL,
    "financial_services": FinancialCompanyType.OTHER_FINANCIAL,
}

# Canonical field -> qualified AKShare-Sina aliases. The immutable artifact keeps
# every original provider label/value; FinancialFieldValue uses a safe stable ID.
_BALANCE_FIELDS: dict[FinancialCompanyType, dict[str, tuple[str, ...]]] = {
    FinancialCompanyType.INDUSTRIAL_NON_BANK: {
        "accounts_payable": ("应付账款", "应付账款及票据", "accounts_payable"),
        "accounts_receivable": ("应收账款", "应收账款及票据", "accounts_receivable"),
        "cash_and_equivalents": ("货币资金", "现金及现金等价物", "cash_and_equivalents"),
        "inventory": ("存货", "inventory"),
        "long_term_debt": ("长期借款", "long_term_debt"),
        "property_plant_equipment": ("固定资产", "property_plant_equipment"),
        "short_term_debt": ("短期借款", "short_term_debt"),
        "total_assets": ("资产总计", "资产合计", "total_assets"),
        "total_equity": (
            "所有者权益合计",
            "股东权益合计",
            "所有者权益(或股东权益)合计",
            "所有者权益（或股东权益）合计",
            "total_equity",
        ),
        "total_liabilities": ("负债合计", "负债总计", "total_liabilities"),
    },
    FinancialCompanyType.BANK: {
        "cash_and_central_bank": (
            "现金及存放中央银行款项",
            "现金及存放央行款项",
            "cash_and_central_bank",
        ),
        "customer_deposits": ("吸收存款", "客户存款", "customer_deposits"),
        "financial_investments": ("金融投资", "financial_investments"),
        "interbank_assets": ("存放同业款项", "存放同业及其他金融机构款项", "interbank_assets"),
        "interbank_liabilities": (
            "同业及其他金融机构存放款项",
            "同业存放款项",
            "interbank_liabilities",
        ),
        "loan_loss_allowance": ("贷款损失准备", "loan_loss_allowance"),
        "loans_and_advances": ("发放贷款和垫款", "客户贷款及垫款", "loans_and_advances"),
        "total_assets": ("资产总计", "资产合计", "total_assets"),
        "total_equity": (
            "所有者权益合计",
            "股东权益合计",
            "所有者权益（或股东权益）合计",
            "total_equity",
        ),
        "total_liabilities": ("负债合计", "负债总计", "total_liabilities"),
    },
}

_INCOME_FIELDS: dict[FinancialCompanyType, dict[str, tuple[str, ...]]] = {
    FinancialCompanyType.INDUSTRIAL_NON_BANK: {
        "cost_of_revenue": ("营业成本", "主营业务成本", "cost_of_revenue"),
        "finance_costs": ("财务费用", "finance_costs"),
        "gross_profit": ("毛利", "毛利润", "gross_profit"),
        "income_tax_expense": ("所得税费用", "income_tax_expense"),
        "net_income": ("净利润", "归属于母公司股东的净利润", "net_income"),
        "operating_expense": ("营业总成本", "营业支出", "operating_expense"),
        "operating_profit": ("营业利润", "operating_profit"),
        "operating_revenue": ("营业总收入", "营业收入", "operating_revenue"),
        "other_income": ("其他收益", "其他业务收入", "other_income"),
        "research_development_expense": ("研发费用", "研究与开发费用", "research_development_expense"),
    },
    FinancialCompanyType.BANK: {
        "credit_impairment_loss": ("信用减值损失", "credit_impairment_loss"),
        "fee_commission_income": ("手续费及佣金收入", "fee_commission_income"),
        "income_tax_expense": ("所得税费用", "income_tax_expense"),
        "interest_expense": ("利息支出", "interest_expense"),
        "interest_income": ("利息收入", "interest_income"),
        "investment_income": ("投资收益", "investment_income"),
        "net_income": ("净利润", "归属于母公司股东的净利润", "net_income"),
        "net_interest_income": ("利息净收入", "净利息收入", "net_interest_income"),
        "operating_expense": ("业务及管理费", "营业支出", "operating_expense"),
        "operating_profit": ("营业利润", "operating_profit"),
    },
}

_CASH_FLOW_FIELDS = {
    "cash_from_financing": ("筹资活动产生的现金流量净额", "cash_from_financing"),
    "cash_from_investing": ("投资活动产生的现金流量净额", "cash_from_investing"),
    "cash_from_operations": ("经营活动产生的现金流量净额", "cash_from_operations"),
    "cash_paid_for_interest": (
        "支付利息、手续费及佣金的现金",
        "分配股利、利润或偿付利息支付的现金",
        "cash_paid_for_interest",
    ),
    "cash_paid_for_taxes": ("支付的各项税费", "cash_paid_for_taxes"),
    "cash_received_from_customers": (
        "销售商品、提供劳务收到的现金",
        "cash_received_from_customers",
    ),
    "closing_cash": ("期末现金及现金等价物余额", "closing_cash"),
    "effect_of_exchange_rates": (
        "汇率变动对现金及现金等价物的影响",
        "effect_of_exchange_rates",
    ),
    "net_change_in_cash": ("现金及现金等价物净增加额", "net_change_in_cash"),
    "opening_cash": ("期初现金及现金等价物余额", "opening_cash"),
}

_CASH_FLOW_INDUSTRIAL_SIGNATURES = frozenset(
    {
        "销售商品、提供劳务收到的现金",
        "购买商品、接受劳务支付的现金",
    }
)
_CASH_FLOW_BANK_SIGNATURES = frozenset(
    {
        "客户存款和同业存放款项净增加额",
        "向中央银行借款净增加额",
        "发放贷款和垫款净增加额",
    }
)

_INDICATOR_MARKERS = (
    "比率",
    "率(%)",
    "率（%）",
    "净资产收益率",
    "每股收益",
    "roe",
    "ratio",
)


class AkshareSinaStatementRowRejection(BaseModel):
    """Typed disposition for one retained original provider row."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["akshare-sina-statement-adapter-v1"] = (
        AKSHARE_SINA_STATEMENT_ADAPTER_VERSION
    )
    row_identity: str = Field(
        pattern=r"^akshare-sina-statement-row:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    candidate_identities: tuple[str, ...] = ()
    reasons: tuple[FinancialPeriodRejectionReason, ...]

    @model_validator(mode="after")
    def _validate_rejection(self) -> AkshareSinaStatementRowRejection:
        if self.candidate_identities != tuple(sorted(set(self.candidate_identities))):
            raise ValueError("AKShare-Sina rejected candidate identities must be canonical")
        canonical_reasons = tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in self.reasons
        )
        if not canonical_reasons or self.reasons != canonical_reasons:
            raise ValueError("AKShare-Sina rejection reasons must be canonical")
        return self


def project_candidates(
    *,
    payload: Mapping[str, object],
    row_artifacts: Sequence[FinancialProviderArtifactIdentity],
    statement_type: FinancialStatementType,
    instrument_identity: InstrumentIdentityEvidence,
    range_start: date,
    as_of_date: date,
) -> tuple[
    tuple[FinancialPeriodCandidate, ...],
    tuple[FinancialPeriodCandidate, ...],
    tuple[AkshareSinaStatementRowRejection, ...],
]:
    """Project annual/reporting views without discarding independent rows."""

    annual: list[FinancialPeriodCandidate] = []
    reporting: list[FinancialPeriodCandidate] = []
    rejections: list[AkshareSinaStatementRowRejection] = []
    rows = payload["rows"]
    assert isinstance(rows, list)
    monetary_unit_contract = str(payload["monetary_unit_contract"])
    if len(rows) != len(row_artifacts):
        raise ValueError("AKShare-Sina statement row artifacts are incomplete")
    for row_index, row in enumerate(rows):
        assert isinstance(row, dict)
        artifact = row_artifacts[row_index]
        if _looks_like_non_statement_payload(row, statement_type=statement_type):
            rejections.append(
                _row_rejection(
                    artifact=artifact,
                    row_index=row_index,
                    candidate_identities=(),
                    reasons=(FinancialPeriodRejectionReason.CAPABILITY_MISMATCH,),
                )
            )
            continue
        candidates: list[FinancialPeriodCandidate] = []
        try:
            period_end = _provider_date(_first_value(row, "报告日", "报告日期", "end_date"), required=True)
            assert period_end is not None
            if (
                (period_end.month, period_end.day) not in _QUARTER_ENDS
                or period_end < range_start
                or period_end > as_of_date
            ):
                raise ValueError("AKShare-Sina statement period is outside the request")
            if period_end.month == 12 and period_end.day == 31:
                candidate = _candidate(
                    row=row,
                    artifact=artifact,
                    statement_type=statement_type,
                    instrument_identity=instrument_identity,
                    period_end=period_end,
                    frequency=FinancialReportingFrequency.ANNUAL,
                    monetary_unit_contract=monetary_unit_contract,
                )
                annual.append(candidate)
                candidates.append(candidate)
            candidate = _candidate(
                row=row,
                artifact=artifact,
                statement_type=statement_type,
                instrument_identity=instrument_identity,
                period_end=period_end,
                frequency=FinancialReportingFrequency.QUARTERLY,
                monetary_unit_contract=monetary_unit_contract,
            )
            reporting.append(candidate)
            candidates.append(candidate)
        except (InvalidOperation, TypeError, ValueError):
            rejections.append(
                _row_rejection(
                    artifact=artifact,
                    row_index=row_index,
                    candidate_identities=(),
                    reasons=(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,),
                )
            )
            continue
        reasons = _candidate_rejection_reasons(
            candidates[0],
            as_of_date=as_of_date,
        )
        if reasons:
            rejections.append(
                _row_rejection(
                    artifact=artifact,
                    row_index=row_index,
                    candidate_identities=tuple(
                        candidate.candidate_identity for candidate in candidates
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


def _candidate(
    *,
    row: Mapping[str, object],
    artifact: FinancialProviderArtifactIdentity,
    statement_type: FinancialStatementType,
    instrument_identity: InstrumentIdentityEvidence,
    period_end: date,
    frequency: FinancialReportingFrequency,
    monetary_unit_contract: str,
) -> FinancialPeriodCandidate:
    raw_comp_type = _optional_text(
        _first_value(row, "公司类型", "comp_type", "provider_company_type")
    )
    declared = _company_type(raw_comp_type)
    classifier_metadata = _classifier_metadata(row)
    present_fields = _classifier_present_fields(
        row=row,
        statement_type=statement_type,
    )
    resolution = resolve_financial_company_type(
        provider_declared_type=declared,
        provider_declaration_qualified=(
            raw_comp_type is not None and declared is not FinancialCompanyType.UNKNOWN
        ),
        classifier_metadata=classifier_metadata,
        present_fields=present_fields,
    )
    fields = _normalized_fields(
        row=row,
        statement_type=statement_type,
        company_type=resolution.company_type,
        monetary_unit_contract=monetary_unit_contract,
    )
    raw_report_type = _optional_text(
        _first_value(row, "类型", "报表类型", "合并类型", "report_type")
    )
    scope = _consolidation_scope(raw_report_type)
    raw_currency = _optional_text(_first_value(row, "币种", "currency"))
    currency = _currency(raw_currency)
    period_identity = FinancialPeriodIdentity.create(
        instrument_identity=instrument_identity,
        capability=FinancialCapability.STATEMENT,
        statement_type=statement_type,
        ratio_family=None,
        period_end=period_end,
        frequency=frequency,
        company_type=resolution.company_type,
        consolidation_scope=scope,
        currency=currency,
        provider_revision_binding=artifact.artifact_identity,
    )
    filing = FinancialFilingMetadata(
        ann_date=_provider_date(
            _first_value(row, "公告日期", "ann_date", "publish_date"),
            required=False,
        ),
        f_ann_date=_provider_date(
            _first_value(row, "首次公告日期", "首次披露日期", "f_ann_date"),
            required=False,
        ),
        report_type=raw_report_type,
        comp_type=raw_comp_type,
        update_flag=_optional_text(
            _first_value(row, "更新日期", "更新字段", "update_flag", "update_time")
        ),
        retrieved_at=artifact.retrieved_at,
        observed_at=artifact.observed_at,
        local_provider_revision_identity=artifact.artifact_identity,
        # The qualified AKShare-Sina contract exposes no stable filing revision
        # identity. Update/revision-looking provider fields remain raw metadata.
        provider_filing_revision_id=None,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=period_identity,
        filing_metadata=filing,
        company_type_resolution=resolution,
        fields=fields,
    )


def _classifier_metadata(row: Mapping[str, object]) -> dict[str, str]:
    for provider_key, canonical_key in (
        ("行业", "industry"),
        ("行业名称", "industry_name"),
        ("provider_industry", "provider_industry"),
        ("sector", "sector"),
    ):
        value = _optional_text(row.get(provider_key))
        normalized = _company_type_hint(value)
        if normalized is not None:
            return {canonical_key: normalized}
    return {}


def _classifier_present_fields(
    *,
    row: Mapping[str, object],
    statement_type: FinancialStatementType,
) -> tuple[str, ...]:
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
        if _populated_alias(row, aliases) is not None
    }
    if statement_type is FinancialStatementType.CASH_FLOW:
        if any(_has_value(row.get(field)) for field in _CASH_FLOW_INDUSTRIAL_SIGNATURES):
            present.add("inventory")
        if any(_has_value(row.get(field)) for field in _CASH_FLOW_BANK_SIGNATURES):
            present.add("customer_deposits")
    return tuple(sorted(present))


def _normalized_fields(
    *,
    row: Mapping[str, object],
    statement_type: FinancialStatementType,
    company_type: FinancialCompanyType,
    monetary_unit_contract: str,
) -> tuple[FinancialFieldValue, ...]:
    if statement_type is FinancialStatementType.CASH_FLOW:
        mapping = _CASH_FLOW_FIELDS
    else:
        mapping = {
            FinancialStatementType.BALANCE_SHEET: _BALANCE_FIELDS,
            FinancialStatementType.INCOME_STATEMENT: _INCOME_FIELDS,
        }.get(statement_type, {}).get(company_type, {})
    raw_currency = _optional_text(_first_value(row, "币种", "currency"))
    raw_unit = _optional_text(_first_value(row, "单位", "金额单位", "unit"))
    scale = _explicit_cny_scale(
        currency=raw_currency,
        unit=raw_unit,
        monetary_unit_contract=monetary_unit_contract,
    )
    fields: list[FinancialFieldValue] = []
    for normalized_field, aliases in mapping.items():
        populated = _populated_alias(row, aliases)
        if populated is None:
            continue
        provider_field, original = populated
        normalized_value = (
            format(_decimal(original) * scale, "f") if scale is not None else None
        )
        fields.append(
            FinancialFieldValue(
                provider_field=(
                    provider_field
                    if _SAFE_PROVIDER_FIELD.fullmatch(provider_field)
                    else f"sina.{normalized_field}"
                ),
                original_value=str(original),
                original_unit=raw_unit,
                normalized_field=normalized_field,
                normalized_value=normalized_value,
                normalized_unit=("CNY_BASE_UNIT" if scale is not None else None),
            )
        )
    return tuple(fields)


def _candidate_rejection_reasons(
    candidate: FinancialPeriodCandidate,
    *,
    as_of_date: date,
) -> tuple[FinancialPeriodRejectionReason, ...]:
    identity = candidate.period_identity
    assessment = assess_financial_period_candidate(
        candidate,
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=identity.statement_type,
        requested_ratio_family=None,
        expected_instrument_identity=identity.instrument_identity,
        expected_frequency=identity.frequency,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=as_of_date,
    )
    reasons = set(assessment.rejection_reasons)
    # Row rejections record hard adapter gates. Ticket 02's missing/late first
    # publication outcomes remain explicit on the candidate assessment but do
    # not turn a current-only candidate into a rejected row.
    reasons.difference_update(
        {
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE,
            FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED,
        }
    )
    # Ticket 02's completeness assessment cannot distinguish an absent field
    # from a retained provider value whose scale is unknown. The latter is an
    # adapter-specific incompatible-unit outcome in addition to completeness.
    if any(
        field.normalized_value is None or field.normalized_unit != "CNY_BASE_UNIT"
        for field in candidate.fields
    ):
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT)
    return tuple(
        reason for reason in FinancialPeriodRejectionReason if reason in reasons
    )


def _row_rejection(
    *,
    artifact: FinancialProviderArtifactIdentity,
    row_index: int,
    candidate_identities: Sequence[str],
    reasons: Sequence[FinancialPeriodRejectionReason],
) -> AkshareSinaStatementRowRejection:
    encoded = json.dumps(
        {"artifact_identity": artifact.artifact_identity, "row_index": row_index},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return AkshareSinaStatementRowRejection(
        row_identity=(
            "akshare-sina-statement-row:v1:" + sha256(encoded).hexdigest()
        ),
        artifact_identity=artifact.artifact_identity,
        candidate_identities=tuple(sorted(set(candidate_identities))),
        reasons=tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in reasons
        ),
    )


def _looks_like_non_statement_payload(
    row: Mapping[str, object],
    *,
    statement_type: FinancialStatementType,
) -> bool:
    explicit = _optional_text(row.get("capability"))
    if explicit is not None and explicit.casefold() in {
        "financial_indicator",
        "financial_indicators",
        "financial_ratio_family",
        "ratio",
    }:
        return True
    recognized = _classifier_present_fields(row=row, statement_type=statement_type)
    return not recognized and any(
        marker in str(column).casefold()
        for column in row
        for marker in _INDICATOR_MARKERS
    )


def _explicit_cny_scale(
    *,
    currency: str | None,
    unit: str | None,
    monetary_unit_contract: str,
) -> Decimal | None:
    if currency is None or currency.strip().upper() not in _CNY_ALIASES:
        return None
    if unit is None:
        # AKShare 1.18.73 passes Sina item_value through without scaling and
        # supplies rCurrency as CNY. The versioned artifact envelope makes
        # that qualified endpoint-level base-unit contract auditable even
        # though the returned DataFrame has no synthetic unit column.
        return (
            Decimal(1)
            if monetary_unit_contract == AKSHARE_SINA_MONETARY_UNIT_CONTRACT
            else None
        )
    normalized = unit.strip().casefold()
    return {
        "元": Decimal(1),
        "人民币元": Decimal(1),
        "cny_base_unit": Decimal(1),
        "cny_yuan": Decimal(1),
        "yuan": Decimal(1),
        "千元": Decimal(1_000),
        "万元": Decimal(10_000),
        "百万元": Decimal(1_000_000),
    }.get(normalized)


def _company_type(value: str | None) -> FinancialCompanyType:
    if value is None:
        return FinancialCompanyType.UNKNOWN
    return _COMPANY_TYPES.get(
        value.strip().casefold().replace(" ", "_"),
        FinancialCompanyType.UNKNOWN,
    )


def _company_type_hint(value: str | None) -> str | None:
    company_type = _company_type(value)
    return {
        FinancialCompanyType.BANK: "bank",
        FinancialCompanyType.INDUSTRIAL_NON_BANK: "industrial",
        FinancialCompanyType.INSURANCE: "insurance",
        FinancialCompanyType.SECURITIES: "securities",
        FinancialCompanyType.OTHER_FINANCIAL: "other_financial",
    }.get(company_type)


def _consolidation_scope(value: str | None) -> FinancialConsolidationScope:
    if value is None:
        return FinancialConsolidationScope.UNKNOWN
    normalized = value.strip().casefold()
    if normalized in _CONSOLIDATED_TYPES:
        return FinancialConsolidationScope.CONSOLIDATED
    if normalized in _PARENT_TYPES:
        return FinancialConsolidationScope.PARENT
    return FinancialConsolidationScope.UNKNOWN


def _currency(value: str | None) -> str:
    if value is None:
        return "XXX"
    normalized = value.strip().upper()
    if normalized in _CNY_ALIASES:
        return "CNY"
    return normalized if re.fullmatch(r"[A-Z]{3}", normalized) else "XXX"


def _populated_alias(
    row: Mapping[str, object],
    aliases: Sequence[str],
) -> tuple[str, object] | None:
    for alias in aliases:
        value = row.get(alias)
        if _has_value(value):
            return alias, value
    return None


def _first_value(row: Mapping[str, object], *fields: str) -> object:
    for field in fields:
        if field in row:
            return row[field]
    return None


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


def _decimal(value: object) -> Decimal:
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("AKShare-Sina monetary value is malformed") from exc
    if not numeric.is_finite():
        raise ValueError("AKShare-Sina monetary value is malformed")
    return numeric


def _provider_date(value: object, *, required: bool) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _optional_text(value)
    if text is None:
        if required:
            raise ValueError("AKShare-Sina statement date is missing")
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError("AKShare-Sina statement date is malformed") from exc


__all__ = ["AkshareSinaStatementRowRejection", "project_candidates"]

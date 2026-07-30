"""Closed provider-neutral contracts for acquired financial periods."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    InstrumentIdentityEvidence,
)

FINANCIAL_CONTRACT_VERSION = "1.0"
FINANCIAL_PROVIDER_DATASET_IDENTITY_VERSION = "financial-provider-dataset:v1"
FINANCIAL_PROVIDER_ARTIFACT_IDENTITY_VERSION = "financial-provider-artifact:v1"
FINANCIAL_PERIOD_IDENTITY_VERSION = "financial-period:v1"
FINANCIAL_RATIO_DECLARATION_V1 = "financial-ratio-declarations-v1"
FINANCIAL_RATIO_DECLARATION_V2 = "financial-ratio-declarations-v2"
FINANCIAL_PERIOD_SELECTION_V1 = "financial-period-selection-v1"
FINANCIAL_PERIOD_SELECTION_V2 = "financial-period-selection-v2"

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_UNORDERED_REQUEST_KEYS = frozenset({"fields", "field_set", "requested_fields"})
_UNORDERED_PAYLOAD_KEYS = frozenset({"data", "records", "rows"})
_UNSAFE_CONTRACT_TEXT = (
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "client_secret",
    "credential",
    "password",
    "raw_error",
    "secret",
    "sk-",
    "token=",
    "traceback",
)
_SECRET_IDENTITY_KEY_MARKERS = (
    "access_key",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "secret",
    "token",
)
_NONMATERIAL_IDENTITY_KEYS = frozenset(
    {
        "analysis",
        "attempt_order",
        "call_index",
        "call_order",
        "commentary",
        "generated_text",
        "model_prose",
        "narrative",
        "pid",
        "process_id",
        "prompt",
        "provider_order",
        "reasoning",
        "response_text",
        "sequence_number",
        "thread_id",
        "worker_id",
    }
)
_CLASSIFIER_METADATA_KEYS = frozenset(
    {
        "company_type",
        "comp_type",
        "industry",
        "industry_name",
        "provider_company_type",
        "provider_industry",
        "sector",
        "sector_name",
    }
)


def _assert_safe_contract_payload(value: object) -> None:
    if isinstance(value, BaseModel):
        _assert_safe_contract_payload(value.model_dump(mode="json"))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_safe_contract_payload(str(key))
            _assert_safe_contract_payload(item)
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            _assert_safe_contract_payload(item)
        return
    if isinstance(value, str) and any(
        marker in value.casefold() for marker in _UNSAFE_CONTRACT_TEXT
    ):
        raise ValueError("financial contract text contains unsafe operational data")


def _project_identity_material(value: object) -> object:
    """Remove operational/secret fields before canonical identity hashing."""

    if isinstance(value, BaseModel):
        return _project_identity_material(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        projected: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in _NONMATERIAL_IDENTITY_KEYS or any(
                marker in normalized_key for marker in _SECRET_IDENTITY_KEY_MARKERS
            ):
                continue
            projected[str(key)] = _project_identity_material(item)
        return projected
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_project_identity_material(item) for item in value]
    if isinstance(value, set | frozenset):
        projected_items = [_project_identity_material(item) for item in value]
        return sorted(projected_items, key=_canonical_json)
    return value


def _canonicalize_identity_data(
    value: object,
    *,
    sort_sequence: bool = False,
    unordered_mapping_keys: frozenset[str] = frozenset(),
) -> object:
    if isinstance(value, BaseModel):
        return _canonicalize_identity_data(
            value.model_dump(mode="json"),
            sort_sequence=sort_sequence,
            unordered_mapping_keys=unordered_mapping_keys,
        )
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize_identity_data(
                item,
                sort_sequence=str(key).casefold() in unordered_mapping_keys,
                unordered_mapping_keys=unordered_mapping_keys,
            )
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        normalized = [
            _canonicalize_identity_data(
                item,
                unordered_mapping_keys=unordered_mapping_keys,
            )
            for item in value
        ]
        return sorted(normalized, key=_canonical_json) if sort_sequence else normalized
    if isinstance(value, set | frozenset):
        normalized = [
            _canonicalize_identity_data(
                item,
                unordered_mapping_keys=unordered_mapping_keys,
            )
            for item in value
        ]
        return sorted(normalized, key=_canonical_json)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_digest(
    value: object,
    *,
    unordered_top_level_sequence: bool = False,
    unordered_mapping_keys: frozenset[str] = frozenset(),
) -> str:
    canonical = _canonicalize_identity_data(
        value,
        sort_sequence=unordered_top_level_sequence,
        unordered_mapping_keys=unordered_mapping_keys,
    )
    return sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("financial observation time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FinancialCapability(str, Enum):
    STATEMENT = "financial_statement"
    RATIO_FAMILY = "financial_ratio_family"


class FinancialStatementType(str, Enum):
    COMPREHENSIVE_FUNDAMENTALS = "comprehensive_fundamentals"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    INCOME_STATEMENT = "income_statement"


class FinancialReportingFrequency(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    ANNUAL = "annual"
    QUARTERLY = "quarterly"


class FinancialRatioFamily(str, Enum):
    PROFIT = "profit"
    OPERATION = "operation"
    GROWTH = "growth"
    BALANCE = "balance"
    CASH_FLOW = "cash_flow"
    DUPONT = "dupont"


class FinancialCompanyType(str, Enum):
    BANK = "bank"
    INDUSTRIAL_NON_BANK = "industrial/non-bank"
    INSURANCE = "insurance"
    SECURITIES = "securities"
    OTHER_FINANCIAL = "other-financial"
    UNKNOWN = "unknown"


class FinancialConsolidationScope(str, Enum):
    CONSOLIDATED = "consolidated"
    PARENT = "parent"
    UNKNOWN = "unknown"


class FinancialCompanyTypeResolutionMethod(str, Enum):
    QUALIFIED_PROVIDER = "qualified_provider"
    CLASSIFIER = "classifier"
    UNRESOLVED = "unresolved"


class FinancialPeriodDisposition(str, Enum):
    ACCEPTED = "accepted"
    CURRENT_ONLY = "current_only"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"
    UNSELECTED = "unselected"


class FinancialPeriodRejectionReason(str, Enum):
    PERMISSION_DENIED = "permission_denied"
    INSUFFICIENT_COMPLETENESS = "insufficient_completeness"
    INCOMPATIBLE_METADATA = "incompatible_metadata"
    CAPABILITY_MISMATCH = "capability_mismatch"
    MISSING_FIRST_PUBLICATION_DATE = "missing_first_publication_date"
    PIT_CUTOFF_NOT_SATISFIED = "pit_cutoff_not_satisfied"
    UNSTABLE_REVISION_IDENTITY = "unstable_revision_identity"
    CONFLICTING_CRITICAL_VALUES = "conflicting_critical_values"
    UNSUPPORTED_SINCE_LISTING_COVERAGE = "unsupported_since_listing_coverage"
    INCOMPATIBLE_UNIT = "incompatible_unit"
    INCOMPATIBLE_CURRENCY = "incompatible_currency"
    INCOMPATIBLE_CONSOLIDATION_SCOPE = "incompatible_consolidation_scope"
    AMBIGUOUS_COMPANY_TYPE = "ambiguous_company_type"


class FinancialCompanyTypeClassifierInput(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    key: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _validate_safe_input(self) -> FinancialCompanyTypeClassifierInput:
        _assert_safe_contract_payload((self.key, self.value))
        return self


class FinancialCompanyTypeResolution(BaseModel):
    """Evidence-bearing result of deterministic company-type resolution."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    company_type: FinancialCompanyType
    method: FinancialCompanyTypeResolutionMethod
    provider_declared_type: FinancialCompanyType | None = None
    provider_declaration_qualified: bool = False
    classifier_version: Literal["financial-company-type-classifier-v1"] = (
        "financial-company-type-classifier-v1"
    )
    classifier_inputs: tuple[FinancialCompanyTypeClassifierInput, ...]
    candidate_types: tuple[FinancialCompanyType, ...]
    ambiguous: bool
    contradictory: bool

    @model_validator(mode="after")
    def _validate_resolution(self) -> FinancialCompanyTypeResolution:
        canonical_candidates = tuple(
            company_type
            for company_type in FinancialCompanyType
            if company_type in self.candidate_types
        )
        if self.candidate_types != canonical_candidates:
            raise ValueError("company-type candidates must be unique and canonical")
        if self.classifier_inputs != tuple(
            sorted(self.classifier_inputs, key=lambda item: (item.key, item.value))
        ):
            raise ValueError("company-type classifier inputs must be canonical")
        if len(self.classifier_inputs) != len(set(self.classifier_inputs)):
            raise ValueError("company-type classifier inputs must be unique")
        for classifier_input in self.classifier_inputs:
            if classifier_input.key == "present_field":
                if not classifier_input.value.replace("_", "").isalnum():
                    raise ValueError("company-type classifier field input is invalid")
            elif classifier_input.key.startswith("metadata:"):
                if (
                    classifier_input.key.removeprefix("metadata:")
                    not in _CLASSIFIER_METADATA_KEYS
                    or classifier_input.value not in _CLASSIFIER_HINT_VALUES
                ):
                    raise ValueError("company-type classifier metadata is invalid")
            else:
                raise ValueError("company-type classifier input key is invalid")
        if self.method is FinancialCompanyTypeResolutionMethod.QUALIFIED_PROVIDER:
            if (
                not self.provider_declaration_qualified
                or self.provider_declared_type in {None, FinancialCompanyType.UNKNOWN}
                or self.company_type is not self.provider_declared_type
            ):
                raise ValueError(
                    "qualified provider resolution requires its exact declaration"
                )
            expected_contradictory = bool(
                set(self.candidate_types) - {self.provider_declared_type}
            )
            if (
                self.contradictory != expected_contradictory
                or self.ambiguous != expected_contradictory
            ):
                raise ValueError("qualified provider contradiction state is invalid")
        elif self.method is FinancialCompanyTypeResolutionMethod.CLASSIFIER:
            if (
                self.company_type is FinancialCompanyType.UNKNOWN
                or self.candidate_types != (self.company_type,)
                or self.ambiguous
                or self.contradictory
            ):
                raise ValueError("classifier resolution requires one supported type")
        elif (
            self.company_type is not FinancialCompanyType.UNKNOWN
            or len(self.candidate_types) == 1
            or not self.ambiguous
            or self.contradictory != (len(self.candidate_types) > 1)
        ):
            raise ValueError("unresolved company type state is invalid")
        return self


_COMPANY_TYPE_HINTS = {
    FinancialCompanyType.BANK: frozenset({"bank", "commercial_bank", "银行"}),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: frozenset(
        {"industrial", "manufacturing", "non_bank", "non-bank", "工业"}
    ),
    FinancialCompanyType.INSURANCE: frozenset({"insurance", "insurer", "保险"}),
    FinancialCompanyType.SECURITIES: frozenset(
        {"securities", "brokerage", "broker_dealer", "证券"}
    ),
    FinancialCompanyType.OTHER_FINANCIAL: frozenset(
        {"other_financial", "finance_company", "financial_services"}
    ),
}
_CLASSIFIER_HINT_VALUES = frozenset().union(*_COMPANY_TYPE_HINTS.values())

_COMPANY_TYPE_SIGNATURES = {
    FinancialCompanyType.BANK: frozenset(
        {"customer_deposits", "loans_and_advances", "net_interest_income"}
    ),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: frozenset(
        {"inventory", "accounts_receivable", "cost_of_revenue"}
    ),
    FinancialCompanyType.INSURANCE: frozenset(
        {"insurance_contract_liabilities", "premiums_earned", "claims_expense"}
    ),
    FinancialCompanyType.SECURITIES: frozenset(
        {
            "customer_brokerage_deposits",
            "brokerage_commission_income",
            "financial_assets_bought_for_resale",
        }
    ),
    FinancialCompanyType.OTHER_FINANCIAL: frozenset(
        {"interest_bearing_assets", "customer_financing", "financial_service_revenue"}
    ),
}


def resolve_financial_company_type(
    *,
    provider_declared_type: FinancialCompanyType | None,
    provider_declaration_qualified: bool,
    classifier_metadata: Mapping[str, object],
    present_fields: Sequence[str],
) -> FinancialCompanyTypeResolution:
    """Prefer qualified declarations while detecting contradictory signatures."""

    normalized_metadata: dict[str, str] = {}
    for key, value in classifier_metadata.items():
        normalized_key = str(key).strip().casefold()
        if normalized_key not in _CLASSIFIER_METADATA_KEYS:
            raise ValueError("unsupported financial company-type classifier metadata")
        normalized_value = str(value).strip().casefold().replace(" ", "_")
        if normalized_value not in _CLASSIFIER_HINT_VALUES:
            raise ValueError("unsupported financial company-type classifier value")
        normalized_metadata[normalized_key] = normalized_value
    normalized_present_fields = {
        str(field).strip() for field in present_fields if str(field).strip()
    }
    if any(
        not field.replace("_", "").isalnum()
        for field in normalized_present_fields
    ):
        raise ValueError("company-type classifier field input is invalid")

    inputs = tuple(
        sorted(
            (
                *(
                    FinancialCompanyTypeClassifierInput(
                        key=f"metadata:{key}",
                        value=value,
                    )
                    for key, value in normalized_metadata.items()
                ),
                *(
                    FinancialCompanyTypeClassifierInput(
                        key="present_field",
                        value=str(field),
                    )
                    for field in normalized_present_fields
                ),
            ),
            key=lambda item: (item.key, item.value),
        )
    )
    normalized_hints = set(normalized_metadata.values())
    normalized_fields = normalized_present_fields
    candidate_set = {
        company_type
        for company_type in FinancialCompanyType
        if company_type is not FinancialCompanyType.UNKNOWN
        and (
            bool(normalized_hints & _COMPANY_TYPE_HINTS[company_type])
            or bool(normalized_fields & _COMPANY_TYPE_SIGNATURES[company_type])
        )
    }
    candidates = tuple(
        company_type
        for company_type in FinancialCompanyType
        if company_type in candidate_set
    )
    qualified_declared = (
        provider_declaration_qualified
        and provider_declared_type is not None
        and provider_declared_type is not FinancialCompanyType.UNKNOWN
    )
    if qualified_declared:
        assert provider_declared_type is not None
        contradictory = bool(candidate_set - {provider_declared_type})
        return FinancialCompanyTypeResolution(
            company_type=provider_declared_type,
            method=FinancialCompanyTypeResolutionMethod.QUALIFIED_PROVIDER,
            provider_declared_type=provider_declared_type,
            provider_declaration_qualified=True,
            classifier_inputs=inputs,
            candidate_types=candidates,
            ambiguous=contradictory,
            contradictory=contradictory,
        )
    if len(candidates) == 1:
        return FinancialCompanyTypeResolution(
            company_type=candidates[0],
            method=FinancialCompanyTypeResolutionMethod.CLASSIFIER,
            provider_declared_type=provider_declared_type,
            provider_declaration_qualified=provider_declaration_qualified,
            classifier_inputs=inputs,
            candidate_types=candidates,
            ambiguous=False,
            contradictory=False,
        )
    return FinancialCompanyTypeResolution(
        company_type=FinancialCompanyType.UNKNOWN,
        method=FinancialCompanyTypeResolutionMethod.UNRESOLVED,
        provider_declared_type=provider_declared_type,
        provider_declaration_qualified=provider_declaration_qualified,
        classifier_inputs=inputs,
        candidate_types=candidates,
        ambiguous=True,
        contradictory=len(candidates) > 1,
    )


class FinancialFieldSetDeclaration(BaseModel):
    """Static qualified field declaration; never derived from a response."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    declaration_version: Literal["financial-field-declarations-v1"] = (
        "financial-field-declarations-v1"
    )
    company_type: FinancialCompanyType
    statement_type: FinancialStatementType
    critical_fields: tuple[str, ...]
    core_fields: tuple[str, ...]
    normalized_unit: Literal["CNY_BASE_UNIT"] = "CNY_BASE_UNIT"

    @model_validator(mode="after")
    def _validate_declaration(self) -> FinancialFieldSetDeclaration:
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("unknown company type has no normalized field declaration")
        if self.statement_type is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS:
            raise ValueError("comprehensive fundamentals is not a full statement")
        if tuple(sorted(set(self.critical_fields))) != self.critical_fields:
            raise ValueError("critical fields must be unique and canonical")
        if tuple(sorted(set(self.core_fields))) != self.core_fields:
            raise ValueError("core fields must be unique and canonical")
        if not set(self.critical_fields) <= set(self.core_fields):
            raise ValueError("critical fields must be part of the declared core")
        return self


_BALANCE_CORES = {
    FinancialCompanyType.BANK: (
        ("customer_deposits", "loans_and_advances", "total_assets", "total_equity", "total_liabilities"),
        ("cash_and_central_bank", "customer_deposits", "financial_investments", "interbank_assets", "interbank_liabilities", "loan_loss_allowance", "loans_and_advances", "total_assets", "total_equity", "total_liabilities"),
    ),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: (
        ("total_assets", "total_equity", "total_liabilities"),
        ("accounts_payable", "accounts_receivable", "cash_and_equivalents", "inventory", "long_term_debt", "property_plant_equipment", "short_term_debt", "total_assets", "total_equity", "total_liabilities"),
    ),
    FinancialCompanyType.INSURANCE: (
        ("insurance_contract_liabilities", "total_assets", "total_equity", "total_liabilities"),
        ("cash_and_equivalents", "claims_payable", "insurance_contract_liabilities", "investment_assets", "policyholder_deposits", "premium_receivables", "reinsurance_assets", "total_assets", "total_equity", "total_liabilities"),
    ),
    FinancialCompanyType.SECURITIES: (
        ("customer_brokerage_deposits", "total_assets", "total_equity", "total_liabilities"),
        ("cash_and_equivalents", "customer_brokerage_deposits", "derivative_assets", "financial_assets_bought_for_resale", "repurchase_liabilities", "settlement_reserves", "total_assets", "total_equity", "total_liabilities", "trading_financial_assets"),
    ),
    FinancialCompanyType.OTHER_FINANCIAL: (
        ("interest_bearing_assets", "total_assets", "total_equity", "total_liabilities"),
        ("cash_and_equivalents", "customer_financing", "financial_investments", "interest_bearing_assets", "interest_bearing_liabilities", "loan_loss_allowance", "other_financial_assets", "total_assets", "total_equity", "total_liabilities"),
    ),
}

_INCOME_CORES = {
    FinancialCompanyType.BANK: (
        ("interest_expense", "interest_income", "net_income", "net_interest_income"),
        ("credit_impairment_loss", "fee_commission_income", "income_tax_expense", "interest_expense", "interest_income", "investment_income", "net_income", "net_interest_income", "operating_expense", "operating_profit"),
    ),
    FinancialCompanyType.INDUSTRIAL_NON_BANK: (
        ("net_income", "operating_profit", "operating_revenue"),
        ("cost_of_revenue", "finance_costs", "gross_profit", "income_tax_expense", "net_income", "operating_expense", "operating_profit", "operating_revenue", "other_income", "research_development_expense"),
    ),
    FinancialCompanyType.INSURANCE: (
        ("claims_expense", "net_income", "premiums_earned", "underwriting_result"),
        ("claims_expense", "fee_income", "income_tax_expense", "investment_income", "net_income", "operating_expense", "premiums_earned", "reinsurance_result", "underwriting_expense", "underwriting_result"),
    ),
    FinancialCompanyType.SECURITIES: (
        ("brokerage_commission_income", "net_income", "operating_revenue", "trading_income"),
        ("asset_management_income", "brokerage_commission_income", "income_tax_expense", "interest_income", "investment_banking_income", "investment_income", "net_income", "operating_expense", "operating_revenue", "trading_income"),
    ),
    FinancialCompanyType.OTHER_FINANCIAL: (
        ("financial_service_revenue", "interest_income", "net_income", "operating_profit"),
        ("credit_impairment_loss", "fee_income", "financial_service_revenue", "income_tax_expense", "interest_expense", "interest_income", "investment_income", "net_income", "operating_expense", "operating_profit"),
    ),
}

_CASH_FLOW_COMMON = (
    "cash_from_financing",
    "cash_from_investing",
    "cash_from_operations",
    "cash_paid_for_interest",
    "cash_paid_for_taxes",
    "cash_received_from_customers",
    "closing_cash",
    "effect_of_exchange_rates",
    "net_change_in_cash",
    "opening_cash",
)


def financial_statement_field_declaration(
    company_type: FinancialCompanyType,
    statement_type: FinancialStatementType,
) -> FinancialFieldSetDeclaration:
    if statement_type is FinancialStatementType.BALANCE_SHEET:
        critical, core = _BALANCE_CORES[company_type]
    elif statement_type is FinancialStatementType.INCOME_STATEMENT:
        critical, core = _INCOME_CORES[company_type]
    elif statement_type is FinancialStatementType.CASH_FLOW:
        critical = ("cash_from_operations", "closing_cash", "net_change_in_cash")
        core = _CASH_FLOW_COMMON
    else:
        raise ValueError("full-statement declaration requires a statement type")
    return FinancialFieldSetDeclaration(
        company_type=company_type,
        statement_type=statement_type,
        critical_fields=tuple(sorted(critical)),
        core_fields=tuple(sorted(core)),
    )


class FinancialRatioFieldSetDeclaration(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    declaration_version: Literal[
        "financial-ratio-declarations-v1",
        "financial-ratio-declarations-v2",
    ] = FINANCIAL_RATIO_DECLARATION_V1
    company_type: FinancialCompanyType
    ratio_family: FinancialRatioFamily
    normalized_fields: tuple[str, ...]
    normalized_unit: Literal["RATIO"] = "RATIO"
    maximum_missingness: Decimal = Decimal("0.1")

    @model_validator(mode="after")
    def _validate_declaration(self) -> FinancialRatioFieldSetDeclaration:
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("unknown company type has no normalized ratio declaration")
        if tuple(sorted(set(self.normalized_fields))) != self.normalized_fields:
            raise ValueError("ratio fields must be unique and canonical")
        if not self.normalized_fields:
            raise ValueError("ratio declaration cannot be empty")
        return self


_RATIO_FAMILY_FIELDS = {
    FinancialRatioFamily.PROFIT: (
        "earnings_per_share",
        "ebitda_margin",
        "gross_margin",
        "net_margin",
        "operating_margin",
        "pre_tax_margin",
        "profit_to_cost",
        "return_on_assets",
        "return_on_equity",
        "return_on_invested_capital",
    ),
    FinancialRatioFamily.OPERATION: (
        "accounts_payable_turnover",
        "asset_turnover",
        "cash_conversion_cycle",
        "current_asset_turnover",
        "fixed_asset_turnover",
        "inventory_days",
        "inventory_turnover",
        "operating_cycle",
        "receivable_days",
        "receivables_turnover",
    ),
    FinancialRatioFamily.GROWTH: (
        "asset_growth",
        "cash_flow_growth",
        "equity_growth",
        "gross_profit_growth",
        "net_income_growth",
        "operating_profit_growth",
        "revenue_growth",
        "return_on_equity_growth",
        "total_profit_growth",
        "working_capital_growth",
    ),
    FinancialRatioFamily.BALANCE: (
        "cash_ratio",
        "current_ratio",
        "debt_to_assets",
        "debt_to_equity",
        "equity_multiplier",
        "interest_coverage",
        "long_term_debt_ratio",
        "quick_ratio",
        "tangible_asset_ratio",
        "working_capital_ratio",
    ),
    FinancialRatioFamily.CASH_FLOW: (
        "cash_flow_adequacy",
        "cash_flow_to_debt",
        "cash_flow_to_liabilities",
        "cash_flow_to_revenue",
        "cash_return_on_assets",
        "free_cash_flow_margin",
        "operating_cash_flow_ratio",
        "quality_of_income",
        "reinvestment_ratio",
        "self_financing_ratio",
    ),
    FinancialRatioFamily.DUPONT: (
        "asset_turnover_component",
        "debt_burden",
        "equity_multiplier_component",
        "financial_leverage",
        "interest_burden",
        "net_profit_margin_component",
        "operating_margin_component",
        "return_on_assets_component",
        "return_on_equity_component",
        "tax_burden",
    ),
}

_QUALIFIED_RATIO_FAMILY_FIELDS = {
    **_RATIO_FAMILY_FIELDS,
    FinancialRatioFamily.PROFIT: tuple(
        field
        for field in _RATIO_FAMILY_FIELDS[FinancialRatioFamily.PROFIT]
        if field != "earnings_per_share"
    ),
    FinancialRatioFamily.OPERATION: (
        "accounts_payable_turnover",
        "asset_turnover",
        "current_asset_turnover",
        "fixed_asset_turnover",
        "inventory_turnover",
        "receivables_turnover",
    ),
}

_QUALIFIED_BANK_RATIO_FAMILY_FIELDS = {
    **_QUALIFIED_RATIO_FAMILY_FIELDS,
    FinancialRatioFamily.PROFIT: (
        "cost_income_ratio",
        "net_interest_margin",
        "net_interest_spread",
        "net_margin",
        "non_performing_loan_ratio",
        "pre_tax_margin",
        "provision_coverage_ratio",
        "return_on_assets",
        "return_on_equity",
        "return_on_invested_capital",
    ),
}


def financial_ratio_field_declaration(
    company_type: FinancialCompanyType,
    ratio_family: FinancialRatioFamily,
    *,
    declaration_version: Literal[
        "financial-ratio-declarations-v1",
        "financial-ratio-declarations-v2",
    ] = FINANCIAL_RATIO_DECLARATION_V1,
) -> FinancialRatioFieldSetDeclaration:
    if declaration_version == FINANCIAL_RATIO_DECLARATION_V1:
        normalized_fields = _RATIO_FAMILY_FIELDS[ratio_family]
    elif declaration_version == FINANCIAL_RATIO_DECLARATION_V2:
        declarations = (
            _QUALIFIED_BANK_RATIO_FAMILY_FIELDS
            if company_type is FinancialCompanyType.BANK
            else _QUALIFIED_RATIO_FAMILY_FIELDS
        )
        normalized_fields = declarations[ratio_family]
    else:
        raise ValueError("unsupported financial ratio declaration version")
    return FinancialRatioFieldSetDeclaration(
        declaration_version=declaration_version,
        company_type=company_type,
        ratio_family=ratio_family,
        normalized_fields=tuple(sorted(normalized_fields)),
    )


class FinancialFieldValue(BaseModel):
    """Original provider value paired with its explicit normalized value."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    provider_field: str = Field(pattern=_SAFE_ID_PATTERN)
    original_value: str = Field(min_length=1, max_length=512)
    original_unit: str | None = Field(default=None, max_length=64)
    normalized_field: str = Field(pattern=_SAFE_ID_PATTERN)
    normalized_value: str | None = Field(default=None, max_length=512)
    normalized_unit: str | None = Field(default=None, max_length=64)

    @field_validator("normalized_value")
    @classmethod
    def _validate_normalized_numeric_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            numeric = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("normalized financial value must be decimal") from exc
        if not numeric.is_finite():
            raise ValueError("normalized financial value must be finite")
        return value

    @model_validator(mode="after")
    def _validate_safe_value(self) -> FinancialFieldValue:
        _assert_safe_contract_payload(self.model_dump(mode="json"))
        return self


class FinancialFilingMetadata(BaseModel):
    """Provider filing metadata with independent announcement/publication dates."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    ann_date: date | None = None
    f_ann_date: date | None = None
    report_type: str | None = Field(default=None, max_length=64)
    comp_type: str | None = Field(default=None, max_length=64)
    update_flag: str | None = Field(default=None, max_length=64)
    retrieved_at: datetime
    observed_at: datetime
    local_provider_revision_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_filing_revision_id: str | None = Field(
        default=None,
        pattern=_SAFE_ID_PATTERN,
    )

    @field_validator("retrieved_at", "observed_at")
    @classmethod
    def _require_concrete_utc_time(cls, value: datetime) -> datetime:
        _utc_text(value)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_safe_metadata(self) -> FinancialFilingMetadata:
        _assert_safe_contract_payload(self.model_dump(mode="json"))
        if (
            self.update_flag in {"0", "1"}
            and self.provider_filing_revision_id == self.update_flag
        ):
            raise ValueError("binary update_flag cannot establish restatement lineage")
        return self

    def is_strict_pit_eligible(self, *, as_of_date: date) -> bool:
        return (
            self.f_ann_date is not None
            and self.f_ann_date <= as_of_date
            and self.observed_at.date() <= as_of_date
        )

    @property
    def provider_restatement_lineage_established(self) -> bool:
        return self.provider_filing_revision_id is not None


def _authoritative_instrument_payload(
    identity: InstrumentIdentityEvidence,
) -> dict[str, object]:
    if not identity.is_authoritative or identity.provenance is None:
        raise ValueError("Financial Period Identity requires authoritative Instrument Identity")
    payload = {
        "canonical_symbol": identity.symbol.strip().upper(),
        "venue": identity.venue,
        "instrument_kind": identity.instrument_kind.value,
        "currency": identity.currency.upper(),
        "provenance": identity.provenance.model_dump(mode="json"),
    }
    _assert_safe_contract_payload(payload)
    return payload


def _financial_period_identity_payload(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    capability: FinancialCapability,
    statement_type: FinancialStatementType | None,
    ratio_family: FinancialRatioFamily | None,
    period_end: date,
    frequency: FinancialReportingFrequency,
    company_type: FinancialCompanyType,
    consolidation_scope: FinancialConsolidationScope,
    currency: str,
    provider_revision_binding: str,
) -> dict[str, object]:
    return {
        "contract_version": FINANCIAL_CONTRACT_VERSION,
        "instrument_identity": _authoritative_instrument_payload(instrument_identity),
        "capability": capability.value,
        "statement_type": statement_type.value if statement_type is not None else None,
        "ratio_family": ratio_family.value if ratio_family is not None else None,
        "period_end": period_end.isoformat(),
        "frequency": frequency.value,
        "company_type": company_type.value,
        "consolidation_scope": consolidation_scope.value,
        "currency": currency.upper(),
        "provider_revision_binding": provider_revision_binding,
    }


class FinancialPeriodIdentity(BaseModel):
    """Canonical provider-revision-bound identity of one financial period."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    instrument_identity: InstrumentIdentityEvidence
    capability: FinancialCapability
    statement_type: FinancialStatementType | None = None
    ratio_family: FinancialRatioFamily | None = None
    period_end: date
    frequency: FinancialReportingFrequency
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    provider_revision_binding: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        capability: FinancialCapability,
        statement_type: FinancialStatementType | None,
        ratio_family: FinancialRatioFamily | None,
        period_end: date,
        frequency: FinancialReportingFrequency,
        company_type: FinancialCompanyType,
        consolidation_scope: FinancialConsolidationScope,
        currency: str,
        provider_revision_binding: str,
    ) -> FinancialPeriodIdentity:
        payload = _financial_period_identity_payload(
            instrument_identity=instrument_identity,
            capability=capability,
            statement_type=statement_type,
            ratio_family=ratio_family,
            period_end=period_end,
            frequency=frequency,
            company_type=company_type,
            consolidation_scope=consolidation_scope,
            currency=currency,
            provider_revision_binding=provider_revision_binding,
        )
        return cls(
            instrument_identity=instrument_identity,
            capability=capability,
            statement_type=statement_type,
            ratio_family=ratio_family,
            period_end=period_end,
            frequency=frequency,
            company_type=company_type,
            consolidation_scope=consolidation_scope,
            currency=currency.upper(),
            provider_revision_binding=provider_revision_binding,
            period_identity=(
                f"{FINANCIAL_PERIOD_IDENTITY_VERSION}:{_canonical_digest(payload)}"
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialPeriodIdentity:
        if self.capability is FinancialCapability.STATEMENT:
            if self.statement_type not in {
                FinancialStatementType.BALANCE_SHEET,
                FinancialStatementType.CASH_FLOW,
                FinancialStatementType.INCOME_STATEMENT,
            } or self.ratio_family is not None:
                raise ValueError("statement period identity requires one full statement")
        elif self.statement_type is not None or self.ratio_family is None:
            raise ValueError("ratio period identity requires one ratio family")
        if self.frequency is FinancialReportingFrequency.NOT_APPLICABLE:
            raise ValueError("financial periods require annual or quarterly frequency")
        payload = _financial_period_identity_payload(
            instrument_identity=self.instrument_identity,
            capability=self.capability,
            statement_type=self.statement_type,
            ratio_family=self.ratio_family,
            period_end=self.period_end,
            frequency=self.frequency,
            company_type=self.company_type,
            consolidation_scope=self.consolidation_scope,
            currency=self.currency,
            provider_revision_binding=self.provider_revision_binding,
        )
        expected = f"{FINANCIAL_PERIOD_IDENTITY_VERSION}:{_canonical_digest(payload)}"
        if self.period_identity != expected:
            raise ValueError("Financial Period Identity mismatch")
        return self


class FinancialProviderDatasetIdentity(BaseModel):
    """Stable identity of one provider endpoint/dataset schema."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    endpoint_id: str = Field(pattern=_SAFE_ID_PATTERN)
    dataset_id: str = Field(pattern=_SAFE_ID_PATTERN)
    schema_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    dataset_identity: str = Field(
        pattern=r"^financial-provider-dataset:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        endpoint_id: str,
        dataset_id: str,
        schema_identity: str,
    ) -> FinancialProviderDatasetIdentity:
        payload = {
            "contract_version": FINANCIAL_CONTRACT_VERSION,
            "provider_id": provider_id,
            "endpoint_id": endpoint_id,
            "dataset_id": dataset_id,
            "schema_identity": schema_identity,
        }
        return cls(
            **payload,
            dataset_identity=(
                f"{FINANCIAL_PROVIDER_DATASET_IDENTITY_VERSION}:"
                f"{_canonical_digest(payload)}"
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialProviderDatasetIdentity:
        payload = self.model_dump(mode="json", exclude={"dataset_identity"})
        _assert_safe_contract_payload(payload)
        expected = (
            f"{FINANCIAL_PROVIDER_DATASET_IDENTITY_VERSION}:"
            f"{_canonical_digest(payload)}"
        )
        if self.dataset_identity != expected:
            raise ValueError("financial provider dataset identity mismatch")
        return self


class FinancialProviderArtifactIdentity(BaseModel):
    """Immutable local revision binding without retaining a provider payload."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    dataset: FinancialProviderDatasetIdentity
    canonical_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_metadata_sha256: str = Field(pattern=_SHA256_PATTERN)
    retrieved_at: datetime
    observed_at: datetime
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )

    @field_validator("retrieved_at", "observed_at")
    @classmethod
    def _require_concrete_utc_time(cls, value: datetime) -> datetime:
        _utc_text(value)
        return value.astimezone(timezone.utc)

    @classmethod
    def create(
        cls,
        *,
        dataset: FinancialProviderDatasetIdentity,
        canonical_request: Mapping[str, object],
        provider_metadata: Mapping[str, object],
        retrieved_at: datetime,
        observed_at: datetime,
        payload: object,
    ) -> FinancialProviderArtifactIdentity:
        request_identity_material = _project_identity_material(canonical_request)
        metadata_identity_material = _project_identity_material(provider_metadata)
        payload_identity_material = _project_identity_material(payload)
        _assert_safe_contract_payload(request_identity_material)
        _assert_safe_contract_payload(metadata_identity_material)
        _assert_safe_contract_payload(payload_identity_material)
        payload_fields: dict[str, Any] = {
            "contract_version": FINANCIAL_CONTRACT_VERSION,
            "dataset": dataset.model_dump(mode="json"),
            "canonical_request_sha256": _canonical_digest(
                request_identity_material,
                unordered_mapping_keys=_UNORDERED_REQUEST_KEYS,
            ),
            "provider_metadata_sha256": _canonical_digest(
                metadata_identity_material
            ),
            "retrieved_at": _utc_text(retrieved_at),
            "observed_at": _utc_text(observed_at),
            "payload_sha256": _canonical_digest(
                payload_identity_material,
                unordered_top_level_sequence=True,
                unordered_mapping_keys=_UNORDERED_PAYLOAD_KEYS,
            ),
        }
        return cls(
            **payload_fields,
            artifact_identity=(
                f"{FINANCIAL_PROVIDER_ARTIFACT_IDENTITY_VERSION}:"
                f"{_canonical_digest(payload_fields)}"
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialProviderArtifactIdentity:
        payload = self.model_dump(mode="json", exclude={"artifact_identity"})
        expected = (
            f"{FINANCIAL_PROVIDER_ARTIFACT_IDENTITY_VERSION}:"
            f"{_canonical_digest(payload)}"
        )
        if self.artifact_identity != expected:
            raise ValueError("financial provider artifact identity mismatch")
        return self


class FinancialPeriodCandidate(BaseModel):
    """One immutable normalized candidate retained whether accepted or rejected."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    artifact: FinancialProviderArtifactIdentity
    period_identity: FinancialPeriodIdentity
    filing_metadata: FinancialFilingMetadata
    company_type_resolution: FinancialCompanyTypeResolution
    fields: tuple[FinancialFieldValue, ...]
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        artifact: FinancialProviderArtifactIdentity,
        period_identity: FinancialPeriodIdentity,
        filing_metadata: FinancialFilingMetadata,
        company_type_resolution: FinancialCompanyTypeResolution,
        fields: Sequence[FinancialFieldValue],
    ) -> FinancialPeriodCandidate:
        canonical_fields = tuple(
            sorted(fields, key=lambda field: (field.normalized_field, field.provider_field))
        )
        identity_payload = {
            "contract_version": FINANCIAL_CONTRACT_VERSION,
            "artifact_identity": artifact.artifact_identity,
            "period_identity": period_identity.period_identity,
            "filing_metadata": filing_metadata.model_dump(mode="json"),
            "company_type_resolution": company_type_resolution.model_dump(mode="json"),
            "fields": [field.model_dump(mode="json") for field in canonical_fields],
        }
        return cls(
            artifact=artifact,
            period_identity=period_identity,
            filing_metadata=filing_metadata,
            company_type_resolution=company_type_resolution,
            fields=canonical_fields,
            candidate_identity=(
                "financial-period-candidate:v1:"
                + _canonical_digest(identity_payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_candidate(self) -> FinancialPeriodCandidate:
        if self.fields != tuple(
            sorted(
                self.fields,
                key=lambda field: (field.normalized_field, field.provider_field),
            )
        ):
            raise ValueError("financial candidate fields must be canonical")
        normalized_fields = tuple(field.normalized_field for field in self.fields)
        if len(normalized_fields) != len(set(normalized_fields)):
            raise ValueError("financial candidate normalized fields must be unique")
        artifact_identity = self.artifact.artifact_identity
        if self.period_identity.provider_revision_binding != artifact_identity:
            raise ValueError("Financial Period Identity is not bound to its artifact")
        if (
            self.filing_metadata.local_provider_revision_identity
            != artifact_identity
        ):
            raise ValueError("filing metadata is not bound to its local revision")
        if (
            self.company_type_resolution.company_type
            is not self.period_identity.company_type
        ):
            raise ValueError("company-type resolution contradicts period identity")
        identity_payload = {
            "contract_version": FINANCIAL_CONTRACT_VERSION,
            "artifact_identity": artifact_identity,
            "period_identity": self.period_identity.period_identity,
            "filing_metadata": self.filing_metadata.model_dump(mode="json"),
            "company_type_resolution": self.company_type_resolution.model_dump(
                mode="json"
            ),
            "fields": [field.model_dump(mode="json") for field in self.fields],
        }
        expected = "financial-period-candidate:v1:" + _canonical_digest(
            identity_payload
        )
        if self.candidate_identity != expected:
            raise ValueError("financial period candidate identity mismatch")
        return self


class FinancialPeriodCompletenessAssessment(BaseModel):
    """Deterministic hard-gate outcome for one normalized financial period."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-period-selection-v1",
        "financial-period-selection-v2",
    ] = FINANCIAL_PERIOD_SELECTION_V1
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    instrument_identity: InstrumentIdentityEvidence
    capability: FinancialCapability
    statement_type: FinancialStatementType | None = None
    ratio_family: FinancialRatioFamily | None = None
    period_end: date
    frequency: FinancialReportingFrequency
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    pit_as_of_date: date
    first_publication_date: date | None = None
    revision_observed_at: datetime
    disposition: FinancialPeriodDisposition
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...]
    declared_critical_fields: tuple[str, ...]
    declared_core_fields: tuple[str, ...]
    present_critical_fields: tuple[str, ...]
    present_core_fields: tuple[str, ...]
    missing_critical_fields: tuple[str, ...]
    missing_core_fields: tuple[str, ...]
    critical_coverage: Decimal = Field(ge=0, le=1)
    core_coverage: Decimal = Field(ge=0, le=1)
    metadata_complete: bool
    usable_for_current_analysis: bool
    strict_pit_eligible: bool

    @field_validator("revision_observed_at")
    @classmethod
    def _require_concrete_utc_time(cls, value: datetime) -> datetime:
        _utc_text(value)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_assessment(self) -> FinancialPeriodCompletenessAssessment:
        _authoritative_instrument_payload(self.instrument_identity)
        canonical_reasons = tuple(
            reason
            for reason in FinancialPeriodRejectionReason
            if reason in self.rejection_reasons
        )
        if self.rejection_reasons != canonical_reasons:
            raise ValueError("financial rejection reasons must be unique and canonical")
        field_sets = (
            self.declared_critical_fields,
            self.declared_core_fields,
            self.present_critical_fields,
            self.present_core_fields,
            self.missing_critical_fields,
            self.missing_core_fields,
        )
        if any(values != tuple(sorted(set(values))) for values in field_sets):
            raise ValueError("financial assessment fields must be unique and canonical")

        expected_critical: tuple[str, ...] = ()
        expected_core: tuple[str, ...] = ()
        ratio_assessment = False
        ratio_contract = (
            self.capability is FinancialCapability.RATIO_FAMILY
            and self.statement_type is None
            and self.ratio_family is not None
        )
        if (
            self.capability is FinancialCapability.STATEMENT
            and self.company_type is not FinancialCompanyType.UNKNOWN
            and self.statement_type
            in {
                FinancialStatementType.BALANCE_SHEET,
                FinancialStatementType.CASH_FLOW,
                FinancialStatementType.INCOME_STATEMENT,
            }
            and self.ratio_family is None
        ):
            declaration = financial_statement_field_declaration(
                self.company_type,
                self.statement_type,
            )
            expected_critical = declaration.critical_fields
            expected_core = declaration.core_fields
        elif (
            self.capability is FinancialCapability.RATIO_FAMILY
            and self.company_type is not FinancialCompanyType.UNKNOWN
            and self.statement_type is None
            and self.ratio_family is not None
        ):
            ratio_assessment = True
            declaration_version = (
                FINANCIAL_RATIO_DECLARATION_V2
                if self.contract_version == FINANCIAL_PERIOD_SELECTION_V2
                else FINANCIAL_RATIO_DECLARATION_V1
            )
            expected_core = financial_ratio_field_declaration(
                self.company_type,
                self.ratio_family,
                declaration_version=declaration_version,
            ).normalized_fields
        elif self.declared_critical_fields or self.declared_core_fields:
            raise ValueError("unsupported assessment cannot declare normalized fields")

        if (
            self.contract_version == FINANCIAL_PERIOD_SELECTION_V2
            and not ratio_contract
        ):
            raise ValueError("financial period selection v2 requires a ratio assessment")

        if (
            self.declared_critical_fields != expected_critical
            or self.declared_core_fields != expected_core
        ):
            raise ValueError("financial assessment field declaration mismatch")
        if (
            set(self.present_critical_fields)
            | set(self.missing_critical_fields)
            != set(expected_critical)
            or set(self.present_critical_fields)
            & set(self.missing_critical_fields)
            or set(self.present_core_fields) | set(self.missing_core_fields)
            != set(expected_core)
            or set(self.present_core_fields) & set(self.missing_core_fields)
        ):
            raise ValueError("financial assessment field partitions are inconsistent")
        if (
            not set(self.present_critical_fields) <= set(self.present_core_fields)
            or not set(self.missing_critical_fields) <= set(self.missing_core_fields)
        ):
            raise ValueError("critical/core presence partitions are inconsistent")
        expected_critical_coverage = (
            Decimal(1)
            if ratio_assessment
            else (
                Decimal(len(self.present_critical_fields))
                / Decimal(len(expected_critical))
                if expected_critical
                else Decimal(0)
            )
        )
        expected_core_coverage = (
            Decimal(len(self.present_core_fields)) / Decimal(len(expected_core))
            if expected_core
            else Decimal(0)
        )
        if (
            self.critical_coverage != expected_critical_coverage
            or self.core_coverage != expected_core_coverage
        ):
            raise ValueError("financial assessment coverage is inconsistent")
        insufficient = bool(
            expected_core
            and (
                (not ratio_assessment and bool(self.missing_critical_fields))
                or self.core_coverage < Decimal("0.9")
            )
        )
        if insufficient != (
            FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS
            in self.rejection_reasons
        ):
            raise ValueError("financial period completeness reason is inconsistent")
        if not self.metadata_complete and (
            FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA
            not in self.rejection_reasons
        ):
            raise ValueError("missing announcement metadata must be rejected")

        expected_strict_pit = (
            self.first_publication_date is not None
            and self.first_publication_date <= self.pit_as_of_date
            and self.revision_observed_at.date() <= self.pit_as_of_date
        )
        if self.strict_pit_eligible != expected_strict_pit:
            raise ValueError("financial strict-PIT eligibility is inconsistent")
        missing_publication = (
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE
            in self.rejection_reasons
        )
        cutoff_failed = (
            FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
            in self.rejection_reasons
        )
        if missing_publication != (self.first_publication_date is None):
            raise ValueError("first-publication rejection is inconsistent")
        if cutoff_failed != (
            self.first_publication_date is not None and not expected_strict_pit
        ):
            raise ValueError("strict-PIT cutoff rejection is inconsistent")

        pit_only_reasons = {
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE,
            FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED,
        }
        reason_set = set(self.rejection_reasons)
        if self.disposition is FinancialPeriodDisposition.ACCEPTED:
            if reason_set or not self.strict_pit_eligible:
                raise ValueError("accepted financial period must pass every hard gate")
        elif self.disposition is FinancialPeriodDisposition.CURRENT_ONLY:
            if not reason_set or not reason_set <= pit_only_reasons:
                raise ValueError("current-only period may fail only strict-PIT gates")
        elif self.disposition is FinancialPeriodDisposition.REJECTED:
            if not reason_set or reason_set <= pit_only_reasons:
                raise ValueError("rejected financial period requires a non-PIT reason")
        elif self.disposition is FinancialPeriodDisposition.CONFLICTED:
            if not reason_set & {
                FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
                FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES,
            }:
                raise ValueError("conflicted period requires a typed conflict")
        else:
            raise ValueError("assessment cannot use unselected disposition")
        if self.usable_for_current_analysis != (
            self.disposition
            in {
                FinancialPeriodDisposition.ACCEPTED,
                FinancialPeriodDisposition.CURRENT_ONLY,
            }
        ):
            raise ValueError("financial current-analysis usability is inconsistent")
        return self


def assess_financial_period_candidate(
    candidate: FinancialPeriodCandidate,
    *,
    requested_capability: FinancialCapability,
    requested_statement_type: FinancialStatementType | None,
    requested_ratio_family: FinancialRatioFamily | None,
    expected_instrument_identity: InstrumentIdentityEvidence,
    expected_frequency: FinancialReportingFrequency,
    expected_currency: str,
    expected_consolidation_scope: FinancialConsolidationScope,
    pit_as_of_date: date,
    ratio_declaration_version: Literal[
        "financial-ratio-declarations-v1",
        "financial-ratio-declarations-v2",
    ] = FINANCIAL_RATIO_DECLARATION_V1,
) -> FinancialPeriodCompletenessAssessment:
    """Apply capability, identity, metadata, and declared completeness gates."""

    identity = candidate.period_identity
    reasons: set[FinancialPeriodRejectionReason] = set()
    if (
        identity.capability is not requested_capability
        or identity.statement_type is not requested_statement_type
        or identity.ratio_family is not requested_ratio_family
    ):
        reasons.add(FinancialPeriodRejectionReason.CAPABILITY_MISMATCH)
    if _authoritative_instrument_payload(
        identity.instrument_identity
    ) != _authoritative_instrument_payload(expected_instrument_identity):
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)
    if identity.frequency is not expected_frequency:
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)
    resolution = candidate.company_type_resolution
    if (
        identity.company_type is FinancialCompanyType.UNKNOWN
        or resolution.ambiguous
        or resolution.contradictory
    ):
        reasons.add(FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE)
    if identity.currency != expected_currency.upper():
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY)
    if identity.consolidation_scope is not expected_consolidation_scope:
        reasons.add(
            FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE
        )
    if candidate.filing_metadata.ann_date is None:
        reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA)

    declared_critical: tuple[str, ...] = ()
    declared_core: tuple[str, ...] = ()
    present_critical: tuple[str, ...] = ()
    present_core: tuple[str, ...] = ()
    missing_critical: tuple[str, ...] = ()
    missing_core: tuple[str, ...] = ()
    critical_coverage = Decimal(0)
    core_coverage = Decimal(0)
    if (
        identity.capability is FinancialCapability.STATEMENT
        and identity.company_type is not FinancialCompanyType.UNKNOWN
        and identity.statement_type
        in {
            FinancialStatementType.BALANCE_SHEET,
            FinancialStatementType.CASH_FLOW,
            FinancialStatementType.INCOME_STATEMENT,
        }
    ):
        assert identity.statement_type is not None
        declaration = financial_statement_field_declaration(
            identity.company_type,
            identity.statement_type,
        )
        declared_critical = declaration.critical_fields
        declared_core = declaration.core_fields
        normalized_fields = {
            field.normalized_field: field
            for field in candidate.fields
            if field.normalized_value is not None
        }
        present_critical = tuple(
            field for field in declared_critical if field in normalized_fields
        )
        present_core = tuple(field for field in declared_core if field in normalized_fields)
        missing_critical = tuple(
            field for field in declared_critical if field not in normalized_fields
        )
        missing_core = tuple(field for field in declared_core if field not in normalized_fields)
        critical_coverage = Decimal(len(present_critical)) / Decimal(
            len(declared_critical)
        )
        core_coverage = Decimal(len(present_core)) / Decimal(len(declared_core))
        if any(
            normalized_fields[field].normalized_unit != declaration.normalized_unit
            for field in present_core
        ):
            reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT)
        if missing_critical or core_coverage < Decimal("0.9"):
            reasons.add(FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS)
    elif (
        identity.capability is FinancialCapability.RATIO_FAMILY
        and identity.company_type is not FinancialCompanyType.UNKNOWN
        and identity.ratio_family is not None
    ):
        ratio_declaration = financial_ratio_field_declaration(
            identity.company_type,
            identity.ratio_family,
            declaration_version=ratio_declaration_version,
        )
        declared_core = ratio_declaration.normalized_fields
        normalized_fields = {
            field.normalized_field: field
            for field in candidate.fields
            if field.normalized_value is not None
        }
        present_core = tuple(field for field in declared_core if field in normalized_fields)
        missing_core = tuple(field for field in declared_core if field not in normalized_fields)
        critical_coverage = Decimal(1)
        core_coverage = Decimal(len(present_core)) / Decimal(len(declared_core))
        if any(
            normalized_fields[field].normalized_unit
            != ratio_declaration.normalized_unit
            for field in present_core
        ):
            reasons.add(FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT)
        if Decimal(1) - core_coverage > ratio_declaration.maximum_missingness:
            reasons.add(FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS)

    revision_observed_at = max(
        candidate.artifact.observed_at,
        candidate.filing_metadata.observed_at,
    )
    first_publication_date = candidate.filing_metadata.f_ann_date
    strict_pit_eligible = (
        first_publication_date is not None
        and first_publication_date <= pit_as_of_date
        and revision_observed_at.date() <= pit_as_of_date
    )
    if first_publication_date is None:
        reasons.add(FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE)
    elif not strict_pit_eligible:
        reasons.add(FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED)
    rejection_reasons = tuple(
        reason for reason in FinancialPeriodRejectionReason if reason in reasons
    )
    current_only_reasons = {
        FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE,
        FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED,
    }
    if not reasons:
        disposition = FinancialPeriodDisposition.ACCEPTED
    elif reasons <= current_only_reasons:
        disposition = FinancialPeriodDisposition.CURRENT_ONLY
    else:
        disposition = FinancialPeriodDisposition.REJECTED
    assessment_contract_version = (
        FINANCIAL_PERIOD_SELECTION_V2
        if (
            identity.capability is FinancialCapability.RATIO_FAMILY
            and ratio_declaration_version == FINANCIAL_RATIO_DECLARATION_V2
        )
        else FINANCIAL_PERIOD_SELECTION_V1
    )
    return FinancialPeriodCompletenessAssessment(
        contract_version=assessment_contract_version,
        candidate_identity=candidate.candidate_identity,
        artifact_identity=candidate.artifact.artifact_identity,
        period_identity=identity.period_identity,
        instrument_identity=identity.instrument_identity,
        capability=identity.capability,
        statement_type=identity.statement_type,
        ratio_family=identity.ratio_family,
        period_end=identity.period_end,
        frequency=identity.frequency,
        company_type=identity.company_type,
        consolidation_scope=identity.consolidation_scope,
        currency=identity.currency,
        pit_as_of_date=pit_as_of_date,
        first_publication_date=first_publication_date,
        revision_observed_at=revision_observed_at,
        disposition=disposition,
        rejection_reasons=rejection_reasons,
        declared_critical_fields=declared_critical,
        declared_core_fields=declared_core,
        present_critical_fields=present_critical,
        present_core_fields=present_core,
        missing_critical_fields=missing_critical,
        missing_core_fields=missing_core,
        critical_coverage=critical_coverage,
        core_coverage=core_coverage,
        metadata_complete=candidate.filing_metadata.ann_date is not None,
        usable_for_current_analysis=disposition
        in {
            FinancialPeriodDisposition.ACCEPTED,
            FinancialPeriodDisposition.CURRENT_ONLY,
        },
        strict_pit_eligible=strict_pit_eligible,
    )


class FinancialListingProvenance(BaseModel):
    """Authoritative listing-date provenance used by the typed exception."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_CONTRACT_VERSION
    authoritative: Literal[True] = True
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    source_ref: str = Field(pattern=_SAFE_ID_PATTERN)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _require_concrete_utc_time(cls, value: datetime) -> datetime:
        _utc_text(value)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_safe_provenance(self) -> FinancialListingProvenance:
        _assert_safe_contract_payload(self.model_dump(mode="json"))
        return self


def _canonical_dates(values: Sequence[date], *, reverse: bool = False) -> tuple[date, ...]:
    return tuple(sorted(set(values), reverse=reverse))


class FinancialSinceListingException(BaseModel):
    """Typed relaxation that still requires every expected post-listing period."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-since-listing-exception-v1"] = (
        "financial-since-listing-exception-v1"
    )
    listing_date: date
    listing_provenance: FinancialListingProvenance
    expected_annual_period_ends: tuple[date, ...]
    expected_reporting_period_ends: tuple[date, ...]
    covered_annual_period_ends: tuple[date, ...]
    covered_reporting_period_ends: tuple[date, ...]
    missing_annual_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    ignored_pre_listing_period_ends: tuple[date, ...]
    supported: bool

    @model_validator(mode="after")
    def _validate_exception(self) -> FinancialSinceListingException:
        date_fields = (
            self.expected_annual_period_ends,
            self.expected_reporting_period_ends,
            self.covered_annual_period_ends,
            self.covered_reporting_period_ends,
            self.missing_annual_period_ends,
            self.missing_reporting_period_ends,
            self.ignored_pre_listing_period_ends,
        )
        if any(values != _canonical_dates(values) for values in date_fields):
            raise ValueError("since-listing period sets must be unique and canonical")
        if any(
            period < self.listing_date
            for period in (
                *self.expected_annual_period_ends,
                *self.expected_reporting_period_ends,
                *self.covered_annual_period_ends,
                *self.covered_reporting_period_ends,
            )
        ):
            raise ValueError("pre-listing periods cannot satisfy since-listing coverage")
        if any(
            period >= self.listing_date
            for period in self.ignored_pre_listing_period_ends
        ):
            raise ValueError("ignored since-listing periods must be pre-listing")
        if (
            set(self.covered_annual_period_ends)
            | set(self.missing_annual_period_ends)
            != set(self.expected_annual_period_ends)
            or set(self.covered_annual_period_ends)
            & set(self.missing_annual_period_ends)
            or set(self.covered_reporting_period_ends)
            | set(self.missing_reporting_period_ends)
            != set(self.expected_reporting_period_ends)
            or set(self.covered_reporting_period_ends)
            & set(self.missing_reporting_period_ends)
        ):
            raise ValueError("since-listing coverage partitions are inconsistent")
        normal_target_impossible = (
            len(self.expected_annual_period_ends) < 5
            or len(self.expected_reporting_period_ends) < 8
        )
        expected_supported = (
            normal_target_impossible
            and not self.missing_annual_period_ends
            and not self.missing_reporting_period_ends
        )
        if self.supported != expected_supported:
            raise ValueError("since-listing exception support is inconsistent")
        return self


class FinancialHistoryCompletenessAssessment(BaseModel):
    """Aggregate five-annual/eight-reporting coverage result."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    statement_type: FinancialStatementType
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_annual_period_ends: tuple[date, ...]
    target_reporting_period_ends: tuple[date, ...]
    covered_annual_period_ends: tuple[date, ...]
    covered_reporting_period_ends: tuple[date, ...]
    missing_annual_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    since_listing_exception: FinancialSinceListingException | None = None
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...]
    complete: bool

    @model_validator(mode="after")
    def _validate_assessment(self) -> FinancialHistoryCompletenessAssessment:
        _authoritative_instrument_payload(self.instrument_identity)
        if self.statement_type not in {
            FinancialStatementType.BALANCE_SHEET,
            FinancialStatementType.CASH_FLOW,
            FinancialStatementType.INCOME_STATEMENT,
        }:
            raise ValueError("statement history requires one full statement type")
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("statement history requires a resolved company type")
        target_fields = (
            self.target_annual_period_ends,
            self.target_reporting_period_ends,
            self.covered_annual_period_ends,
            self.covered_reporting_period_ends,
            self.missing_annual_period_ends,
            self.missing_reporting_period_ends,
        )
        if any(values != _canonical_dates(values, reverse=True) for values in target_fields):
            raise ValueError("history target period sets must be unique and canonical")
        if (
            set(self.covered_annual_period_ends)
            | set(self.missing_annual_period_ends)
            != set(self.target_annual_period_ends)
            or set(self.covered_annual_period_ends)
            & set(self.missing_annual_period_ends)
            or set(self.covered_reporting_period_ends)
            | set(self.missing_reporting_period_ends)
            != set(self.target_reporting_period_ends)
            or set(self.covered_reporting_period_ends)
            & set(self.missing_reporting_period_ends)
        ):
            raise ValueError("history coverage partitions are inconsistent")
        expected_complete = (
            len(self.target_annual_period_ends) == 5
            and len(self.target_reporting_period_ends) == 8
            and not self.missing_annual_period_ends
            and not self.missing_reporting_period_ends
        ) or bool(
            self.since_listing_exception is not None
            and self.since_listing_exception.supported
        )
        if self.complete != expected_complete:
            raise ValueError("financial history completeness is inconsistent")
        expected_reasons = (
            ()
            if self.complete
            else (
                (
                    FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,
                    FinancialPeriodRejectionReason.UNSUPPORTED_SINCE_LISTING_COVERAGE,
                )
                if self.since_listing_exception is not None
                else (FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,)
            )
        )
        if self.rejection_reasons != expected_reasons:
            raise ValueError("financial history rejection reasons are inconsistent")
        return self


def assess_financial_history_coverage(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    statement_type: FinancialStatementType,
    company_type: FinancialCompanyType,
    consolidation_scope: FinancialConsolidationScope,
    currency: str,
    assessments: Sequence[FinancialPeriodCompletenessAssessment],
    eligible_annual_period_ends: Sequence[date],
    eligible_reporting_period_ends: Sequence[date],
    listing_date: date | None = None,
    listing_provenance: FinancialListingProvenance | None = None,
) -> FinancialHistoryCompletenessAssessment:
    """Assess normal targets, then the fully-covered authoritative listing exception."""

    if (listing_date is None) != (listing_provenance is None):
        raise ValueError("since-listing coverage requires date and authoritative provenance")
    expected_instrument_payload = _authoritative_instrument_payload(
        instrument_identity
    )
    usable = tuple(
        item
        for item in assessments
        if item.usable_for_current_analysis
        and item.capability is FinancialCapability.STATEMENT
        and item.statement_type is statement_type
        and item.ratio_family is None
        and item.company_type is company_type
        and item.consolidation_scope is consolidation_scope
        and item.currency == currency.upper()
        and _authoritative_instrument_payload(item.instrument_identity)
        == expected_instrument_payload
    )
    available_annual = {
        item.period_end
        for item in usable
        if item.frequency is FinancialReportingFrequency.ANNUAL
    }
    available_reporting = {
        item.period_end
        for item in usable
        if item.frequency
        in {
            FinancialReportingFrequency.ANNUAL,
            FinancialReportingFrequency.QUARTERLY,
        }
    }
    target_annual = _canonical_dates(eligible_annual_period_ends, reverse=True)[:5]
    target_reporting = _canonical_dates(
        eligible_reporting_period_ends,
        reverse=True,
    )[:8]
    covered_annual = tuple(
        period for period in target_annual if period in available_annual
    )
    covered_reporting = tuple(
        period for period in target_reporting if period in available_reporting
    )
    missing_annual = tuple(
        period for period in target_annual if period not in available_annual
    )
    missing_reporting = tuple(
        period for period in target_reporting if period not in available_reporting
    )
    normal_complete = (
        len(target_annual) == 5
        and len(target_reporting) == 8
        and not missing_annual
        and not missing_reporting
    )

    since_listing_exception: FinancialSinceListingException | None = None
    if not normal_complete and listing_date is not None and listing_provenance is not None:
        expected_annual = _canonical_dates(
            tuple(
                period
                for period in eligible_annual_period_ends
                if period >= listing_date
            )
        )
        expected_reporting = _canonical_dates(
            tuple(
                period
                for period in eligible_reporting_period_ends
                if period >= listing_date
            )
        )
        covered_since_annual = tuple(
            period for period in expected_annual if period in available_annual
        )
        covered_since_reporting = tuple(
            period for period in expected_reporting if period in available_reporting
        )
        missing_since_annual = tuple(
            period for period in expected_annual if period not in available_annual
        )
        missing_since_reporting = tuple(
            period for period in expected_reporting if period not in available_reporting
        )
        ignored_pre_listing = _canonical_dates(
            tuple(
                period
                for period in available_annual | available_reporting
                if period < listing_date
            )
        )
        normal_target_impossible = (
            len(expected_annual) < 5 or len(expected_reporting) < 8
        )
        since_listing_exception = FinancialSinceListingException(
            listing_date=listing_date,
            listing_provenance=listing_provenance,
            expected_annual_period_ends=expected_annual,
            expected_reporting_period_ends=expected_reporting,
            covered_annual_period_ends=covered_since_annual,
            covered_reporting_period_ends=covered_since_reporting,
            missing_annual_period_ends=missing_since_annual,
            missing_reporting_period_ends=missing_since_reporting,
            ignored_pre_listing_period_ends=ignored_pre_listing,
            supported=(
                normal_target_impossible
                and not missing_since_annual
                and not missing_since_reporting
            ),
        )
    complete = normal_complete or bool(
        since_listing_exception is not None and since_listing_exception.supported
    )
    reasons: tuple[FinancialPeriodRejectionReason, ...]
    if complete:
        reasons = ()
    elif since_listing_exception is not None:
        reasons = (
            FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,
            FinancialPeriodRejectionReason.UNSUPPORTED_SINCE_LISTING_COVERAGE,
        )
    else:
        reasons = (FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,)
    return FinancialHistoryCompletenessAssessment(
        instrument_identity=instrument_identity,
        statement_type=statement_type,
        company_type=company_type,
        consolidation_scope=consolidation_scope,
        currency=currency.upper(),
        target_annual_period_ends=target_annual,
        target_reporting_period_ends=target_reporting,
        covered_annual_period_ends=covered_annual,
        covered_reporting_period_ends=covered_reporting,
        missing_annual_period_ends=missing_annual,
        missing_reporting_period_ends=missing_reporting,
        since_listing_exception=since_listing_exception,
        rejection_reasons=reasons,
        complete=complete,
    )


class FinancialRatioHistoryCompletenessAssessment(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    ratio_family: FinancialRatioFamily
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_reporting_period_ends: tuple[date, ...]
    covered_reporting_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...]
    complete: bool

    @model_validator(mode="after")
    def _validate_assessment(
        self,
    ) -> FinancialRatioHistoryCompletenessAssessment:
        _authoritative_instrument_payload(self.instrument_identity)
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("ratio history requires a resolved company type")
        period_sets = (
            self.target_reporting_period_ends,
            self.covered_reporting_period_ends,
            self.missing_reporting_period_ends,
        )
        if any(
            values != _canonical_dates(values, reverse=True)
            for values in period_sets
        ):
            raise ValueError("ratio history period sets must be unique and canonical")
        if (
            set(self.covered_reporting_period_ends)
            | set(self.missing_reporting_period_ends)
            != set(self.target_reporting_period_ends)
            or set(self.covered_reporting_period_ends)
            & set(self.missing_reporting_period_ends)
        ):
            raise ValueError("ratio history coverage partitions are inconsistent")
        expected_complete = (
            len(self.target_reporting_period_ends) == 8
            and not self.missing_reporting_period_ends
        )
        if self.complete != expected_complete:
            raise ValueError("ratio history completeness is inconsistent")
        expected_reasons = (
            ()
            if self.complete
            else (FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,)
        )
        if self.rejection_reasons != expected_reasons:
            raise ValueError("ratio history rejection reasons are inconsistent")
        return self


def assess_financial_ratio_history_coverage(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    ratio_family: FinancialRatioFamily,
    company_type: FinancialCompanyType,
    consolidation_scope: FinancialConsolidationScope,
    currency: str,
    assessments: Sequence[FinancialPeriodCompletenessAssessment],
    eligible_reporting_period_ends: Sequence[date],
) -> FinancialRatioHistoryCompletenessAssessment:
    target = _canonical_dates(eligible_reporting_period_ends, reverse=True)[:8]
    expected_instrument_payload = _authoritative_instrument_payload(
        instrument_identity
    )
    usable_periods = {
        item.period_end
        for item in assessments
        if item.usable_for_current_analysis
        and item.capability is FinancialCapability.RATIO_FAMILY
        and item.ratio_family is ratio_family
        and item.company_type is company_type
        and item.consolidation_scope is consolidation_scope
        and item.currency == currency.upper()
        and _authoritative_instrument_payload(item.instrument_identity)
        == expected_instrument_payload
        and item.frequency is FinancialReportingFrequency.QUARTERLY
    }
    covered = tuple(period for period in target if period in usable_periods)
    missing = tuple(period for period in target if period not in usable_periods)
    complete = len(target) == 8 and not missing
    return FinancialRatioHistoryCompletenessAssessment(
        instrument_identity=instrument_identity,
        ratio_family=ratio_family,
        company_type=company_type,
        consolidation_scope=consolidation_scope,
        currency=currency.upper(),
        target_reporting_period_ends=target,
        covered_reporting_period_ends=covered,
        missing_reporting_period_ends=missing,
        rejection_reasons=()
        if complete
        else (FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,),
        complete=complete,
    )


class FinancialProviderArtifactEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-manifest-v1"] = "financial-manifest-v1"
    artifact: FinancialProviderArtifactIdentity
    retention: Literal["retained"] = "retained"


FinancialUnavailableReason = (
    FinancialPeriodRejectionReason | AcquisitionUnavailableReason
)


class FinancialAcquisitionOutcome(BaseModel):
    """Typed payload-free outcome for one provider capability attempt."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-manifest-v1"] = "financial-manifest-v1"
    outcome: Literal["available", "unavailable"]
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    capability: FinancialCapability
    artifact_identity: str | None = Field(
        default=None,
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$",
    )
    reason: FinancialUnavailableReason | None = None

    @classmethod
    def available(
        cls,
        *,
        provider_id: str,
        capability: FinancialCapability,
        artifact_identity: str,
    ) -> FinancialAcquisitionOutcome:
        return cls(
            outcome="available",
            provider_id=provider_id,
            capability=capability,
            artifact_identity=artifact_identity,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        provider_id: str,
        capability: FinancialCapability,
        reason: FinancialUnavailableReason,
    ) -> FinancialAcquisitionOutcome:
        return cls(
            outcome="unavailable",
            provider_id=provider_id,
            capability=capability,
            reason=reason,
        )

    @model_validator(mode="after")
    def _validate_outcome(self) -> FinancialAcquisitionOutcome:
        _assert_safe_contract_payload(self.model_dump(mode="json"))
        if self.outcome == "available":
            if self.artifact_identity is None or self.reason is not None:
                raise ValueError("available financial outcome requires only an artifact")
        elif self.artifact_identity is not None or self.reason is None:
            raise ValueError("unavailable financial outcome requires only a typed reason")
        return self


class FinancialProviderCandidate(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-manifest-v1"] = "financial-manifest-v1"
    candidate: FinancialPeriodCandidate
    assessment: FinancialPeriodCompletenessAssessment

    @model_validator(mode="after")
    def _validate_binding(self) -> FinancialProviderCandidate:
        if self.assessment.candidate_identity != self.candidate.candidate_identity:
            raise ValueError("provider candidate assessment binding mismatch")
        if self.assessment.artifact_identity != self.candidate.artifact.artifact_identity:
            raise ValueError("provider candidate artifact binding mismatch")
        if self.assessment.period_identity != self.candidate.period_identity.period_identity:
            raise ValueError("provider candidate period binding mismatch")
        identity = self.candidate.period_identity
        if (
            _authoritative_instrument_payload(self.assessment.instrument_identity)
            != _authoritative_instrument_payload(identity.instrument_identity)
            or self.assessment.capability is not identity.capability
            or self.assessment.statement_type is not identity.statement_type
            or self.assessment.ratio_family is not identity.ratio_family
            or self.assessment.period_end != identity.period_end
            or self.assessment.frequency is not identity.frequency
            or self.assessment.company_type is not identity.company_type
            or self.assessment.consolidation_scope is not identity.consolidation_scope
            or self.assessment.currency != identity.currency
        ):
            raise ValueError("provider candidate assessment context mismatch")
        expected_observed_at = max(
            self.candidate.artifact.observed_at,
            self.candidate.filing_metadata.observed_at,
        )
        if (
            self.assessment.first_publication_date
            != self.candidate.filing_metadata.f_ann_date
            or self.assessment.revision_observed_at != expected_observed_at
            or self.assessment.metadata_complete
            != (self.candidate.filing_metadata.ann_date is not None)
        ):
            raise ValueError("provider candidate filing assessment mismatch")
        return self


class FinancialPeriodSelection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    disposition: Literal["accepted", "current_only"]
    strict_pit_eligible: bool
    selection_identity: str = Field(
        pattern=r"^financial-period-selection:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        candidate: FinancialPeriodCandidate,
        assessment: FinancialPeriodCompletenessAssessment,
    ) -> FinancialPeriodSelection:
        if assessment.disposition not in {
            FinancialPeriodDisposition.ACCEPTED,
            FinancialPeriodDisposition.CURRENT_ONLY,
        }:
            raise ValueError("only usable financial candidates may be selected")
        if (
            assessment.candidate_identity != candidate.candidate_identity
            or assessment.artifact_identity != candidate.artifact.artifact_identity
            or assessment.period_identity != candidate.period_identity.period_identity
        ):
            raise ValueError("financial selection candidate binding mismatch")
        payload = {
            "contract_version": "financial-period-selection-v1",
            "candidate_identity": candidate.candidate_identity,
            "artifact_identity": candidate.artifact.artifact_identity,
            "period_identity": candidate.period_identity.period_identity,
            "provider_id": candidate.artifact.dataset.provider_id,
            "disposition": assessment.disposition.value,
            "strict_pit_eligible": assessment.strict_pit_eligible,
        }
        return cls(
            **payload,
            selection_identity=(
                "financial-period-selection:v1:" + _canonical_digest(payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialPeriodSelection:
        payload = self.model_dump(mode="json", exclude={"selection_identity"})
        expected = "financial-period-selection:v1:" + _canonical_digest(payload)
        if self.selection_identity != expected:
            raise ValueError("financial period selection identity mismatch")
        if self.strict_pit_eligible != (self.disposition == "accepted"):
            raise ValueError("financial selection PIT disposition is inconsistent")
        return self


class FinancialRejectedPeriod(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    reasons: tuple[FinancialPeriodRejectionReason, ...]

    @classmethod
    def create(
        cls,
        *,
        candidate: FinancialPeriodCandidate,
        assessment: FinancialPeriodCompletenessAssessment,
    ) -> FinancialRejectedPeriod:
        if assessment.disposition not in {
            FinancialPeriodDisposition.REJECTED,
            FinancialPeriodDisposition.CONFLICTED,
        } or not assessment.rejection_reasons:
            raise ValueError("rejected-period contract requires typed rejection")
        if (
            assessment.candidate_identity != candidate.candidate_identity
            or assessment.artifact_identity != candidate.artifact.artifact_identity
            or assessment.period_identity != candidate.period_identity.period_identity
        ):
            raise ValueError("financial rejection candidate binding mismatch")
        return cls(
            candidate_identity=candidate.candidate_identity,
            artifact_identity=candidate.artifact.artifact_identity,
            period_identity=candidate.period_identity.period_identity,
            reasons=assessment.rejection_reasons,
        )

    @model_validator(mode="after")
    def _validate_reasons(self) -> FinancialRejectedPeriod:
        canonical = tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in self.reasons
        )
        if self.reasons != canonical:
            raise ValueError("rejected-period reasons must be unique and canonical")
        return self


class FinancialCriticalValueConflict(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    selected_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    conflicting_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    normalized_field: str = Field(pattern=_SAFE_ID_PATTERN)
    selected_value: Decimal
    conflicting_value: Decimal
    conflict_identity: str = Field(
        pattern=r"^financial-critical-conflict:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        selected_candidate_identity: str,
        conflicting_candidate_identity: str,
        normalized_field: str,
        selected_value: Decimal,
        conflicting_value: Decimal,
    ) -> FinancialCriticalValueConflict:
        payload = {
            "contract_version": "financial-period-selection-v1",
            "selected_candidate_identity": selected_candidate_identity,
            "conflicting_candidate_identity": conflicting_candidate_identity,
            "normalized_field": normalized_field,
            "selected_value": str(selected_value),
            "conflicting_value": str(conflicting_value),
        }
        return cls(
            **payload,
            conflict_identity=(
                "financial-critical-conflict:v1:" + _canonical_digest(payload)
            ),
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialCriticalValueConflict:
        if self.selected_value == self.conflicting_value:
            raise ValueError("critical-value conflict requires different values")
        payload = self.model_dump(mode="json", exclude={"conflict_identity"})
        expected = "financial-critical-conflict:v1:" + _canonical_digest(payload)
        if self.conflict_identity != expected:
            raise ValueError("financial critical-value conflict identity mismatch")
        return self


class FinancialPeriodOverlapFinding(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-period-selection-v1"] = (
        "financial-period-selection-v1"
    )
    selected_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    overlapping_candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    disposition: Literal[
        "equivalent_unselected",
        "critical_conflict",
        "company_type_conflict",
    ]
    compared_critical_fields: tuple[str, ...]
    conflict_identities: tuple[str, ...]
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...] = ()
    overlap_identity: str = Field(pattern=r"^financial-overlap:v1:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_identity(self) -> FinancialPeriodOverlapFinding:
        if self.compared_critical_fields != tuple(
            sorted(set(self.compared_critical_fields))
        ):
            raise ValueError("overlap critical fields must be unique and canonical")
        if self.conflict_identities != tuple(sorted(set(self.conflict_identities))):
            raise ValueError("overlap conflicts must be unique and canonical")
        canonical_reasons = tuple(
            reason
            for reason in FinancialPeriodRejectionReason
            if reason in self.rejection_reasons
        )
        if self.rejection_reasons != canonical_reasons:
            raise ValueError("overlap rejection reasons must be unique and canonical")
        expected_reasons = {
            "equivalent_unselected": (),
            "critical_conflict": (
                FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES,
            ),
            "company_type_conflict": (
                FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
            ),
        }[self.disposition]
        if (
            (self.disposition == "critical_conflict")
            != bool(self.conflict_identities)
            or self.rejection_reasons != expected_reasons
        ):
            raise ValueError("overlap disposition is inconsistent")
        payload = self.model_dump(mode="json", exclude={"overlap_identity"})
        expected = "financial-overlap:v1:" + _canonical_digest(payload)
        if self.overlap_identity != expected:
            raise ValueError("financial overlap identity mismatch")
        return self


def _period_compatibility_payload(candidate: FinancialPeriodCandidate) -> dict[str, object]:
    identity = candidate.period_identity
    return {
        "instrument_identity": _authoritative_instrument_payload(
            identity.instrument_identity
        ),
        "capability": identity.capability.value,
        "statement_type": (
            identity.statement_type.value if identity.statement_type is not None else None
        ),
        "ratio_family": (
            identity.ratio_family.value if identity.ratio_family is not None else None
        ),
        "period_end": identity.period_end.isoformat(),
        "frequency": identity.frequency.value,
        "consolidation_scope": identity.consolidation_scope.value,
        "currency": identity.currency,
    }


def compare_financial_period_candidates(
    selected: FinancialPeriodCandidate,
    overlapping: FinancialPeriodCandidate,
) -> tuple[FinancialPeriodOverlapFinding, tuple[FinancialCriticalValueConflict, ...]]:
    """Compare compatible overlap and isolate critical conflicts to one period."""

    if _period_compatibility_payload(selected) != _period_compatibility_payload(
        overlapping
    ):
        raise ValueError("financial overlap candidates have incompatible Period Identity")
    identity = selected.period_identity
    if identity.company_type is not overlapping.period_identity.company_type:
        payload = {
            "contract_version": "financial-period-selection-v1",
            "selected_candidate_identity": selected.candidate_identity,
            "overlapping_candidate_identity": overlapping.candidate_identity,
            "disposition": "company_type_conflict",
            "compared_critical_fields": [],
            "conflict_identities": [],
            "rejection_reasons": [
                FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE.value
            ],
        }
        return (
            FinancialPeriodOverlapFinding(
                **payload,
                overlap_identity=(
                    "financial-overlap:v1:" + _canonical_digest(payload)
                ),
            ),
            (),
        )
    if (
        identity.capability is FinancialCapability.STATEMENT
        and identity.statement_type is not None
    ):
        critical_fields = financial_statement_field_declaration(
            identity.company_type,
            identity.statement_type,
        ).critical_fields
    else:
        critical_fields = ()
    selected_values = {
        field.normalized_field: field.normalized_value for field in selected.fields
    }
    overlapping_values = {
        field.normalized_field: field.normalized_value for field in overlapping.fields
    }
    compared_fields = tuple(
        field
        for field in critical_fields
        if selected_values.get(field) is not None
        and overlapping_values.get(field) is not None
    )
    conflicts = tuple(
        FinancialCriticalValueConflict.create(
            selected_candidate_identity=selected.candidate_identity,
            conflicting_candidate_identity=overlapping.candidate_identity,
            normalized_field=field,
            selected_value=Decimal(str(selected_values[field])),
            conflicting_value=Decimal(str(overlapping_values[field])),
        )
        for field in compared_fields
        if Decimal(str(selected_values[field]))
        != Decimal(str(overlapping_values[field]))
    )
    payload = {
        "contract_version": "financial-period-selection-v1",
        "selected_candidate_identity": selected.candidate_identity,
        "overlapping_candidate_identity": overlapping.candidate_identity,
        "disposition": (
            "critical_conflict" if conflicts else "equivalent_unselected"
        ),
        "compared_critical_fields": list(compared_fields),
        "conflict_identities": sorted(
            conflict.conflict_identity for conflict in conflicts
        ),
        "rejection_reasons": (
            [FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES.value]
            if conflicts
            else []
        ),
    }
    overlap = FinancialPeriodOverlapFinding(
        **payload,
        overlap_identity="financial-overlap:v1:" + _canonical_digest(payload),
    )
    return overlap, conflicts


def mark_financial_period_conflicted(
    assessment: FinancialPeriodCompletenessAssessment,
) -> FinancialPeriodCompletenessAssessment:
    reasons = tuple(
        reason
        for reason in FinancialPeriodRejectionReason
        if reason
        in {
            *assessment.rejection_reasons,
            FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES,
        }
    )
    return FinancialPeriodCompletenessAssessment.model_validate(
        {
            **assessment.model_dump(mode="json"),
            "disposition": FinancialPeriodDisposition.CONFLICTED.value,
            "rejection_reasons": [reason.value for reason in reasons],
            "usable_for_current_analysis": False,
        }
    )


def mark_financial_period_company_type_conflicted(
    assessment: FinancialPeriodCompletenessAssessment,
) -> FinancialPeriodCompletenessAssessment:
    """Return a new typed conflict without mutating the retained candidate."""

    return FinancialPeriodCompletenessAssessment.model_validate(
        {
            **assessment.model_dump(mode="json"),
            "disposition": FinancialPeriodDisposition.CONFLICTED.value,
            "rejection_reasons": [
                FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE.value
            ],
            "usable_for_current_analysis": False,
        }
    )


FinancialAggregateCompleteness = (
    FinancialHistoryCompletenessAssessment
    | FinancialRatioHistoryCompletenessAssessment
)


def _manifest_identity_payload(
    *,
    capability_routing_plan_signature: str,
    instrument_identity: InstrumentIdentityEvidence,
    requested_capability: FinancialCapability,
    requested_statement_type: FinancialStatementType | None,
    requested_ratio_family: FinancialRatioFamily | None,
    artifacts: Sequence[FinancialProviderArtifactEntry],
    acquisition_outcomes: Sequence[FinancialAcquisitionOutcome],
    provider_candidates: Sequence[FinancialProviderCandidate],
    selections: Sequence[FinancialPeriodSelection],
    rejected_periods: Sequence[FinancialRejectedPeriod],
    overlaps: Sequence[FinancialPeriodOverlapFinding],
    conflicts: Sequence[FinancialCriticalValueConflict],
    aggregate_completeness: FinancialAggregateCompleteness,
) -> dict[str, object]:
    return {
        "contract_version": "financial-manifest-v1",
        "capability_routing_plan_signature": capability_routing_plan_signature,
        "instrument_identity": _authoritative_instrument_payload(instrument_identity),
        "requested_capability": requested_capability.value,
        "requested_statement_type": (
            requested_statement_type.value
            if requested_statement_type is not None
            else None
        ),
        "requested_ratio_family": (
            requested_ratio_family.value if requested_ratio_family is not None else None
        ),
        "normalizer_version": "mainland-financial-normalizer-v1",
        "completeness_policy_version": "mainland-financial-completeness-v1",
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "acquisition_outcomes": [
            item.model_dump(mode="json") for item in acquisition_outcomes
        ],
        "provider_candidates": [
            item.model_dump(mode="json") for item in provider_candidates
        ],
        "selections": [item.model_dump(mode="json") for item in selections],
        "rejected_periods": [
            item.model_dump(mode="json") for item in rejected_periods
        ],
        "overlaps": [item.model_dump(mode="json") for item in overlaps],
        "conflicts": [item.model_dump(mode="json") for item in conflicts],
        "aggregate_completeness": aggregate_completeness.model_dump(mode="json"),
    }


class FinancialAcquisitionManifest(BaseModel):
    """Safe immutable operational manifest; it is not a Source Fact container."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-manifest-v1"] = "financial-manifest-v1"
    capability_routing_plan_signature: str = Field(
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$"
    )
    instrument_identity: InstrumentIdentityEvidence
    requested_capability: FinancialCapability
    requested_statement_type: FinancialStatementType | None = None
    requested_ratio_family: FinancialRatioFamily | None = None
    normalizer_version: Literal["mainland-financial-normalizer-v1"] = (
        "mainland-financial-normalizer-v1"
    )
    completeness_policy_version: Literal[
        "mainland-financial-completeness-v1"
    ] = "mainland-financial-completeness-v1"
    artifacts: tuple[FinancialProviderArtifactEntry, ...]
    acquisition_outcomes: tuple[FinancialAcquisitionOutcome, ...]
    provider_candidates: tuple[FinancialProviderCandidate, ...]
    selections: tuple[FinancialPeriodSelection, ...]
    rejected_periods: tuple[FinancialRejectedPeriod, ...]
    overlaps: tuple[FinancialPeriodOverlapFinding, ...]
    conflicts: tuple[FinancialCriticalValueConflict, ...]
    aggregate_completeness: FinancialAggregateCompleteness
    manifest_identity: str = Field(pattern=r"^financial-manifest:v1:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        capability_routing_plan_signature: str,
        instrument_identity: InstrumentIdentityEvidence,
        requested_capability: FinancialCapability,
        requested_statement_type: FinancialStatementType | None,
        requested_ratio_family: FinancialRatioFamily | None,
        artifacts: Sequence[FinancialProviderArtifactEntry],
        acquisition_outcomes: Sequence[FinancialAcquisitionOutcome],
        provider_candidates: Sequence[FinancialProviderCandidate],
        selections: Sequence[FinancialPeriodSelection],
        rejected_periods: Sequence[FinancialRejectedPeriod],
        overlaps: Sequence[FinancialPeriodOverlapFinding],
        conflicts: Sequence[FinancialCriticalValueConflict],
        aggregate_completeness: FinancialAggregateCompleteness,
    ) -> FinancialAcquisitionManifest:
        canonical_artifacts = tuple(
            sorted(artifacts, key=lambda item: item.artifact.artifact_identity)
        )
        canonical_outcomes = tuple(
            sorted(
                acquisition_outcomes,
                key=lambda item: (
                    item.provider_id,
                    item.capability.value,
                    item.outcome,
                    item.artifact_identity or "",
                    item.reason.value if item.reason is not None else "",
                ),
            )
        )
        canonical_candidates = tuple(
            sorted(
                provider_candidates,
                key=lambda item: item.candidate.candidate_identity,
            )
        )
        canonical_selections = tuple(
            sorted(selections, key=lambda item: item.selection_identity)
        )
        canonical_rejections = tuple(
            sorted(rejected_periods, key=lambda item: item.candidate_identity)
        )
        canonical_overlaps = tuple(
            sorted(overlaps, key=lambda item: item.overlap_identity)
        )
        canonical_conflicts = tuple(
            sorted(conflicts, key=lambda item: item.conflict_identity)
        )
        payload = _manifest_identity_payload(
            capability_routing_plan_signature=capability_routing_plan_signature,
            instrument_identity=instrument_identity,
            requested_capability=requested_capability,
            requested_statement_type=requested_statement_type,
            requested_ratio_family=requested_ratio_family,
            artifacts=canonical_artifacts,
            acquisition_outcomes=canonical_outcomes,
            provider_candidates=canonical_candidates,
            selections=canonical_selections,
            rejected_periods=canonical_rejections,
            overlaps=canonical_overlaps,
            conflicts=canonical_conflicts,
            aggregate_completeness=aggregate_completeness,
        )
        return cls(
            capability_routing_plan_signature=capability_routing_plan_signature,
            instrument_identity=instrument_identity,
            requested_capability=requested_capability,
            requested_statement_type=requested_statement_type,
            requested_ratio_family=requested_ratio_family,
            artifacts=canonical_artifacts,
            acquisition_outcomes=canonical_outcomes,
            provider_candidates=canonical_candidates,
            selections=canonical_selections,
            rejected_periods=canonical_rejections,
            overlaps=canonical_overlaps,
            conflicts=canonical_conflicts,
            aggregate_completeness=aggregate_completeness,
            manifest_identity="financial-manifest:v1:" + _canonical_digest(payload),
        )

    @model_validator(mode="after")
    def _validate_manifest(self) -> FinancialAcquisitionManifest:
        _assert_safe_contract_payload(
            self.model_dump(mode="json", exclude={"manifest_identity"})
        )
        if self.requested_capability is FinancialCapability.STATEMENT:
            if self.requested_statement_type not in {
                FinancialStatementType.BALANCE_SHEET,
                FinancialStatementType.CASH_FLOW,
                FinancialStatementType.INCOME_STATEMENT,
            } or self.requested_ratio_family is not None:
                raise ValueError("statement manifest request identity is invalid")
            if (
                not isinstance(
                    self.aggregate_completeness,
                    FinancialHistoryCompletenessAssessment,
                )
                or self.aggregate_completeness.statement_type
                is not self.requested_statement_type
            ):
                raise ValueError("statement manifest completeness context is invalid")
        elif self.requested_statement_type is not None or self.requested_ratio_family is None:
            raise ValueError("ratio manifest request identity is invalid")
        elif (
            not isinstance(
                self.aggregate_completeness,
                FinancialRatioHistoryCompletenessAssessment,
            )
            or self.aggregate_completeness.ratio_family
            is not self.requested_ratio_family
        ):
            raise ValueError("ratio manifest completeness context is invalid")
        if _authoritative_instrument_payload(
            self.aggregate_completeness.instrument_identity
        ) != _authoritative_instrument_payload(self.instrument_identity):
            raise ValueError("manifest completeness Instrument Identity mismatch")
        if self.artifacts != tuple(
            sorted(self.artifacts, key=lambda item: item.artifact.artifact_identity)
        ):
            raise ValueError("manifest artifacts must be canonical")
        if self.acquisition_outcomes != tuple(
            sorted(
                self.acquisition_outcomes,
                key=lambda item: (
                    item.provider_id,
                    item.capability.value,
                    item.outcome,
                    item.artifact_identity or "",
                    item.reason.value if item.reason is not None else "",
                ),
            )
        ):
            raise ValueError("manifest acquisition outcomes must be canonical")
        if self.provider_candidates != tuple(
            sorted(
                self.provider_candidates,
                key=lambda item: item.candidate.candidate_identity,
            )
        ):
            raise ValueError("manifest provider candidates must be canonical")
        if self.selections != tuple(
            sorted(self.selections, key=lambda item: item.selection_identity)
        ):
            raise ValueError("manifest selections must be canonical")
        if self.rejected_periods != tuple(
            sorted(self.rejected_periods, key=lambda item: item.candidate_identity)
        ):
            raise ValueError("manifest rejected periods must be canonical")
        if self.overlaps != tuple(
            sorted(self.overlaps, key=lambda item: item.overlap_identity)
        ):
            raise ValueError("manifest overlaps must be canonical")
        if self.conflicts != tuple(
            sorted(self.conflicts, key=lambda item: item.conflict_identity)
        ):
            raise ValueError("manifest conflicts must be canonical")
        artifact_ids = tuple(item.artifact.artifact_identity for item in self.artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("manifest artifact identities must be unique")
        artifact_id_set = set(artifact_ids)
        candidate_by_id = {
            item.candidate.candidate_identity: item for item in self.provider_candidates
        }
        if len(candidate_by_id) != len(self.provider_candidates):
            raise ValueError("manifest candidate identities must be unique")
        if any(
            item.candidate.artifact.artifact_identity not in artifact_id_set
            for item in self.provider_candidates
        ):
            raise ValueError("manifest candidate artifact was not retained")
        if any(
            outcome.outcome == "available"
            and outcome.artifact_identity not in artifact_id_set
            for outcome in self.acquisition_outcomes
        ):
            raise ValueError("available manifest outcome artifact was not retained")
        for selection in self.selections:
            provider_candidate = candidate_by_id.get(selection.candidate_identity)
            if provider_candidate is None:
                raise ValueError("manifest selection candidate is absent")
            candidate = provider_candidate.candidate
            assessment = provider_candidate.assessment
            if (
                selection.artifact_identity != assessment.artifact_identity
                or selection.period_identity != assessment.period_identity
                or selection.provider_id != candidate.artifact.dataset.provider_id
                or selection.disposition != assessment.disposition.value
                or selection.strict_pit_eligible != assessment.strict_pit_eligible
            ):
                raise ValueError("manifest selection artifact binding mismatch")
            aggregate = self.aggregate_completeness
            identity = candidate.period_identity
            if (
                identity.company_type is not aggregate.company_type
                or identity.consolidation_scope is not aggregate.consolidation_scope
                or identity.currency != aggregate.currency
                or identity.capability is not self.requested_capability
                or identity.statement_type is not self.requested_statement_type
                or identity.ratio_family is not self.requested_ratio_family
                or _authoritative_instrument_payload(identity.instrument_identity)
                != _authoritative_instrument_payload(aggregate.instrument_identity)
            ):
                raise ValueError("manifest selection completeness context mismatch")
        rejection_by_id = {
            item.candidate_identity: item for item in self.rejected_periods
        }
        if len(rejection_by_id) != len(self.rejected_periods):
            raise ValueError("manifest rejected-period identities must be unique")
        for candidate_id, provider_candidate in candidate_by_id.items():
            if provider_candidate.assessment.disposition in {
                FinancialPeriodDisposition.REJECTED,
                FinancialPeriodDisposition.CONFLICTED,
            } and candidate_id not in rejection_by_id:
                raise ValueError("manifest rejected candidate lacks typed rejection")
        if any(
            rejection.candidate_identity not in candidate_by_id
            or rejection.artifact_identity not in artifact_id_set
            for rejection in self.rejected_periods
        ):
            raise ValueError("manifest rejected-period binding is absent")
        if any(
            rejection.period_identity
            != candidate_by_id[rejection.candidate_identity].assessment.period_identity
            or rejection.reasons
            != candidate_by_id[rejection.candidate_identity].assessment.rejection_reasons
            for rejection in self.rejected_periods
        ):
            raise ValueError("manifest rejected-period assessment mismatch")
        conflict_by_id = {
            item.conflict_identity: item for item in self.conflicts
        }
        if len(conflict_by_id) != len(self.conflicts):
            raise ValueError("manifest critical-conflict identities must be unique")
        if any(
            conflict.selected_candidate_identity not in candidate_by_id
            or conflict.conflicting_candidate_identity not in candidate_by_id
            for conflict in self.conflicts
        ):
            raise ValueError("manifest critical conflict candidate is absent")
        overlap_by_id = {item.overlap_identity: item for item in self.overlaps}
        if len(overlap_by_id) != len(self.overlaps):
            raise ValueError("manifest overlap identities must be unique")
        if any(
            overlap.selected_candidate_identity not in candidate_by_id
            or overlap.overlapping_candidate_identity not in candidate_by_id
            or not set(overlap.conflict_identities) <= set(conflict_by_id)
            for overlap in self.overlaps
        ):
            raise ValueError("manifest overlap binding is absent")
        payload = _manifest_identity_payload(
            capability_routing_plan_signature=self.capability_routing_plan_signature,
            instrument_identity=self.instrument_identity,
            requested_capability=self.requested_capability,
            requested_statement_type=self.requested_statement_type,
            requested_ratio_family=self.requested_ratio_family,
            artifacts=self.artifacts,
            acquisition_outcomes=self.acquisition_outcomes,
            provider_candidates=self.provider_candidates,
            selections=self.selections,
            rejected_periods=self.rejected_periods,
            overlaps=self.overlaps,
            conflicts=self.conflicts,
            aggregate_completeness=self.aggregate_completeness,
        )
        expected = "financial-manifest:v1:" + _canonical_digest(payload)
        if self.manifest_identity != expected:
            raise ValueError("financial acquisition manifest identity mismatch")
        return self


__all__ = [
    "FINANCIAL_PERIOD_SELECTION_V1",
    "FINANCIAL_PERIOD_SELECTION_V2",
    "FINANCIAL_RATIO_DECLARATION_V1",
    "FINANCIAL_RATIO_DECLARATION_V2",
    "FinancialAcquisitionManifest",
    "FinancialAcquisitionOutcome",
    "FinancialCapability",
    "FinancialCompanyType",
    "FinancialCompanyTypeResolution",
    "FinancialCompanyTypeResolutionMethod",
    "FinancialConsolidationScope",
    "FinancialCriticalValueConflict",
    "FinancialFieldValue",
    "FinancialFieldSetDeclaration",
    "FinancialFilingMetadata",
    "FinancialHistoryCompletenessAssessment",
    "FinancialListingProvenance",
    "FinancialPeriodCandidate",
    "FinancialPeriodCompletenessAssessment",
    "FinancialPeriodDisposition",
    "FinancialPeriodIdentity",
    "FinancialPeriodOverlapFinding",
    "FinancialPeriodRejectionReason",
    "FinancialPeriodSelection",
    "FinancialProviderArtifactEntry",
    "FinancialProviderArtifactIdentity",
    "FinancialProviderCandidate",
    "FinancialProviderDatasetIdentity",
    "FinancialRejectedPeriod",
    "FinancialRatioFieldSetDeclaration",
    "FinancialRatioFamily",
    "FinancialRatioHistoryCompletenessAssessment",
    "FinancialReportingFrequency",
    "FinancialStatementType",
    "FinancialSinceListingException",
    "FinancialUnavailableReason",
    "assess_financial_period_candidate",
    "assess_financial_history_coverage",
    "assess_financial_ratio_history_coverage",
    "compare_financial_period_candidates",
    "financial_ratio_field_declaration",
    "financial_statement_field_declaration",
    "mark_financial_period_conflicted",
    "mark_financial_period_company_type_conflicted",
    "resolve_financial_company_type",
]

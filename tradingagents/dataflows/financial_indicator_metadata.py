"""Closed semantic metadata contracts for normalized financial indicators."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
    FINANCIAL_RATIO_DECLARATION_V1,
    FINANCIAL_RATIO_DECLARATION_V2,
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialPeriodCandidate,
    FinancialRatioFamily,
    assert_safe_financial_projection,
    financial_ratio_field_declaration,
)
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind

FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION = (
    "financial-indicator-field-semantics-v1"
)
FINANCIAL_INDICATOR_FIELD_SEMANTICS_IDENTITY_NAMESPACE = (
    "financial-indicator-field-semantics:v1"
)
FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION = (
    "financial-indicator-metadata-compatibility-v1"
)
FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_IDENTITY_NAMESPACE = (
    "financial-indicator-metadata-compatibility:v1"
)

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_FIELD_PATTERN = r"^[a-z][a-z0-9_]{0,127}$"
_DECLARATION_VERSIONS = (
    FINANCIAL_RATIO_DECLARATION_V1,
    FINANCIAL_RATIO_DECLARATION_V2,
    FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
)


class FinancialIndicatorSemanticAssessmentState(str, Enum):
    PROVEN = "proven"
    UNPROVEN = "unproven"
    NOT_APPLICABLE = "not_applicable"


class FinancialIndicatorFieldClass(str, Enum):
    DIMENSIONLESS_RATIO = "dimensionless_ratio"
    PERCENTAGE_RATE = "percentage_rate"
    PER_SHARE = "per_share"
    MONETARY = "monetary"
    SHARE_COUNT = "share_count"
    DAYS_TURNOVER_TIME = "days_turnover_time"
    OTHER = "other"


class FinancialIndicatorCanonicalBasis(str, Enum):
    RATIO = "RATIO"
    CURRENCY_PER_SHARE = "CURRENCY_PER_SHARE"
    CURRENCY_BASE_UNIT = "CURRENCY_BASE_UNIT"
    SHARES = "SHARES"
    DAYS = "DAYS"
    TURNS_PER_REPORTING_PERIOD = "TURNS_PER_REPORTING_PERIOD"
    UNCLASSIFIED = "UNCLASSIFIED"


class FinancialIndicatorSemanticApplicability(str, Enum):
    REQUIRED = "required"
    NOT_APPLICABLE = "not_applicable"
    UNCLASSIFIED = "unclassified"


class FinancialIndicatorRequiredProviderEvidence(str, Enum):
    NUMERIC_VALUE = "numeric_value"
    PROVIDER_FIELD = "provider_field"
    ORIGINAL_UNIT_BASIS = "original_unit_basis"
    DETERMINISTIC_RATIO_NORMALIZATION = "deterministic_ratio_normalization"
    VALUE_CURRENCY = "value_currency"
    SHARE_BASIS = "share_basis"
    MONETARY_SCALE = "monetary_scale"
    SHARE_COUNT_BASIS = "share_count_basis"
    TIME_OR_TURNOVER_BASIS = "time_or_turnover_basis"
    EFFECTIVE_DATE = "effective_date"
    ANNOUNCEMENT_METADATA = "announcement_metadata"
    PROVEN_FILING_SCOPE = "proven_filing_scope"
    PUBLICATION_OBSERVATION_LINEAGE = "publication_observation_lineage"
    EXPLICIT_FIELD_CLASSIFICATION = "explicit_field_classification"


class FinancialIndicatorClassRule(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    field_class: FinancialIndicatorFieldClass
    canonical_unit_bases: tuple[FinancialIndicatorCanonicalBasis, ...]
    value_currency_applicability: FinancialIndicatorSemanticApplicability
    filing_scope_material: bool
    required_provider_evidence: tuple[FinancialIndicatorRequiredProviderEvidence, ...]
    current_only_eligible: bool
    strict_pit_eligible: bool

    @property
    def canonical_unit_basis(self) -> FinancialIndicatorCanonicalBasis:
        return self.canonical_unit_bases[0]


def _evidence(
    *items: FinancialIndicatorRequiredProviderEvidence,
) -> tuple[FinancialIndicatorRequiredProviderEvidence, ...]:
    return tuple(items)


_CLASS_RULES: Mapping[
    FinancialIndicatorFieldClass, FinancialIndicatorClassRule
] = MappingProxyType(
    {
        FinancialIndicatorFieldClass.DIMENSIONLESS_RATIO: (
            FinancialIndicatorClassRule(
                field_class=FinancialIndicatorFieldClass.DIMENSIONLESS_RATIO,
                canonical_unit_bases=(FinancialIndicatorCanonicalBasis.RATIO,),
                value_currency_applicability=(
                    FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
                ),
                filing_scope_material=True,
                required_provider_evidence=_evidence(
                    FinancialIndicatorRequiredProviderEvidence.NUMERIC_VALUE,
                    FinancialIndicatorRequiredProviderEvidence.PROVIDER_FIELD,
                    FinancialIndicatorRequiredProviderEvidence.ORIGINAL_UNIT_BASIS,
                    FinancialIndicatorRequiredProviderEvidence.DETERMINISTIC_RATIO_NORMALIZATION,
                    FinancialIndicatorRequiredProviderEvidence.ANNOUNCEMENT_METADATA,
                    FinancialIndicatorRequiredProviderEvidence.PROVEN_FILING_SCOPE,
                ),
                current_only_eligible=True,
                strict_pit_eligible=True,
            )
        ),
        FinancialIndicatorFieldClass.PERCENTAGE_RATE: FinancialIndicatorClassRule(
            field_class=FinancialIndicatorFieldClass.PERCENTAGE_RATE,
            canonical_unit_bases=(FinancialIndicatorCanonicalBasis.RATIO,),
            value_currency_applicability=(
                FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
            ),
            filing_scope_material=True,
            required_provider_evidence=_evidence(
                FinancialIndicatorRequiredProviderEvidence.NUMERIC_VALUE,
                FinancialIndicatorRequiredProviderEvidence.PROVIDER_FIELD,
                FinancialIndicatorRequiredProviderEvidence.ORIGINAL_UNIT_BASIS,
                FinancialIndicatorRequiredProviderEvidence.DETERMINISTIC_RATIO_NORMALIZATION,
                FinancialIndicatorRequiredProviderEvidence.ANNOUNCEMENT_METADATA,
                FinancialIndicatorRequiredProviderEvidence.PROVEN_FILING_SCOPE,
            ),
            current_only_eligible=True,
            strict_pit_eligible=True,
        ),
        FinancialIndicatorFieldClass.PER_SHARE: FinancialIndicatorClassRule(
            field_class=FinancialIndicatorFieldClass.PER_SHARE,
            canonical_unit_bases=(
                FinancialIndicatorCanonicalBasis.CURRENCY_PER_SHARE,
            ),
            value_currency_applicability=(
                FinancialIndicatorSemanticApplicability.REQUIRED
            ),
            filing_scope_material=True,
            required_provider_evidence=_evidence(
                FinancialIndicatorRequiredProviderEvidence.NUMERIC_VALUE,
                FinancialIndicatorRequiredProviderEvidence.PROVIDER_FIELD,
                FinancialIndicatorRequiredProviderEvidence.ORIGINAL_UNIT_BASIS,
                FinancialIndicatorRequiredProviderEvidence.VALUE_CURRENCY,
                FinancialIndicatorRequiredProviderEvidence.SHARE_BASIS,
                FinancialIndicatorRequiredProviderEvidence.ANNOUNCEMENT_METADATA,
                FinancialIndicatorRequiredProviderEvidence.PROVEN_FILING_SCOPE,
            ),
            current_only_eligible=True,
            strict_pit_eligible=True,
        ),
        FinancialIndicatorFieldClass.MONETARY: FinancialIndicatorClassRule(
            field_class=FinancialIndicatorFieldClass.MONETARY,
            canonical_unit_bases=(
                FinancialIndicatorCanonicalBasis.CURRENCY_BASE_UNIT,
            ),
            value_currency_applicability=(
                FinancialIndicatorSemanticApplicability.REQUIRED
            ),
            filing_scope_material=True,
            required_provider_evidence=_evidence(
                FinancialIndicatorRequiredProviderEvidence.NUMERIC_VALUE,
                FinancialIndicatorRequiredProviderEvidence.PROVIDER_FIELD,
                FinancialIndicatorRequiredProviderEvidence.ORIGINAL_UNIT_BASIS,
                FinancialIndicatorRequiredProviderEvidence.VALUE_CURRENCY,
                FinancialIndicatorRequiredProviderEvidence.MONETARY_SCALE,
                FinancialIndicatorRequiredProviderEvidence.ANNOUNCEMENT_METADATA,
                FinancialIndicatorRequiredProviderEvidence.PROVEN_FILING_SCOPE,
            ),
            current_only_eligible=True,
            strict_pit_eligible=True,
        ),
        FinancialIndicatorFieldClass.SHARE_COUNT: FinancialIndicatorClassRule(
            field_class=FinancialIndicatorFieldClass.SHARE_COUNT,
            canonical_unit_bases=(FinancialIndicatorCanonicalBasis.SHARES,),
            value_currency_applicability=(
                FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
            ),
            filing_scope_material=False,
            required_provider_evidence=_evidence(
                FinancialIndicatorRequiredProviderEvidence.NUMERIC_VALUE,
                FinancialIndicatorRequiredProviderEvidence.PROVIDER_FIELD,
                FinancialIndicatorRequiredProviderEvidence.ORIGINAL_UNIT_BASIS,
                FinancialIndicatorRequiredProviderEvidence.SHARE_COUNT_BASIS,
                FinancialIndicatorRequiredProviderEvidence.EFFECTIVE_DATE,
                FinancialIndicatorRequiredProviderEvidence.PUBLICATION_OBSERVATION_LINEAGE,
            ),
            current_only_eligible=True,
            strict_pit_eligible=True,
        ),
        FinancialIndicatorFieldClass.DAYS_TURNOVER_TIME: (
            FinancialIndicatorClassRule(
                field_class=FinancialIndicatorFieldClass.DAYS_TURNOVER_TIME,
                canonical_unit_bases=(
                    FinancialIndicatorCanonicalBasis.DAYS,
                    FinancialIndicatorCanonicalBasis.TURNS_PER_REPORTING_PERIOD,
                ),
                value_currency_applicability=(
                    FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
                ),
                filing_scope_material=True,
                required_provider_evidence=_evidence(
                    FinancialIndicatorRequiredProviderEvidence.NUMERIC_VALUE,
                    FinancialIndicatorRequiredProviderEvidence.PROVIDER_FIELD,
                    FinancialIndicatorRequiredProviderEvidence.ORIGINAL_UNIT_BASIS,
                    FinancialIndicatorRequiredProviderEvidence.TIME_OR_TURNOVER_BASIS,
                    FinancialIndicatorRequiredProviderEvidence.ANNOUNCEMENT_METADATA,
                    FinancialIndicatorRequiredProviderEvidence.PROVEN_FILING_SCOPE,
                ),
                current_only_eligible=True,
                strict_pit_eligible=True,
            )
        ),
        FinancialIndicatorFieldClass.OTHER: FinancialIndicatorClassRule(
            field_class=FinancialIndicatorFieldClass.OTHER,
            canonical_unit_bases=(FinancialIndicatorCanonicalBasis.UNCLASSIFIED,),
            value_currency_applicability=(
                FinancialIndicatorSemanticApplicability.UNCLASSIFIED
            ),
            filing_scope_material=True,
            required_provider_evidence=_evidence(
                FinancialIndicatorRequiredProviderEvidence.EXPLICIT_FIELD_CLASSIFICATION,
            ),
            current_only_eligible=False,
            strict_pit_eligible=False,
        ),
    }
)


def financial_indicator_class_rule(
    field_class: FinancialIndicatorFieldClass,
) -> FinancialIndicatorClassRule:
    return _CLASS_RULES[field_class]


class FinancialIndicatorFieldSemantics(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-indicator-field-semantics-v1"] = (
        FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION
    )
    family: FinancialRatioFamily
    normalized_field: str = Field(pattern=_SAFE_FIELD_PATTERN)
    field_class: FinancialIndicatorFieldClass
    canonical_unit_basis: FinancialIndicatorCanonicalBasis
    value_currency_applicability: FinancialIndicatorSemanticApplicability
    filing_scope_material: bool
    required_provider_evidence: tuple[FinancialIndicatorRequiredProviderEvidence, ...]
    current_only_eligible: bool
    strict_pit_eligible: bool
    declaration_versions: tuple[
        Literal[
            "financial-ratio-declarations-v1",
            "financial-ratio-declarations-v2",
            "financial-ratio-declarations-baostock-v1",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def _validate_class_rule(self) -> FinancialIndicatorFieldSemantics:
        rule = financial_indicator_class_rule(self.field_class)
        if (
            self.canonical_unit_basis not in rule.canonical_unit_bases
            or self.value_currency_applicability
            is not rule.value_currency_applicability
            or self.filing_scope_material != rule.filing_scope_material
            or self.required_provider_evidence != rule.required_provider_evidence
            or self.current_only_eligible != rule.current_only_eligible
            or self.strict_pit_eligible != rule.strict_pit_eligible
        ):
            raise ValueError("financial indicator field contradicts its class rule")
        if (
            not self.declaration_versions
            or self.declaration_versions
            != tuple(
                version
                for version in _DECLARATION_VERSIONS
                if version in self.declaration_versions
            )
        ):
            raise ValueError("indicator declaration versions must be canonical")
        return self


class FinancialIndicatorDeclarationSemanticAlias(BaseModel):
    """Explicit legacy declaration-family binding to the v1 semantic family."""

    model_config = _CLOSED_MODEL_CONFIG

    declaration_version: Literal[
        "financial-ratio-declarations-v1",
        "financial-ratio-declarations-v2",
        "financial-ratio-declarations-baostock-v1",
    ]
    declared_family: FinancialRatioFamily
    normalized_field: str = Field(pattern=_SAFE_FIELD_PATTERN)
    semantic_family: FinancialRatioFamily


_DECLARATION_SEMANTIC_ALIASES = (
    FinancialIndicatorDeclarationSemanticAlias(
        declaration_version=FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
        declared_family=FinancialRatioFamily.BALANCE,
        normalized_field="liability_growth",
        semantic_family=FinancialRatioFamily.GROWTH,
    ),
)


_PROFIT_PER_SHARE = {"earnings_per_share"}
_PROFIT_RATES = {
    "ebitda_margin",
    "gross_margin",
    "net_margin",
    "operating_margin",
    "pre_tax_margin",
    "profit_to_cost",
    "return_on_assets",
    "return_on_equity",
    "return_on_invested_capital",
}
_OPERATION_DAYS = {
    "cash_conversion_cycle",
    "inventory_days",
    "operating_cycle",
    "receivable_days",
}
_OPERATION_TURNS = {
    "accounts_payable_turnover",
    "asset_turnover",
    "current_asset_turnover",
    "fixed_asset_turnover",
    "inventory_turnover",
    "receivables_turnover",
}
_GROWTH_RATES = {
    "asset_growth",
    "basic_earnings_per_share_growth",
    "cash_flow_growth",
    "equity_growth",
    "gross_profit_growth",
    "liability_growth",
    "net_income_growth",
    "operating_profit_growth",
    "parent_net_income_growth",
    "revenue_growth",
    "return_on_equity_growth",
    "total_profit_growth",
    "working_capital_growth",
}
_BALANCE_RATIOS = {
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
}
_CASH_FLOW_RATIOS = {
    "cash_flow_adequacy",
    "cash_flow_to_debt",
    "cash_flow_to_gross_revenue",
    "cash_flow_to_liabilities",
    "cash_flow_to_revenue",
    "current_assets_to_assets",
    "interest_coverage",
    "non_current_assets_to_assets",
    "operating_cash_flow_ratio",
    "quality_of_income",
    "reinvestment_ratio",
    "self_financing_ratio",
    "tangible_asset_ratio",
}
_CASH_FLOW_RATES = {"cash_return_on_assets", "free_cash_flow_margin"}
_DUPONT_TURNS = {"asset_turnover_component"}
_DUPONT_RATIOS = {
    "debt_burden",
    "equity_multiplier_component",
    "financial_leverage",
    "interest_burden",
    "parent_net_income_share",
    "tax_burden",
}
_DUPONT_RATES = {
    "ebit_margin_component",
    "net_profit_margin_component",
    "operating_margin_component",
    "return_on_assets_component",
    "return_on_equity_component",
}

_SEMANTIC_GROUPS = (
    (
        FinancialRatioFamily.PROFIT,
        FinancialIndicatorFieldClass.PER_SHARE,
        FinancialIndicatorCanonicalBasis.CURRENCY_PER_SHARE,
        _PROFIT_PER_SHARE,
    ),
    (
        FinancialRatioFamily.PROFIT,
        FinancialIndicatorFieldClass.PERCENTAGE_RATE,
        FinancialIndicatorCanonicalBasis.RATIO,
        _PROFIT_RATES,
    ),
    (
        FinancialRatioFamily.OPERATION,
        FinancialIndicatorFieldClass.DAYS_TURNOVER_TIME,
        FinancialIndicatorCanonicalBasis.DAYS,
        _OPERATION_DAYS,
    ),
    (
        FinancialRatioFamily.OPERATION,
        FinancialIndicatorFieldClass.DAYS_TURNOVER_TIME,
        FinancialIndicatorCanonicalBasis.TURNS_PER_REPORTING_PERIOD,
        _OPERATION_TURNS,
    ),
    (
        FinancialRatioFamily.GROWTH,
        FinancialIndicatorFieldClass.PERCENTAGE_RATE,
        FinancialIndicatorCanonicalBasis.RATIO,
        _GROWTH_RATES,
    ),
    (
        FinancialRatioFamily.BALANCE,
        FinancialIndicatorFieldClass.DIMENSIONLESS_RATIO,
        FinancialIndicatorCanonicalBasis.RATIO,
        _BALANCE_RATIOS,
    ),
    (
        FinancialRatioFamily.CASH_FLOW,
        FinancialIndicatorFieldClass.DIMENSIONLESS_RATIO,
        FinancialIndicatorCanonicalBasis.RATIO,
        _CASH_FLOW_RATIOS,
    ),
    (
        FinancialRatioFamily.CASH_FLOW,
        FinancialIndicatorFieldClass.PERCENTAGE_RATE,
        FinancialIndicatorCanonicalBasis.RATIO,
        _CASH_FLOW_RATES,
    ),
    (
        FinancialRatioFamily.DUPONT,
        FinancialIndicatorFieldClass.DAYS_TURNOVER_TIME,
        FinancialIndicatorCanonicalBasis.TURNS_PER_REPORTING_PERIOD,
        _DUPONT_TURNS,
    ),
    (
        FinancialRatioFamily.DUPONT,
        FinancialIndicatorFieldClass.DIMENSIONLESS_RATIO,
        FinancialIndicatorCanonicalBasis.RATIO,
        _DUPONT_RATIOS,
    ),
    (
        FinancialRatioFamily.DUPONT,
        FinancialIndicatorFieldClass.PERCENTAGE_RATE,
        FinancialIndicatorCanonicalBasis.RATIO,
        _DUPONT_RATES,
    ),
)


def _declaration_pair_versions() -> dict[tuple[FinancialRatioFamily, str], tuple[str, ...]]:
    memberships: dict[tuple[FinancialRatioFamily, str], set[str]] = {}
    company_types = tuple(
        item for item in FinancialCompanyType if item is not FinancialCompanyType.UNKNOWN
    )
    for version in _DECLARATION_VERSIONS:
        for company_type in company_types:
            for family in FinancialRatioFamily:
                declaration = financial_ratio_field_declaration(
                    company_type,
                    family,
                    declaration_version=version,
                )
                for field in declaration.normalized_fields:
                    pair = (family, field)
                    memberships.setdefault(pair, set()).add(version)
    return {
        pair: tuple(version for version in _DECLARATION_VERSIONS if version in versions)
        for pair, versions in memberships.items()
    }


_DECLARATION_PAIR_VERSIONS = _declaration_pair_versions()


def _declaration_versions_for_semantic_pair(
    pair: tuple[FinancialRatioFamily, str],
) -> tuple[str, ...] | None:
    versions = set(_DECLARATION_PAIR_VERSIONS.get(pair, ()))
    for alias in _DECLARATION_SEMANTIC_ALIASES:
        if (alias.semantic_family, alias.normalized_field) != pair:
            continue
        declared_versions = _DECLARATION_PAIR_VERSIONS.get(
            (alias.declared_family, alias.normalized_field),
            (),
        )
        if alias.declaration_version in declared_versions:
            versions.add(alias.declaration_version)
    if not versions:
        return None
    return tuple(version for version in _DECLARATION_VERSIONS if version in versions)


def _default_entries() -> tuple[FinancialIndicatorFieldSemantics, ...]:
    entries: list[FinancialIndicatorFieldSemantics] = []
    for family, field_class, basis, fields in _SEMANTIC_GROUPS:
        rule = financial_indicator_class_rule(field_class)
        for normalized_field in fields:
            pair = (family, normalized_field)
            declaration_versions = _declaration_versions_for_semantic_pair(pair)
            if declaration_versions is None:
                raise ValueError("semantic field has no declared financial ratio binding")
            entries.append(
                FinancialIndicatorFieldSemantics(
                    family=family,
                    normalized_field=normalized_field,
                    field_class=field_class,
                    canonical_unit_basis=basis,
                    value_currency_applicability=rule.value_currency_applicability,
                    filing_scope_material=rule.filing_scope_material,
                    required_provider_evidence=rule.required_provider_evidence,
                    current_only_eligible=rule.current_only_eligible,
                    strict_pit_eligible=rule.strict_pit_eligible,
                    declaration_versions=declaration_versions,
                )
            )
    return tuple(entries)


_EXPECTED_ENTRIES = _default_entries()
_EXPECTED_PAIRS = frozenset(
    (entry.family, entry.normalized_field) for entry in _EXPECTED_ENTRIES
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _policy_identity(
    entries: Sequence[FinancialIndicatorFieldSemantics],
    aliases: Sequence[FinancialIndicatorDeclarationSemanticAlias],
) -> str:
    payload = {
        "contract_version": FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION,
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "declaration_semantic_aliases": [
            alias.model_dump(mode="json") for alias in aliases
        ],
    }
    digest = sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{FINANCIAL_INDICATOR_FIELD_SEMANTICS_IDENTITY_NAMESPACE}:{digest}"


class FinancialIndicatorFieldSemanticsPolicy(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-indicator-field-semantics-v1"] = (
        FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION
    )
    entries: tuple[FinancialIndicatorFieldSemantics, ...]
    declaration_semantic_aliases: tuple[
        FinancialIndicatorDeclarationSemanticAlias,
        ...,
    ]
    policy_identity: str = Field(
        pattern=r"^financial-indicator-field-semantics:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        entries: Sequence[FinancialIndicatorFieldSemantics],
        declaration_semantic_aliases: Sequence[
            FinancialIndicatorDeclarationSemanticAlias
        ] = _DECLARATION_SEMANTIC_ALIASES,
    ) -> FinancialIndicatorFieldSemanticsPolicy:
        canonical = tuple(
            sorted(entries, key=lambda item: (item.family.value, item.normalized_field))
        )
        canonical_aliases = tuple(
            sorted(
                declaration_semantic_aliases,
                key=lambda item: (
                    item.declaration_version,
                    item.declared_family.value,
                    item.normalized_field,
                    item.semantic_family.value,
                ),
            )
        )
        return cls(
            entries=canonical,
            declaration_semantic_aliases=canonical_aliases,
            policy_identity=_policy_identity(canonical, canonical_aliases),
        )

    @model_validator(mode="after")
    def _validate_closed_registry(self) -> FinancialIndicatorFieldSemanticsPolicy:
        canonical = tuple(
            sorted(
                self.entries,
                key=lambda item: (item.family.value, item.normalized_field),
            )
        )
        pairs = tuple((entry.family, entry.normalized_field) for entry in self.entries)
        if self.entries != canonical or len(pairs) != len(set(pairs)):
            raise ValueError("indicator field-semantics registry must be canonical")
        if len(self.entries) != 70 or frozenset(pairs) != _EXPECTED_PAIRS:
            raise ValueError("indicator field-semantics registry is not exact v1")
        expected_by_pair = {
            (entry.family, entry.normalized_field): entry
            for entry in _EXPECTED_ENTRIES
        }
        if any(
            entry != expected_by_pair[(entry.family, entry.normalized_field)]
            for entry in self.entries
        ):
            raise ValueError("indicator field-semantics entry is not exact v1")
        if self.declaration_semantic_aliases != _DECLARATION_SEMANTIC_ALIASES:
            raise ValueError("indicator declaration semantic aliases are not exact v1")
        if self.policy_identity != _policy_identity(
            self.entries,
            self.declaration_semantic_aliases,
        ):
            raise ValueError("indicator field-semantics policy identity mismatch")
        return self

    def lookup(
        self,
        family: FinancialRatioFamily,
        normalized_field: str,
        *,
        declaration_version: str,
    ) -> FinancialIndicatorFieldSemantics:
        for entry in self.entries:
            if (
                entry.family is family
                and entry.normalized_field == normalized_field
                and declaration_version in entry.declaration_versions
            ):
                return entry
        semantic_family = next(
            (
                alias.semantic_family
                for alias in self.declaration_semantic_aliases
                if alias.declaration_version == declaration_version
                and alias.declared_family is family
                and alias.normalized_field == normalized_field
            ),
            None,
        )
        if semantic_family is not None:
            for entry in self.entries:
                if (
                    entry.family is semantic_family
                    and entry.normalized_field == normalized_field
                    and declaration_version in entry.declaration_versions
                ):
                    return entry
        raise ValueError("financial indicator field semantics are not registered")


FINANCIAL_INDICATOR_FIELD_SEMANTICS_REGISTRY = (
    FinancialIndicatorFieldSemanticsPolicy.create(_EXPECTED_ENTRIES)
)


def financial_indicator_field_semantics_policy_v1(
) -> FinancialIndicatorFieldSemanticsPolicy:
    return FINANCIAL_INDICATOR_FIELD_SEMANTICS_REGISTRY


def financial_indicator_field_semantics(
    family: FinancialRatioFamily,
    normalized_field: str,
    *,
    declaration_version: str,
) -> FinancialIndicatorFieldSemantics:
    return FINANCIAL_INDICATOR_FIELD_SEMANTICS_REGISTRY.lookup(
        family,
        normalized_field,
        declaration_version=declaration_version,
    )


class FinancialIndicatorMetadataDimension(str, Enum):
    INSTRUMENT_CURRENCY = "instrument_currency"
    VALUE_UNIT = "value_unit"
    VALUE_CURRENCY = "value_currency"
    FILING_CONSOLIDATION_SCOPE = "filing_consolidation_scope"


class FinancialIndicatorMetadataEvidenceOrigin(str, Enum):
    AUTHORITATIVE_INSTRUMENT_IDENTITY = "authoritative_instrument_identity"
    PROVIDER_ARTIFACT = "provider_artifact"
    FIELD_SEMANTICS_POLICY = "field_semantics_policy"


class FinancialIndicatorMetadataReason(str, Enum):
    AUTHORITATIVE_INSTRUMENT_IDENTITY_PROVEN = (
        "authoritative_instrument_identity_proven"
    )
    QUALIFIED_PROVIDER_EVIDENCE_PROVEN = "qualified_provider_evidence_proven"
    FIELD_SEMANTICS_NOT_APPLICABLE = "field_semantics_not_applicable"
    MATERIAL_EVIDENCE_UNPROVEN = "material_evidence_unproven"
    QUALIFIED_PROVIDER_EVIDENCE_MISMATCH = (
        "qualified_provider_evidence_mismatch"
    )
    NORMALIZED_UNIT_BASIS_UNPROVEN = "normalized_unit_basis_unproven"


_SAFE_RAW_MARKER_PATTERN = r"^[A-Za-z0-9._:%+\-]{1,64}$"
_UNSAFE_ASSESSMENT_TEXT = (
    "credential",
    "error",
    "password",
    "path",
    "payload",
    "query",
    "secret",
    "token",
)


def _validate_safe_marker(value: str | None) -> str | None:
    if value is None:
        return None
    if any(marker in value.casefold() for marker in _UNSAFE_ASSESSMENT_TEXT):
        raise ValueError("indicator metadata marker contains unsafe operational text")
    return value


def _is_raw_unknown(*values: str | None) -> bool:
    return any(
        value is not None and value.upper() in {"UNKNOWN", "XXX"}
        for value in values
    )


class FinancialIndicatorProviderFieldMetadata(BaseModel):
    """Safe provider-origin metadata inputs; request intent is intentionally absent."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-indicator-metadata-compatibility-v1"] = (
        FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION
    )
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    provider_artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    normalized_field: str = Field(pattern=_SAFE_FIELD_PATTERN)
    unit_normalization_contract_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
    )
    value_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    raw_value_currency_marker: str | None = Field(
        default=None,
        pattern=_SAFE_RAW_MARKER_PATTERN,
    )
    filing_consolidation_scope: FinancialConsolidationScope | None = None
    raw_filing_consolidation_scope_marker: str | None = Field(
        default=None,
        pattern=_SAFE_RAW_MARKER_PATTERN,
    )

    @field_validator(
        "raw_value_currency_marker",
        "raw_filing_consolidation_scope_marker",
    )
    @classmethod
    def _safe_marker(cls, value: str | None) -> str | None:
        return _validate_safe_marker(value)

    @model_validator(mode="after")
    def _validate_provider_values(self) -> FinancialIndicatorProviderFieldMetadata:
        if self.value_currency in {"XXX"}:
            raise ValueError("unknown currency marker is not provider currency proof")
        if self.filing_consolidation_scope is FinancialConsolidationScope.UNKNOWN:
            raise ValueError("unknown filing scope is not provider scope proof")
        assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


class FinancialIndicatorMetadataDimensionAssessment(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-indicator-metadata-compatibility-v1"] = (
        FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION
    )
    dimension: FinancialIndicatorMetadataDimension
    raw_provider_marker: str | None = Field(
        default=None,
        pattern=_SAFE_RAW_MARKER_PATTERN,
    )
    raw_provider_value: str | None = Field(
        default=None,
        pattern=_SAFE_RAW_MARKER_PATTERN,
    )
    state: FinancialIndicatorSemanticAssessmentState
    safe_evidence_origin: FinancialIndicatorMetadataEvidenceOrigin
    expected_semantic_value: str | None = Field(
        default=None,
        pattern=_SAFE_RAW_MARKER_PATTERN,
    )
    evidence_value: str | None = Field(
        default=None,
        pattern=_SAFE_RAW_MARKER_PATTERN,
    )
    reason: FinancialIndicatorMetadataReason

    @field_validator(
        "raw_provider_marker",
        "raw_provider_value",
        "expected_semantic_value",
        "evidence_value",
    )
    @classmethod
    def _safe_values(cls, value: str | None) -> str | None:
        return _validate_safe_marker(value)

    @model_validator(mode="after")
    def _validate_state(self) -> FinancialIndicatorMetadataDimensionAssessment:
        raw_unknown = {
            value.upper()
            for value in (self.raw_provider_marker, self.raw_provider_value)
            if value is not None
        } & {"UNKNOWN", "XXX"}
        if self.state is FinancialIndicatorSemanticAssessmentState.PROVEN:
            if raw_unknown:
                raise ValueError("raw UNKNOWN/XXX can never assess as PROVEN")
            if (
                self.expected_semantic_value is None
                or self.evidence_value is None
                or self.expected_semantic_value.casefold()
                != self.evidence_value.casefold()
            ):
                raise ValueError("PROVEN metadata must match the expected value")
            if self.dimension is FinancialIndicatorMetadataDimension.INSTRUMENT_CURRENCY:
                if (
                    self.safe_evidence_origin
                    is not FinancialIndicatorMetadataEvidenceOrigin.AUTHORITATIVE_INSTRUMENT_IDENTITY
                    or self.reason
                    is not FinancialIndicatorMetadataReason.AUTHORITATIVE_INSTRUMENT_IDENTITY_PROVEN
                ):
                    raise ValueError(
                        "instrument currency must be proven by Instrument Identity"
                    )
            elif (
                self.safe_evidence_origin
                is not FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT
                or self.reason
                is not FinancialIndicatorMetadataReason.QUALIFIED_PROVIDER_EVIDENCE_PROVEN
            ):
                raise ValueError("provider metadata proof origin is inconsistent")
        elif self.state is FinancialIndicatorSemanticAssessmentState.UNPROVEN:
            if self.expected_semantic_value is None:
                raise ValueError("UNPROVEN material metadata requires an expectation")
            if (
                self.safe_evidence_origin
                is not FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT
                or self.reason
                not in {
                FinancialIndicatorMetadataReason.MATERIAL_EVIDENCE_UNPROVEN,
                FinancialIndicatorMetadataReason.QUALIFIED_PROVIDER_EVIDENCE_MISMATCH,
                FinancialIndicatorMetadataReason.NORMALIZED_UNIT_BASIS_UNPROVEN,
                }
            ):
                raise ValueError("UNPROVEN metadata reason is inconsistent")
        else:
            if (
                self.expected_semantic_value is not None
                or self.evidence_value is not None
                or self.safe_evidence_origin
                is not FinancialIndicatorMetadataEvidenceOrigin.FIELD_SEMANTICS_POLICY
                or self.reason
                is not FinancialIndicatorMetadataReason.FIELD_SEMANTICS_NOT_APPLICABLE
            ):
                raise ValueError("NOT_APPLICABLE must originate in field semantics")
        assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


class FinancialIndicatorFieldMetadataAssessment(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-indicator-metadata-compatibility-v1"] = (
        FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION
    )
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    provider_artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    normalized_field: str = Field(pattern=_SAFE_FIELD_PATTERN)
    unit_normalization_contract_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
    )
    field_class: FinancialIndicatorFieldClass
    canonical_unit_basis: FinancialIndicatorCanonicalBasis
    dimensions: tuple[FinancialIndicatorMetadataDimensionAssessment, ...]
    metadata_compatible: bool

    @model_validator(mode="after")
    def _validate_dimensions(self) -> FinancialIndicatorFieldMetadataAssessment:
        if tuple(item.dimension for item in self.dimensions) != tuple(
            FinancialIndicatorMetadataDimension
        ):
            raise ValueError("indicator metadata dimensions must be complete and canonical")
        compatible = all(
            item.state is not FinancialIndicatorSemanticAssessmentState.UNPROVEN
            for item in self.dimensions
        )
        if self.metadata_compatible != compatible:
            raise ValueError("field metadata compatibility is inconsistent")
        return self


class FinancialIndicatorMetadataCompatibilityAssessment(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-indicator-metadata-compatibility-v1"] = (
        FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION
    )
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    provider_artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    candidate_identity: str = Field(
        pattern=r"^financial-period-candidate:v1:[0-9a-f]{64}$"
    )
    period_identity: str = Field(pattern=r"^financial-period:v1:[0-9a-f]{64}$")
    family: FinancialRatioFamily
    period_end: date
    retained_field_set: tuple[str, ...]
    instrument_identity: InstrumentIdentityEvidence
    requested_value_currency: str = Field(pattern=r"^[A-Z]{3}$")
    requested_filing_consolidation_scope: FinancialConsolidationScope
    pit_as_of_date: date
    declaration_version: Literal[
        "financial-ratio-declarations-v1",
        "financial-ratio-declarations-v2",
        "financial-ratio-declarations-baostock-v1",
    ]
    field_semantics_version: Literal["financial-indicator-field-semantics-v1"] = (
        FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION
    )
    field_semantics_policy_identity: str = Field(
        pattern=r"^financial-indicator-field-semantics:v1:[0-9a-f]{64}$"
    )
    field_assessments: tuple[FinancialIndicatorFieldMetadataAssessment, ...]
    announcement_date: date | None = None
    first_publication_date: date | None = None
    revision_observed_at: datetime
    filing_metadata_observed_at: datetime
    compatible: bool
    current_only_metadata_eligible: bool
    strict_pit_metadata_eligible: bool
    assessment_identity: str = Field(
        pattern=r"^financial-indicator-metadata-compatibility:v1:[0-9a-f]{64}$"
    )

    @field_validator("revision_observed_at", "filing_metadata_observed_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("compatibility observation time must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_assessment(self) -> FinancialIndicatorMetadataCompatibilityAssessment:
        if (
            not self.instrument_identity.is_authoritative
            or self.instrument_identity.instrument_kind is not InstrumentKind.EQUITY
            or self.instrument_identity.currency != self.requested_value_currency
        ):
            raise ValueError("compatibility requires authoritative Instrument Identity")
        if self.requested_filing_consolidation_scope is FinancialConsolidationScope.UNKNOWN:
            raise ValueError("requested filing scope must be explicit intent")
        canonical_fields = tuple(sorted(set(self.retained_field_set)))
        assessment_fields = tuple(
            item.normalized_field for item in self.field_assessments
        )
        if (
            not canonical_fields
            or self.retained_field_set != canonical_fields
            or assessment_fields != canonical_fields
        ):
            raise ValueError("retained indicator fields must be complete and canonical")
        policy = financial_indicator_field_semantics_policy_v1()
        if self.field_semantics_policy_identity != policy.policy_identity:
            raise ValueError("field-semantics policy identity mismatch")
        for field_assessment in self.field_assessments:
            semantics = policy.lookup(
                self.family,
                field_assessment.normalized_field,
                declaration_version=self.declaration_version,
            )
            if (
                field_assessment.provider_id != self.provider_id
                or field_assessment.provider_artifact_identity
                != self.provider_artifact_identity
                or field_assessment.field_class is not semantics.field_class
                or field_assessment.canonical_unit_basis
                is not semantics.canonical_unit_basis
            ):
                raise ValueError("field assessment contradicts pinned semantics")
            by_dimension = {
                item.dimension: item for item in field_assessment.dimensions
            }
            instrument_currency = by_dimension[
                FinancialIndicatorMetadataDimension.INSTRUMENT_CURRENCY
            ]
            unit = by_dimension[FinancialIndicatorMetadataDimension.VALUE_UNIT]
            currency = by_dimension[FinancialIndicatorMetadataDimension.VALUE_CURRENCY]
            scope = by_dimension[
                FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE
            ]
            if (
                instrument_currency.state
                is not FinancialIndicatorSemanticAssessmentState.PROVEN
                or instrument_currency.expected_semantic_value
                != self.instrument_identity.currency
                or instrument_currency.evidence_value
                != self.instrument_identity.currency
                or instrument_currency.raw_provider_marker is not None
                or instrument_currency.raw_provider_value is not None
            ):
                raise ValueError(
                    "instrument currency contradicts authoritative Instrument Identity"
                )
            valid_original_units = {
                FinancialIndicatorFieldClass.DIMENSIONLESS_RATIO: {
                    "PERCENT",
                    "RATE",
                    "RATIO",
                },
                FinancialIndicatorFieldClass.PERCENTAGE_RATE: {
                    "PERCENT",
                    "RATE",
                    "RATIO",
                },
                FinancialIndicatorFieldClass.PER_SHARE: {"CURRENCY_PER_SHARE"},
                FinancialIndicatorFieldClass.MONETARY: {"CURRENCY_BASE_UNIT"},
                FinancialIndicatorFieldClass.SHARE_COUNT: {"SHARES"},
                FinancialIndicatorFieldClass.DAYS_TURNOVER_TIME: {
                    semantics.canonical_unit_basis.value,
                },
                FinancialIndicatorFieldClass.OTHER: set(),
            }[semantics.field_class]
            if unit.expected_semantic_value != semantics.canonical_unit_basis.value:
                raise ValueError("value-unit expectation contradicts pinned semantics")
            if unit.state is FinancialIndicatorSemanticAssessmentState.PROVEN and (
                unit.evidence_value != semantics.canonical_unit_basis.value
                or unit.raw_provider_value != semantics.canonical_unit_basis.value
                or unit.raw_provider_marker is None
                or unit.raw_provider_marker.upper() not in valid_original_units
            ):
                raise ValueError("proven value-unit basis contradicts pinned semantics")
            if (
                currency.state
                is FinancialIndicatorSemanticAssessmentState.NOT_APPLICABLE
            ) != (
                semantics.value_currency_applicability
                is FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
            ):
                raise ValueError("value-currency applicability contradicts semantics")
            if (
                scope.state
                is FinancialIndicatorSemanticAssessmentState.NOT_APPLICABLE
            ) == semantics.filing_scope_material:
                raise ValueError("filing-scope materiality contradicts semantics")
            if semantics.value_currency_applicability is (
                FinancialIndicatorSemanticApplicability.REQUIRED
            ):
                if currency.expected_semantic_value != self.requested_value_currency:
                    raise ValueError(
                        "value-currency expectation contradicts requested metadata"
                    )
                canonical_currency_marker = (
                    currency.raw_provider_marker.upper()
                    if currency.raw_provider_marker is not None
                    and len(currency.raw_provider_marker) == 3
                    and currency.raw_provider_marker.isalpha()
                    else None
                )
                if currency.state is FinancialIndicatorSemanticAssessmentState.PROVEN and (
                    currency.evidence_value != self.requested_value_currency
                    or currency.raw_provider_value != currency.evidence_value
                    or (
                        canonical_currency_marker is not None
                        and canonical_currency_marker != currency.evidence_value
                    )
                ):
                    raise ValueError(
                        "proven value currency contradicts provider evidence"
                    )
            if semantics.filing_scope_material:
                expected_scope = self.requested_filing_consolidation_scope.value
                if scope.expected_semantic_value != expected_scope:
                    raise ValueError(
                        "filing-scope expectation contradicts requested metadata"
                    )
                canonical_scope_marker = (
                    scope.raw_provider_marker.casefold()
                    if scope.raw_provider_marker is not None
                    and scope.raw_provider_marker.casefold()
                    in {
                        FinancialConsolidationScope.CONSOLIDATED.value,
                        FinancialConsolidationScope.PARENT.value,
                    }
                    else None
                )
                if scope.state is FinancialIndicatorSemanticAssessmentState.PROVEN and (
                    scope.evidence_value != expected_scope
                    or scope.raw_provider_value != scope.evidence_value
                    or (
                        canonical_scope_marker is not None
                        and canonical_scope_marker != scope.evidence_value
                    )
                ):
                    raise ValueError(
                        "proven filing scope contradicts provider evidence"
                    )
        compatible = all(item.metadata_compatible for item in self.field_assessments)
        if self.compatible != compatible:
            raise ValueError("candidate metadata compatibility is inconsistent")
        expected_current = bool(
            compatible
            and self.announcement_date is not None
            and self.announcement_date <= self.pit_as_of_date
        )
        expected_strict = bool(
            expected_current
            and self.first_publication_date is not None
            and self.first_publication_date <= self.pit_as_of_date
            and self.revision_observed_at.date() <= self.pit_as_of_date
            and self.filing_metadata_observed_at.date() <= self.pit_as_of_date
        )
        if (
            self.current_only_metadata_eligible != expected_current
            or self.strict_pit_metadata_eligible != expected_strict
        ):
            raise ValueError("metadata availability eligibility is inconsistent")
        payload = self.model_dump(mode="json", exclude={"assessment_identity"})
        expected_identity = _compatibility_identity(payload)
        if self.assessment_identity != expected_identity:
            raise ValueError("metadata compatibility assessment identity mismatch")
        assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


def _compatibility_identity(payload: object) -> str:
    digest = sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_IDENTITY_NAMESPACE}:{digest}"


def _proven(
    dimension: FinancialIndicatorMetadataDimension,
    *,
    expected: str,
    evidence: str,
    raw_marker: str | None,
    raw_value: str | None,
    origin: FinancialIndicatorMetadataEvidenceOrigin,
    reason: FinancialIndicatorMetadataReason,
) -> FinancialIndicatorMetadataDimensionAssessment:
    return FinancialIndicatorMetadataDimensionAssessment(
        dimension=dimension,
        raw_provider_marker=raw_marker,
        raw_provider_value=raw_value,
        state=FinancialIndicatorSemanticAssessmentState.PROVEN,
        safe_evidence_origin=origin,
        expected_semantic_value=expected,
        evidence_value=evidence,
        reason=reason,
    )


def _unproven(
    dimension: FinancialIndicatorMetadataDimension,
    *,
    expected: str,
    evidence: str | None,
    raw_marker: str | None,
    raw_value: str | None,
    reason: FinancialIndicatorMetadataReason,
) -> FinancialIndicatorMetadataDimensionAssessment:
    return FinancialIndicatorMetadataDimensionAssessment(
        dimension=dimension,
        raw_provider_marker=raw_marker,
        raw_provider_value=raw_value,
        state=FinancialIndicatorSemanticAssessmentState.UNPROVEN,
        safe_evidence_origin=FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT,
        expected_semantic_value=expected,
        evidence_value=evidence,
        reason=reason,
    )


def _not_applicable(
    dimension: FinancialIndicatorMetadataDimension,
    *,
    raw_marker: str | None,
    raw_value: str | None,
) -> FinancialIndicatorMetadataDimensionAssessment:
    return FinancialIndicatorMetadataDimensionAssessment(
        dimension=dimension,
        raw_provider_marker=raw_marker,
        raw_provider_value=raw_value,
        state=FinancialIndicatorSemanticAssessmentState.NOT_APPLICABLE,
        safe_evidence_origin=(
            FinancialIndicatorMetadataEvidenceOrigin.FIELD_SEMANTICS_POLICY
        ),
        expected_semantic_value=None,
        evidence_value=None,
        reason=FinancialIndicatorMetadataReason.FIELD_SEMANTICS_NOT_APPLICABLE,
    )


def _unit_assessment(
    field: FinancialFieldValue,
    semantics: FinancialIndicatorFieldSemantics,
    metadata: FinancialIndicatorProviderFieldMetadata,
) -> FinancialIndicatorMetadataDimensionAssessment:
    expected = semantics.canonical_unit_basis.value
    normalized_unit = field.normalized_unit
    original_unit = field.original_unit
    valid_original_units = {
        FinancialIndicatorFieldClass.DIMENSIONLESS_RATIO: {
            "PERCENT",
            "RATE",
            "RATIO",
        },
        FinancialIndicatorFieldClass.PERCENTAGE_RATE: {
            "PERCENT",
            "RATE",
            "RATIO",
        },
        FinancialIndicatorFieldClass.PER_SHARE: {"CURRENCY_PER_SHARE"},
        FinancialIndicatorFieldClass.MONETARY: {"CURRENCY_BASE_UNIT"},
        FinancialIndicatorFieldClass.SHARE_COUNT: {"SHARES"},
        FinancialIndicatorFieldClass.DAYS_TURNOVER_TIME: {
            semantics.canonical_unit_basis.value,
        },
        FinancialIndicatorFieldClass.OTHER: set(),
    }[semantics.field_class]
    if (
        normalized_unit == expected
        and original_unit is not None
        and original_unit.upper() in valid_original_units
        and not _is_raw_unknown(original_unit, normalized_unit)
        and bool(metadata.unit_normalization_contract_version)
    ):
        return _proven(
            FinancialIndicatorMetadataDimension.VALUE_UNIT,
            expected=expected,
            evidence=normalized_unit,
            raw_marker=original_unit,
            raw_value=normalized_unit,
            origin=FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT,
            reason=FinancialIndicatorMetadataReason.QUALIFIED_PROVIDER_EVIDENCE_PROVEN,
        )
    return _unproven(
        FinancialIndicatorMetadataDimension.VALUE_UNIT,
        expected=expected,
        evidence=normalized_unit,
        raw_marker=original_unit,
        raw_value=normalized_unit,
        reason=FinancialIndicatorMetadataReason.NORMALIZED_UNIT_BASIS_UNPROVEN,
    )


def _value_currency_assessment(
    semantics: FinancialIndicatorFieldSemantics,
    metadata: FinancialIndicatorProviderFieldMetadata,
    requested_value_currency: str,
) -> FinancialIndicatorMetadataDimensionAssessment:
    if (
        semantics.value_currency_applicability
        is FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
    ):
        return _not_applicable(
            FinancialIndicatorMetadataDimension.VALUE_CURRENCY,
            raw_marker=metadata.raw_value_currency_marker,
            raw_value=metadata.value_currency,
        )
    actual = metadata.value_currency
    marker_unknown = _is_raw_unknown(metadata.raw_value_currency_marker, actual)
    marker = metadata.raw_value_currency_marker
    marker_conflict = bool(
        marker is not None
        and len(marker) == 3
        and marker.isalpha()
        and marker.upper() != requested_value_currency
    )
    if actual == requested_value_currency and not marker_unknown and not marker_conflict:
        return _proven(
            FinancialIndicatorMetadataDimension.VALUE_CURRENCY,
            expected=requested_value_currency,
            evidence=actual,
            raw_marker=metadata.raw_value_currency_marker,
            raw_value=actual,
            origin=FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT,
            reason=FinancialIndicatorMetadataReason.QUALIFIED_PROVIDER_EVIDENCE_PROVEN,
        )
    return _unproven(
        FinancialIndicatorMetadataDimension.VALUE_CURRENCY,
        expected=requested_value_currency,
        evidence=actual,
        raw_marker=metadata.raw_value_currency_marker,
        raw_value=actual,
        reason=(
            FinancialIndicatorMetadataReason.MATERIAL_EVIDENCE_UNPROVEN
            if actual is None or marker_unknown
            else FinancialIndicatorMetadataReason.QUALIFIED_PROVIDER_EVIDENCE_MISMATCH
        ),
    )


def _scope_assessment(
    semantics: FinancialIndicatorFieldSemantics,
    metadata: FinancialIndicatorProviderFieldMetadata,
    requested_scope: FinancialConsolidationScope,
) -> FinancialIndicatorMetadataDimensionAssessment:
    if not semantics.filing_scope_material:
        return _not_applicable(
            FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE,
            raw_marker=metadata.raw_filing_consolidation_scope_marker,
            raw_value=(
                metadata.filing_consolidation_scope.value
                if metadata.filing_consolidation_scope is not None
                else None
            ),
        )
    actual = metadata.filing_consolidation_scope
    marker_unknown = _is_raw_unknown(
        metadata.raw_filing_consolidation_scope_marker,
        actual.value if actual is not None else None,
    )
    marker = metadata.raw_filing_consolidation_scope_marker
    canonical_marker = marker.casefold() if marker is not None else None
    marker_conflict = bool(
        canonical_marker in {
            FinancialConsolidationScope.CONSOLIDATED.value,
            FinancialConsolidationScope.PARENT.value,
        }
        and canonical_marker != requested_scope.value
    )
    if actual is requested_scope and not marker_unknown and not marker_conflict:
        return _proven(
            FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE,
            expected=requested_scope.value,
            evidence=actual.value,
            raw_marker=metadata.raw_filing_consolidation_scope_marker,
            raw_value=actual.value,
            origin=FinancialIndicatorMetadataEvidenceOrigin.PROVIDER_ARTIFACT,
            reason=FinancialIndicatorMetadataReason.QUALIFIED_PROVIDER_EVIDENCE_PROVEN,
        )
    return _unproven(
        FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE,
        expected=requested_scope.value,
        evidence=actual.value if actual is not None else None,
        raw_marker=metadata.raw_filing_consolidation_scope_marker,
        raw_value=actual.value if actual is not None else None,
        reason=(
            FinancialIndicatorMetadataReason.MATERIAL_EVIDENCE_UNPROVEN
            if actual is None or marker_unknown
            else FinancialIndicatorMetadataReason.QUALIFIED_PROVIDER_EVIDENCE_MISMATCH
        ),
    )


def _field_assessment(
    *,
    field: FinancialFieldValue,
    semantics: FinancialIndicatorFieldSemantics,
    metadata: FinancialIndicatorProviderFieldMetadata,
    instrument_currency: str,
    requested_value_currency: str,
    requested_scope: FinancialConsolidationScope,
) -> FinancialIndicatorFieldMetadataAssessment:
    dimensions = (
        _proven(
            FinancialIndicatorMetadataDimension.INSTRUMENT_CURRENCY,
            expected=instrument_currency,
            evidence=instrument_currency,
            raw_marker=None,
            raw_value=None,
            origin=(
                FinancialIndicatorMetadataEvidenceOrigin.AUTHORITATIVE_INSTRUMENT_IDENTITY
            ),
            reason=(
                FinancialIndicatorMetadataReason.AUTHORITATIVE_INSTRUMENT_IDENTITY_PROVEN
            ),
        ),
        _unit_assessment(field, semantics, metadata),
        _value_currency_assessment(
            semantics,
            metadata,
            requested_value_currency,
        ),
        _scope_assessment(semantics, metadata, requested_scope),
    )
    return FinancialIndicatorFieldMetadataAssessment(
        provider_id=metadata.provider_id,
        provider_artifact_identity=metadata.provider_artifact_identity,
        normalized_field=field.normalized_field,
        unit_normalization_contract_version=(
            metadata.unit_normalization_contract_version
        ),
        field_class=semantics.field_class,
        canonical_unit_basis=semantics.canonical_unit_basis,
        dimensions=dimensions,
        metadata_compatible=all(
            item.state is not FinancialIndicatorSemanticAssessmentState.UNPROVEN
            for item in dimensions
        ),
    )


def assess_financial_indicator_metadata_compatibility(
    *,
    candidate: FinancialPeriodCandidate,
    family: FinancialRatioFamily,
    instrument_identity: InstrumentIdentityEvidence,
    requested_value_currency: str,
    requested_filing_consolidation_scope: FinancialConsolidationScope,
    pit_as_of_date: date,
    declaration_version: str,
    provider_field_metadata: Sequence[FinancialIndicatorProviderFieldMetadata],
) -> FinancialIndicatorMetadataCompatibilityAssessment:
    """Assess one retained candidate without using requested metadata as evidence."""

    if (
        candidate.period_identity.capability is not FinancialCapability.RATIO_FAMILY
        or candidate.period_identity.ratio_family is not family
        or candidate.period_identity.instrument_identity != instrument_identity
        or candidate.artifact.artifact_identity
        != candidate.period_identity.provider_revision_binding
    ):
        raise ValueError("indicator compatibility candidate binding mismatch")
    if (
        not instrument_identity.is_authoritative
        or instrument_identity.instrument_kind is not InstrumentKind.EQUITY
        or requested_value_currency != instrument_identity.currency
        or requested_filing_consolidation_scope is FinancialConsolidationScope.UNKNOWN
    ):
        raise ValueError("indicator compatibility request intent is invalid")
    fields = tuple(sorted(candidate.fields, key=lambda item: item.normalized_field))
    metadata_items = tuple(
        sorted(provider_field_metadata, key=lambda item: item.normalized_field)
    )
    metadata_by_field = {item.normalized_field: item for item in metadata_items}
    field_names = tuple(item.normalized_field for item in fields)
    if (
        len(metadata_by_field) != len(metadata_items)
        or tuple(metadata_by_field) != field_names
        or any(
            item.provider_id != candidate.artifact.dataset.provider_id
            or item.provider_artifact_identity
            != candidate.artifact.artifact_identity
            for item in metadata_items
        )
    ):
        raise ValueError(
            "provider metadata must match the retained field set and artifact binding"
        )
    field_assessments = tuple(
        _field_assessment(
            field=field,
            semantics=financial_indicator_field_semantics(
                family,
                field.normalized_field,
                declaration_version=declaration_version,
            ),
            metadata=metadata_by_field[field.normalized_field],
            instrument_currency=instrument_identity.currency,
            requested_value_currency=requested_value_currency,
            requested_scope=requested_filing_consolidation_scope,
        )
        for field in fields
    )
    compatible = all(item.metadata_compatible for item in field_assessments)
    announcement_date = candidate.filing_metadata.ann_date
    first_publication_date = candidate.filing_metadata.f_ann_date
    revision_observed_at = candidate.artifact.observed_at
    filing_metadata_observed_at = candidate.filing_metadata.observed_at
    current_eligible = bool(
        compatible
        and announcement_date is not None
        and announcement_date <= pit_as_of_date
    )
    strict_eligible = bool(
        current_eligible
        and first_publication_date is not None
        and first_publication_date <= pit_as_of_date
        and revision_observed_at.date() <= pit_as_of_date
        and filing_metadata_observed_at.date() <= pit_as_of_date
    )
    policy = financial_indicator_field_semantics_policy_v1()
    payload = {
        "contract_version": FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION,
        "provider_id": candidate.artifact.dataset.provider_id,
        "provider_artifact_identity": candidate.artifact.artifact_identity,
        "candidate_identity": candidate.candidate_identity,
        "period_identity": candidate.period_identity.period_identity,
        "family": family,
        "period_end": candidate.period_identity.period_end,
        "retained_field_set": field_names,
        "instrument_identity": instrument_identity,
        "requested_value_currency": requested_value_currency,
        "requested_filing_consolidation_scope": requested_filing_consolidation_scope,
        "pit_as_of_date": pit_as_of_date,
        "declaration_version": declaration_version,
        "field_semantics_version": FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION,
        "field_semantics_policy_identity": policy.policy_identity,
        "field_assessments": field_assessments,
        "announcement_date": announcement_date,
        "first_publication_date": first_publication_date,
        "revision_observed_at": revision_observed_at,
        "filing_metadata_observed_at": filing_metadata_observed_at,
        "compatible": compatible,
        "current_only_metadata_eligible": current_eligible,
        "strict_pit_metadata_eligible": strict_eligible,
    }
    identity_payload = FinancialIndicatorMetadataCompatibilityAssessment.model_construct(
        **payload,
        assessment_identity=(
            f"{FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_IDENTITY_NAMESPACE}:"
            + "0" * 64
        ),
    ).model_dump(mode="json", exclude={"assessment_identity"})
    return FinancialIndicatorMetadataCompatibilityAssessment(
        **payload,
        assessment_identity=_compatibility_identity(identity_payload),
    )


def evaluate_financial_indicator_candidates(
    *,
    candidates: Sequence[FinancialPeriodCandidate],
    instrument_identity: InstrumentIdentityEvidence,
    requested_value_currency: str,
    requested_filing_consolidation_scope: FinancialConsolidationScope,
    pit_as_of_date: date,
    declaration_version: str,
    normalizer_contract_version: str,
) -> tuple[FinancialIndicatorMetadataCompatibilityAssessment, ...]:
    """Evaluate truthful candidate-bound metadata through one provider-neutral seam."""

    assessments: list[FinancialIndicatorMetadataCompatibilityAssessment] = []
    for candidate in sorted(candidates, key=lambda item: item.candidate_identity):
        if not candidate.fields:
            continue
        identity = candidate.period_identity
        family = identity.ratio_family
        if family is None:
            raise ValueError("indicator metadata evaluation requires a ratio family")
        raw_currency = identity.currency.upper()
        raw_scope = identity.consolidation_scope
        provider_metadata = tuple(
            FinancialIndicatorProviderFieldMetadata(
                provider_id=candidate.artifact.dataset.provider_id,
                provider_artifact_identity=candidate.artifact.artifact_identity,
                normalized_field=field.normalized_field,
                unit_normalization_contract_version=normalizer_contract_version,
                value_currency=(
                    None if raw_currency in {"UNKNOWN", "XXX"} else raw_currency
                ),
                raw_value_currency_marker=raw_currency,
                filing_consolidation_scope=(
                    None
                    if raw_scope is FinancialConsolidationScope.UNKNOWN
                    else raw_scope
                ),
                raw_filing_consolidation_scope_marker=raw_scope.value.upper(),
            )
            for field in candidate.fields
        )
        assessments.append(
            assess_financial_indicator_metadata_compatibility(
                candidate=candidate,
                family=family,
                instrument_identity=instrument_identity,
                requested_value_currency=requested_value_currency,
                requested_filing_consolidation_scope=(
                    requested_filing_consolidation_scope
                ),
                pit_as_of_date=pit_as_of_date,
                declaration_version=declaration_version,
                provider_field_metadata=provider_metadata,
            )
        )
    return tuple(assessments)


__all__ = [
    "FINANCIAL_INDICATOR_FIELD_SEMANTICS_IDENTITY_NAMESPACE",
    "FINANCIAL_INDICATOR_FIELD_SEMANTICS_REGISTRY",
    "FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION",
    "FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_IDENTITY_NAMESPACE",
    "FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION",
    "FinancialIndicatorCanonicalBasis",
    "FinancialIndicatorClassRule",
    "FinancialIndicatorFieldClass",
    "FinancialIndicatorFieldSemantics",
    "FinancialIndicatorFieldSemanticsPolicy",
    "FinancialIndicatorDeclarationSemanticAlias",
    "FinancialIndicatorFieldMetadataAssessment",
    "FinancialIndicatorMetadataCompatibilityAssessment",
    "FinancialIndicatorMetadataDimension",
    "FinancialIndicatorMetadataDimensionAssessment",
    "FinancialIndicatorMetadataEvidenceOrigin",
    "FinancialIndicatorMetadataReason",
    "FinancialIndicatorProviderFieldMetadata",
    "FinancialIndicatorRequiredProviderEvidence",
    "FinancialIndicatorSemanticApplicability",
    "FinancialIndicatorSemanticAssessmentState",
    "assess_financial_indicator_metadata_compatibility",
    "evaluate_financial_indicator_candidates",
    "financial_indicator_class_rule",
    "financial_indicator_field_semantics",
    "financial_indicator_field_semantics_policy_v1",
]

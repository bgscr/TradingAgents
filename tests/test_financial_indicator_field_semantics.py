from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_MANIFEST_V3,
    FINANCIAL_PERIOD_SELECTION_V3,
    FINANCIAL_PROVIDER_CANDIDATE_V2,
    FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
    FINANCIAL_RATIO_DECLARATION_V1,
    FINANCIAL_RATIO_DECLARATION_V2,
    FinancialRatioFamily,
)
from tradingagents.dataflows.financial_indicator_metadata import (
    FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION,
    FinancialIndicatorCanonicalBasis,
    FinancialIndicatorFieldClass,
    FinancialIndicatorFieldSemanticsPolicy,
    FinancialIndicatorSemanticApplicability,
    financial_indicator_class_rule,
    financial_indicator_field_semantics,
    financial_indicator_field_semantics_policy_v1,
)

_EXPECTED_FIELDS = {
    FinancialRatioFamily.PROFIT: {
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
    },
    FinancialRatioFamily.OPERATION: {
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
    },
    FinancialRatioFamily.GROWTH: {
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
    },
    FinancialRatioFamily.BALANCE: {
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
    },
    FinancialRatioFamily.CASH_FLOW: {
        "cash_flow_adequacy",
        "cash_flow_to_debt",
        "cash_flow_to_gross_revenue",
        "cash_flow_to_liabilities",
        "cash_flow_to_revenue",
        "cash_return_on_assets",
        "current_assets_to_assets",
        "free_cash_flow_margin",
        "interest_coverage",
        "non_current_assets_to_assets",
        "operating_cash_flow_ratio",
        "quality_of_income",
        "reinvestment_ratio",
        "self_financing_ratio",
        "tangible_asset_ratio",
    },
    FinancialRatioFamily.DUPONT: {
        "asset_turnover_component",
        "debt_burden",
        "ebit_margin_component",
        "equity_multiplier_component",
        "financial_leverage",
        "interest_burden",
        "net_profit_margin_component",
        "operating_margin_component",
        "parent_net_income_share",
        "return_on_assets_component",
        "return_on_equity_component",
        "tax_burden",
    },
}


def test_v1_registry_is_exact_closed_immutable_and_complete() -> None:
    policy = financial_indicator_field_semantics_policy_v1()
    pairs = {(entry.family, entry.normalized_field) for entry in policy.entries}

    assert policy.contract_version == FINANCIAL_INDICATOR_FIELD_SEMANTICS_VERSION
    assert len(policy.entries) == 70
    assert pairs == {
        (family, field)
        for family, fields in _EXPECTED_FIELDS.items()
        for field in fields
    }
    assert len({entry.normalized_field for entry in policy.entries}) == 68
    assert Counter(entry.normalized_field for entry in policy.entries) == Counter(
        {
            **{
                field: 1
                for fields in _EXPECTED_FIELDS.values()
                for field in fields
            },
            "interest_coverage": 2,
            "tangible_asset_ratio": 2,
        }
    )
    with pytest.raises(ValidationError):
        policy.entries = ()


def test_registry_identity_is_order_independent_and_declarations_are_bound() -> None:
    policy = financial_indicator_field_semantics_policy_v1()
    reordered = FinancialIndicatorFieldSemanticsPolicy.create(
        tuple(reversed(policy.entries))
    )

    assert reordered == policy
    assert policy.policy_identity.startswith(
        "financial-indicator-field-semantics:v1:"
    )
    assert {
        version
        for entry in policy.entries
        for version in entry.declaration_versions
    } == {
        FINANCIAL_RATIO_DECLARATION_V1,
        FINANCIAL_RATIO_DECLARATION_V2,
        FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
    }


def test_registry_class_rules_are_explicit_and_not_response_derived() -> None:
    ratio = financial_indicator_field_semantics(
        FinancialRatioFamily.BALANCE,
        "current_ratio",
        declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
    )
    rate = financial_indicator_field_semantics(
        FinancialRatioFamily.PROFIT,
        "gross_margin",
        declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
    )
    per_share = financial_indicator_field_semantics(
        FinancialRatioFamily.PROFIT,
        "earnings_per_share",
        declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
    )
    days = financial_indicator_field_semantics(
        FinancialRatioFamily.OPERATION,
        "inventory_days",
        declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
    )
    turns = financial_indicator_field_semantics(
        FinancialRatioFamily.OPERATION,
        "inventory_turnover",
        declaration_version=FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
    )

    assert ratio.field_class is FinancialIndicatorFieldClass.DIMENSIONLESS_RATIO
    assert rate.field_class is FinancialIndicatorFieldClass.PERCENTAGE_RATE
    assert ratio.canonical_unit_basis is FinancialIndicatorCanonicalBasis.RATIO
    assert rate.canonical_unit_basis is FinancialIndicatorCanonicalBasis.RATIO
    assert (
        ratio.value_currency_applicability
        is FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
    )
    assert (
        rate.value_currency_applicability
        is FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
    )
    assert per_share.field_class is FinancialIndicatorFieldClass.PER_SHARE
    assert (
        per_share.value_currency_applicability
        is FinancialIndicatorSemanticApplicability.REQUIRED
    )
    assert (
        per_share.canonical_unit_basis
        is FinancialIndicatorCanonicalBasis.CURRENCY_PER_SHARE
    )
    assert days.canonical_unit_basis is FinancialIndicatorCanonicalBasis.DAYS
    assert (
        turns.canonical_unit_basis
        is FinancialIndicatorCanonicalBasis.TURNS_PER_REPORTING_PERIOD
    )
    assert all(entry.filing_scope_material for entry in (ratio, rate, per_share, days, turns))


def test_baostock_legacy_declared_family_is_explicitly_bound_to_growth_semantics() -> None:
    semantics = financial_indicator_field_semantics(
        FinancialRatioFamily.BALANCE,
        "liability_growth",
        declaration_version=FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
    )

    assert semantics.family is FinancialRatioFamily.GROWTH
    assert semantics.field_class is FinancialIndicatorFieldClass.PERCENTAGE_RATE


def test_future_class_rules_exist_but_other_fails_closed() -> None:
    monetary = financial_indicator_class_rule(FinancialIndicatorFieldClass.MONETARY)
    share_count = financial_indicator_class_rule(
        FinancialIndicatorFieldClass.SHARE_COUNT
    )
    other = financial_indicator_class_rule(FinancialIndicatorFieldClass.OTHER)

    assert monetary.value_currency_applicability is FinancialIndicatorSemanticApplicability.REQUIRED
    assert monetary.canonical_unit_basis is FinancialIndicatorCanonicalBasis.CURRENCY_BASE_UNIT
    assert share_count.value_currency_applicability is FinancialIndicatorSemanticApplicability.NOT_APPLICABLE
    assert share_count.canonical_unit_basis is FinancialIndicatorCanonicalBasis.SHARES
    assert other.current_only_eligible is False
    assert other.strict_pit_eligible is False


def test_forward_indicator_selection_bindings_are_named_without_persistence() -> None:
    assert FINANCIAL_PERIOD_SELECTION_V3 == "financial-period-selection-v3"
    assert FINANCIAL_PROVIDER_CANDIDATE_V2 == "financial-provider-candidate-v2"
    assert FINANCIAL_MANIFEST_V3 == "financial-manifest-v3"


@pytest.mark.parametrize(
    ("family", "field", "version"),
    [
        (FinancialRatioFamily.PROFIT, "unknown_field", FINANCIAL_RATIO_DECLARATION_V1),
        (FinancialRatioFamily.PROFIT, "gross_margin", "financial-ratio-declarations-v99"),
        (FinancialRatioFamily.DUPONT, "earnings_per_share", FINANCIAL_RATIO_DECLARATION_V1),
    ],
)
def test_unknown_or_unregistered_field_semantics_fail_closed(
    family: FinancialRatioFamily,
    field: str,
    version: str,
) -> None:
    with pytest.raises(ValueError, match="not registered"):
        financial_indicator_field_semantics(
            family,
            field,
            declaration_version=version,
        )

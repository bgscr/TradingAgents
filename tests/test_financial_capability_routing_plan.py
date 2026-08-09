from __future__ import annotations

import copy
import importlib.util
import json
from hashlib import sha256

import pytest

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    CAPABILITY_ROUTING_PLAN_VERSION,
    CAPABILITY_ROUTING_PLAN_VERSION_V2,
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
    MainlandCapabilityRoutingRunProjection,
    preflight_mainland_capability_routing,
)
from tradingagents.default_config import DEFAULT_CONFIG


def _qualified_plan(monkeypatch: pytest.MonkeyPatch) -> MainlandCapabilityRoutingPlan:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": [
                "statements",
                "financial_indicators",
                "adjustment_factors",
                "name_events",
            ],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    asset = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    result = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-secret"},
    )
    assert result.plan is not None
    return result.plan


def _resign(payload: dict[str, object]) -> None:
    material = dict(payload)
    material.pop("plan_signature", None)
    digest = sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    payload["plan_signature"] = f"mainland-routing-plan:v1:{digest}"


def test_plan_1_2_binds_exact_indicator_contracts_without_policy_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)

    assert CAPABILITY_ROUTING_PLAN_VERSION == "1.2"
    assert plan.plan_version == "1.2"
    assert plan.indicator_field_semantics_version == (
        "financial-indicator-field-semantics-v1"
    )
    assert plan.indicator_metadata_compatibility_version == (
        "financial-indicator-metadata-compatibility-v1"
    )
    assert plan.indicator_manifest_contract_version == "financial-manifest-v3"
    assert plan.indicator_selection_contract_version == (
        "financial-period-selection-v3"
    )
    assert plan.manifest_contract_version == "financial-manifest-v2"
    assert plan.selection_contract_version == (
        "financial-period-selection-statement-v2"
    )
    assert plan.eligibility_policy_version == (
        "mainland-financial-period-eligibility-v2"
    )
    assert plan.completeness_policy_version == (
        "mainland-financial-completeness-v2"
    )
    assert plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT) == (
        "akshare",
        "baostock",
        "yfinance",
    )
    assert plan.route_for(MainlandCapability.FINANCIAL_INDICATORS) == (
        "tushare",
        "akshare",
        "baostock_qualified_families",
        "yfinance",
    )
    assert plan.plan_signature.startswith("mainland-routing-plan:v1:")


def test_historical_plan_1_1_remains_readable_under_recorded_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _qualified_plan(monkeypatch)
    historical_payload = current.model_dump(mode="json")
    historical_payload["plan_version"] = CAPABILITY_ROUTING_PLAN_VERSION_V2
    for key in (
        "indicator_field_semantics_version",
        "indicator_metadata_compatibility_version",
        "indicator_manifest_contract_version",
        "indicator_selection_contract_version",
    ):
        historical_payload.pop(key)
    _resign(historical_payload)

    historical = MainlandCapabilityRoutingPlan.model_validate(historical_payload)
    projection = MainlandCapabilityRoutingRunProjection.from_plan(historical)

    assert historical.plan_version == "1.1"
    assert historical.indicator_field_semantics_version is None
    assert historical.indicator_metadata_compatibility_version is None
    assert historical.indicator_manifest_contract_version is None
    assert historical.indicator_selection_contract_version is None
    assert projection.plan_version == "1.1"
    assert projection.indicator_metadata_compatibility_version is None
    assert historical.plan_signature != current.plan_signature
    assert historical.routes == current.routes
    assert historical.completeness_policy_version == current.completeness_policy_version
    assert historical.eligibility_policy_version == current.eligibility_policy_version

from __future__ import annotations

import copy
import importlib.util
import json
from hashlib import sha256

import pytest

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    CAPABILITY_ROUTING_PLAN_VERSION,
    CAPABILITY_ROUTING_PLAN_VERSION_1_3,
    CAPABILITY_ROUTING_PLAN_VERSION_1_4,
    CAPABILITY_ROUTING_PLAN_VERSION_V2,
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
    MainlandCapabilityRoutingRunProjection,
    preflight_mainland_capability_routing,
    prepare_mainland_temporal_capability_routing_plan,
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


def test_plan_1_4_binds_exact_acquisition_window_versions_without_activating_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _qualified_plan(monkeypatch)
    plan = prepare_mainland_temporal_capability_routing_plan(predecessor)

    assert CAPABILITY_ROUTING_PLAN_VERSION == "1.2"
    assert CAPABILITY_ROUTING_PLAN_VERSION_1_3 == "1.3"
    assert CAPABILITY_ROUTING_PLAN_VERSION_1_4 == "1.4"
    assert predecessor.plan_version == "1.2"
    assert plan.plan_version == "1.4"
    assert plan.routes == predecessor.routes
    assert plan.financial_eligibility_binding == predecessor.financial_eligibility_binding
    assert plan.indicator_field_semantics_version == (
        predecessor.indicator_field_semantics_version
    )
    assert plan.indicator_metadata_compatibility_version == (
        predecessor.indicator_metadata_compatibility_version
    )
    assert plan.indicator_manifest_contract_version == "financial-manifest-v3"
    assert plan.indicator_selection_contract_version == (
        "financial-period-selection-v3"
    )
    assert plan.completeness_policy_version == predecessor.completeness_policy_version
    assert plan.plan_signature.startswith("mainland-routing-plan:v1:")
    assert plan.plan_signature != predecessor.plan_signature

    temporal = plan.temporal_contract_bindings
    assert temporal is not None
    assert temporal.model_dump(mode="json") == {
        "acquisition_window_version": "financial-eligibility-acquisition-window-v1",
        "aggregate_completeness_version": "financial-aggregate-completeness-v5",
        "calendar_eligibility_binding_version": "market-session-eligibility-binding-v2",
        "company_type_evidence_binding_version": "financial-company-type-evidence-binding-v4",
        "company_type_evidence_projection_version": "financial-company-type-evidence-projection-v3",
        "company_type_provider_response_version": "financial-company-type-evidence-provider-response-v3",
        "company_type_routing_request_version": "financial-company-type-evidence-routing-request-v3",
        "completeness_row_version": "financial-completeness-row-v6",
        "eligibility_metadata_binding_version": "financial-eligibility-metadata-binding-v3",
        "financial_dispatch_audit_projection_version": "5.0",
        "financial_dispatch_ledger_version": "1.7",
        "financial_eligibility_acquisition_result_version": "financial-eligibility-acquisition-result-v3",
        "financial_eligibility_context_version": "financial-eligibility-acquisition-context-v3",
        "financial_eligibility_request_binding_version": "financial-eligibility-request-binding-v3",
        "financial_tool_manifest_version": "financial-tool-manifest-v5",
        "financial_tool_message_envelope_version": "financial-tool-message-envelope-v5",
        "indicator_manifest_version": "financial-manifest-v5",
        "indicator_routing_request_version": "financial-indicator-routing-request-v8",
        "indicator_routing_result_version": "financial-indicator-routing-result-v5",
        "indicator_selection_checkpoint_version": "financial-indicator-selection-checkpoint-v4",
        "indicator_selection_dispatch_version": "financial-indicator-selection-dispatch-v4",
        "listing_metadata_projection_version": "financial-listing-metadata-projection-v3",
        "listing_metadata_version": "financial-listing-metadata-v3",
        "manifest_audit_version": "financial-manifest-audit-v5",
        "manifest_request_audit_version": "financial-manifest-request-audit-v5",
        "provider_subrequest_cache_checkpoint_version": "1.3",
        "statement_manifest_version": "financial-manifest-v5",
        "statement_routing_request_version": "financial-statement-routing-request-v5",
        "statement_routing_result_version": "financial-statement-routing-result-v3",
        "statement_selection_checkpoint_version": "financial-statement-selection-checkpoint-v3",
        "statement_selection_dispatch_version": "financial-statement-selection-dispatch-v3",
        "temporal_decision_version": "financial-metadata-temporal-eligibility-decision-v2",
        "temporal_evidence_acquisition_binding_version": (
            "financial-temporal-evidence-acquisition-binding-v1"
        ),
        "temporal_policy_version": "provider-neutral-temporal-eligibility-v2",
        "temporal_transaction_binding_version": (
            "financial-eligibility-temporal-transaction-binding-v1"
        ),
    }


def test_historical_plan_1_2_remains_readable_under_recorded_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _qualified_plan(monkeypatch)
    forward = prepare_mainland_temporal_capability_routing_plan(predecessor)

    historical = MainlandCapabilityRoutingPlan.model_validate(
        predecessor.model_dump(mode="json")
    )
    projection = MainlandCapabilityRoutingRunProjection.from_plan(historical)

    assert historical == predecessor
    assert historical.plan_version == "1.2"
    assert historical.temporal_contract_bindings is None
    assert projection.temporal_contract_bindings is None
    assert forward.plan_signature != historical.plan_signature

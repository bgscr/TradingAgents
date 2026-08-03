from __future__ import annotations

import copy
import importlib.util
import json
import sys
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPreflightError,
    TushareCapability,
    preflight_mainland_capability_routing,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.qualified_routing_composition import (
    PRODUCTION_QUALIFIED_ROUTING_BLUEPRINT_VERSION,
    PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    ProductionQualifiedRoutingCompositionBlueprint,
    ProductionQualifiedRoutingFactoryContract,
    build_production_qualified_routing_composition_blueprint,
)

_PROVIDER_SOURCE_COVERAGE = {
    "akshare": True,
    "akshare_sina": True,
    "baostock": True,
    "baostock_qualified_families": True,
    "tushare": True,
    "yfinance": True,
    "yfinance_derived": True,
}
_DEPENDENCY_AVAILABILITY = {
    "akshare": True,
    "baostock": True,
    "tushare": True,
    "yfinance": True,
}


def _valid_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": [
                "name_events",
                "statements",
                "adjustment_factors",
                "financial_indicators",
            ],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
            "data_cache_dir": str(tmp_path / "cache"),
            "market_history_database_path": str(
                tmp_path / "market-history" / "market_history.sqlite3"
            ),
            "market_history_payload_root": str(
                tmp_path / "market-history" / "payloads"
            ),
            "market_history_backup_root": str(
                tmp_path / "market-history" / "backups"
            ),
            "results_dir": str(tmp_path / "results"),
        }
    )
    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=config,
    )
    preflight = preflight_mainland_capability_routing(
        asset_configuration,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-token"},
    )
    assert preflight.plan is not None
    plan = preflight.plan
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["asset_configuration_signature"] = (
        asset_configuration.asset_configuration_signature
    )
    return {
        "plan": plan,
        "asset_configuration": asset_configuration,
        "financial_eligibility_binding": plan.financial_eligibility_binding,
        "config": config,
        "validation_workload": True,
        "disposition": (
            MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
        ),
        "environment": {"TUSHARE_TOKEN": "fixture-token"},
        "dependency_availability": dict(_DEPENDENCY_AVAILABILITY),
        "provider_source_coverage": dict(_PROVIDER_SOURCE_COVERAGE),
        "factory_contract_version": (
            PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION
        ),
    }


def test_explicit_qualified_shadow_validation_returns_a_closed_blueprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _valid_inputs(tmp_path, monkeypatch)

    blueprint = build_production_qualified_routing_composition_blueprint(**inputs)

    assert isinstance(blueprint, ProductionQualifiedRoutingCompositionBlueprint)
    assert (
        blueprint.factory_contract_version
        == PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION
    )
    assert blueprint.blueprint_version == PRODUCTION_QUALIFIED_ROUTING_BLUEPRINT_VERSION
    assert blueprint.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
    assert blueprint.validation_intent == "qualified_validation"
    assert (
        blueprint.disposition
        is MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
    )
    assert blueprint.enabled_capabilities == tuple(TushareCapability)
    assert blueprint.plan_signature == inputs["plan"].plan_signature
    assert blueprint.asset_configuration_signature == (
        inputs["asset_configuration"].asset_configuration_signature
    )
    assert blueprint.blueprint_identity.startswith(
        "production-qualified-routing-blueprint:v1:"
    )
    assert blueprint.model_config["frozen"] is True
    assert not any(tmp_path.rglob("*"))


def test_blueprint_identity_is_canonical_and_captured_inputs_are_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_inputs = _valid_inputs(tmp_path, monkeypatch)
    second_inputs = copy.deepcopy(first_inputs)
    second_inputs["config"] = dict(
        reversed(tuple(second_inputs["config"].items()))
    )
    second_inputs["dependency_availability"] = dict(
        reversed(tuple(_DEPENDENCY_AVAILABILITY.items()))
    )
    second_inputs["provider_source_coverage"] = dict(
        reversed(tuple(_PROVIDER_SOURCE_COVERAGE.items()))
    )
    second_inputs["environment"] = {"TUSHARE_TOKEN": "different-token"}
    first_inputs["config"].update(
        process_id=101,
        runtime_call_order="first",
        model_prose="bullish prose must not bind construction",
    )
    second_inputs["config"].update(
        process_id=202,
        runtime_call_order="second",
        model_prose="bearish prose must not bind construction",
    )

    first = build_production_qualified_routing_composition_blueprint(**first_inputs)
    second = build_production_qualified_routing_composition_blueprint(**second_inputs)
    original_identity = first.blueprint_identity
    original_projection = first.model_dump_json()

    first_inputs["config"]["tushare_account_scope_label"] = "mutated"
    first_inputs["environment"]["TUSHARE_TOKEN"] = "mutated-token"
    first_inputs["dependency_availability"]["tushare"] = False
    first_inputs["provider_source_coverage"]["tushare"] = False

    assert first == second
    assert first.blueprint_identity == original_identity
    assert first.model_dump_json() == original_projection
    with pytest.raises(ValidationError):
        first.blueprint_identity = "production-qualified-routing-blueprint:v1:" + (
            "0" * 64
        )


def test_factory_snapshots_each_mutable_boundary_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _valid_inputs(tmp_path, monkeypatch)

    class CountingMapping(dict):
        def __init__(self, values):
            super().__init__(values)
            self.reads: dict[str, int] = {}

        def get(self, key, default=None):
            self.reads[key] = self.reads.get(key, 0) + 1
            return super().get(key, default)

    boundaries = {
        key: CountingMapping(inputs[key])
        for key in (
            "config",
            "environment",
            "dependency_availability",
            "provider_source_coverage",
        )
    }
    inputs.update(boundaries)

    blueprint = build_production_qualified_routing_composition_blueprint(**inputs)

    assert blueprint.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
    assert all(
        read_count == 1
        for boundary in boundaries.values()
        for read_count in boundary.reads.values()
    )


def test_blueprint_identity_changes_for_material_safe_plan_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_inputs = _valid_inputs(tmp_path / "first", monkeypatch)
    second_inputs = _valid_inputs(tmp_path / "second", monkeypatch)
    second_config = second_inputs["config"]
    second_config["tushare_calls_per_minute"] = 30
    second_config["tushare_operator_safety_ceiling_calls_per_minute"] = 30
    second_preflight = preflight_mainland_capability_routing(
        second_inputs["asset_configuration"],
        config=second_config,
        environment=second_inputs["environment"],
    )
    assert second_preflight.plan is not None
    second_plan = second_preflight.plan
    second_config["mainland_capability_routing_plan_signature"] = (
        second_plan.plan_signature
    )
    second_inputs["plan"] = second_plan
    second_inputs["financial_eligibility_binding"] = (
        second_plan.financial_eligibility_binding
    )

    first = build_production_qualified_routing_composition_blueprint(**first_inputs)
    second = build_production_qualified_routing_composition_blueprint(**second_inputs)

    assert first.plan_signature != second.plan_signature
    assert first.blueprint_identity != second.blueprint_identity


def test_blueprint_identity_binds_roots_without_leaking_local_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_inputs = _valid_inputs(tmp_path / "first", monkeypatch)
    second_inputs = _valid_inputs(tmp_path / "second", monkeypatch)

    first = build_production_qualified_routing_composition_blueprint(**first_inputs)
    second = build_production_qualified_routing_composition_blueprint(**second_inputs)

    assert first.plan_signature == second.plan_signature
    assert first.blueprint_identity != second.blueprint_identity
    assert str(tmp_path) not in repr(first)
    assert str(tmp_path) not in first.model_dump_json()


def test_blueprint_public_values_are_secret_payload_url_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _valid_inputs(tmp_path, monkeypatch)
    token = "secret-canary-value"
    unsafe_url = "https://user:password@example.test/data?token=secret-canary"
    raw_error = "provider exploded with secret-canary"
    raw_payload = {"raw_text": "secret-canary-payload"}
    inputs["environment"] = {"TUSHARE_TOKEN": token}
    inputs["config"].update(
        backend_url=unsafe_url,
        raw_provider_error=raw_error,
        raw_payload=raw_payload,
    )

    blueprint = build_production_qualified_routing_composition_blueprint(**inputs)
    public_text = "\n".join(
        (
            blueprint.blueprint_identity,
            repr(blueprint),
            blueprint.model_dump_json(),
            json.dumps(blueprint.model_dump(mode="json"), sort_keys=True),
        )
    )

    for forbidden in (
        token,
        "secret-canary",
        "token_digest",
        unsafe_url,
        raw_error,
        "raw_text",
        str(tmp_path),
        "?",
    ):
        assert forbidden not in public_text


def test_closed_contract_rejects_tampered_or_extra_factory_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _valid_inputs(tmp_path, monkeypatch)
    blueprint = build_production_qualified_routing_composition_blueprint(**inputs)
    contract_payload = blueprint.factory_contract.model_dump(mode="python")
    contract_payload["unexpected_provider_payload"] = "must-not-cross-boundary"

    with pytest.raises(ValidationError):
        ProductionQualifiedRoutingFactoryContract.model_validate(contract_payload)

    blueprint_payload = blueprint.model_dump(mode="python")
    blueprint_payload.update(
        blueprint_identity="production-qualified-routing-blueprint:v1:" + ("0" * 64),
        runtime_root_path=blueprint.runtime_root_path,
        database_path=blueprint.database_path,
        provider_artifact_root_path=blueprint.provider_artifact_root_path,
        backup_root_path=blueprint.backup_root_path,
        report_root_path=blueprint.report_root_path,
    )
    with pytest.raises(ValidationError, match="identity"):
        ProductionQualifiedRoutingCompositionBlueprint.model_validate(
            blueprint_payload
        )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("factory_contract", "qualified_routing_factory_contract_invalid"),
        ("validation_intent", "qualified_validation_workload_required"),
        ("disposition", "qualified_routing_disposition_invalid"),
        ("qualified_v1", "qualified_v1_authority_disabled"),
        ("legacy", "invalid_routing_mode"),
        ("empty_plan", "qualified_routing_plan_not_applicable"),
        ("plan_signature", "qualified_routing_plan_invalid"),
        ("configured_plan_signature", "qualified_routing_plan_invalid"),
        ("asset_binding", "qualified_routing_asset_binding_invalid"),
        ("unsafe_asset_identity", "qualified_routing_asset_binding_invalid"),
        ("unsafe_asset_token_digest", "qualified_routing_asset_binding_invalid"),
        ("unsafe_asset_token_fragment", "qualified_routing_asset_binding_invalid"),
        ("eligibility_binding", "qualified_routing_eligibility_binding_invalid"),
        ("qualification_profile", "qualification_profile_mismatch"),
        ("enabled_capabilities", "qualified_routing_plan_invalid"),
        ("data_usage_mode", "data_usage_mode_not_personal_research"),
        ("token", "tushare_token_missing"),
        ("dependency", "qualified_routing_dependency_missing"),
        ("source_coverage", "qualified_routing_source_coverage_incomplete"),
        ("account_scope", "tushare_account_scope_invalid"),
        ("account_scope_token", "tushare_account_scope_invalid"),
        ("account_scope_token_digest", "tushare_account_scope_invalid"),
        ("account_scope_token_fragment", "tushare_account_scope_invalid"),
        ("pacing", "tushare_pacing_invalid"),
        ("safety_ceiling", "tushare_operator_safety_ceiling_invalid"),
        ("runtime_root", "qualified_routing_runtime_root_invalid"),
        ("mapping_error", "qualified_routing_factory_contract_invalid"),
        ("unsafe_output", "qualified_routing_factory_output_invalid"),
    ),
)
def test_invalid_factory_inputs_fail_typed_before_every_resource_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_reason: str,
) -> None:
    inputs = _valid_inputs(tmp_path, monkeypatch)
    config = inputs["config"]
    plan = inputs["plan"]
    if case == "factory_contract":
        inputs["factory_contract_version"] = "future-factory-contract"
    elif case == "validation_intent":
        inputs["validation_workload"] = False
    elif case == "disposition":
        inputs["disposition"] = (
            MainlandCapabilityRoutingDisposition.QUALIFIED_AUTHORITATIVE
        )
    elif case == "qualified_v1":
        inputs["plan"] = plan.model_copy(
            update={"mode": MainlandCapabilityRoutingMode.QUALIFIED_V1}
        )
    elif case == "legacy":
        inputs["plan"] = plan.model_copy(
            update={"mode": MainlandCapabilityRoutingMode.LEGACY}
        )
    elif case == "empty_plan":
        inputs["plan"] = plan.model_copy(
            update={"enabled_tushare_capabilities": ()}
        )
    elif case == "plan_signature":
        inputs["plan"] = plan.model_copy(
            update={"plan_signature": "mainland-routing-plan:v1:" + ("0" * 64)}
        )
    elif case == "configured_plan_signature":
        config["mainland_capability_routing_plan_signature"] = (
            "mainland-routing-plan:v1:" + ("0" * 64)
        )
    elif case == "asset_binding":
        config["asset_configuration_signature"] = "asset-config:v1:" + ("0" * 64)
    elif case == "unsafe_asset_identity":
        asset_configuration = inputs["asset_configuration"]
        unsafe_identity = asset_configuration.instrument_identity.model_copy(
            update={"symbol": "601328.SS?token=fixture-token"}
        )
        unsafe_asset = asset_configuration.model_copy(
            update={"instrument_identity": unsafe_identity}
        )
        inputs["asset_configuration"] = unsafe_asset
        config["asset_configuration_signature"] = (
            unsafe_asset.asset_configuration_signature
        )
    elif case in {"unsafe_asset_token_digest", "unsafe_asset_token_fragment"}:
        asset_configuration = inputs["asset_configuration"]
        unsafe_material = (
            sha256(b"fixture-token").hexdigest()
            if case == "unsafe_asset_token_digest"
            else "fixture-"
        )
        unsafe_asset = asset_configuration.model_copy(
            update={"registry_id": f"asset-registry:{unsafe_material}"}
        )
        inputs["asset_configuration"] = unsafe_asset
        config["asset_configuration_signature"] = (
            unsafe_asset.asset_configuration_signature
        )
    elif case == "eligibility_binding":
        inputs["financial_eligibility_binding"] = None
    elif case == "qualification_profile":
        config["tushare_qualification_profile"] = "wrong-profile"
    elif case == "enabled_capabilities":
        config["tushare_enabled_capabilities"] = ["statements"]
    elif case == "data_usage_mode":
        config["data_usage_mode"] = "production"
    elif case == "token":
        inputs["environment"] = {}
    elif case == "dependency":
        inputs["dependency_availability"]["baostock"] = False
    elif case == "source_coverage":
        inputs["provider_source_coverage"]["baostock"] = False
    elif case == "account_scope":
        config["tushare_account_scope_label"] = "credential-primary"
    elif case == "account_scope_token":
        inputs["environment"] = {
            "TUSHARE_TOKEN": config["tushare_account_scope_label"]
        }
    elif case in {"account_scope_token_digest", "account_scope_token_fragment"}:
        unsafe_label = (
            sha256(b"fixture-token").hexdigest()
            if case == "account_scope_token_digest"
            else "fixture-"
        )
        config["tushare_account_scope_label"] = unsafe_label
        unsafe_preflight = preflight_mainland_capability_routing(
            inputs["asset_configuration"],
            config=config,
            environment=inputs["environment"],
        )
        assert unsafe_preflight.plan is not None
        unsafe_plan = unsafe_preflight.plan
        inputs["plan"] = unsafe_plan
        inputs["financial_eligibility_binding"] = (
            unsafe_plan.financial_eligibility_binding
        )
        config["mainland_capability_routing_plan_signature"] = (
            unsafe_plan.plan_signature
        )
    elif case == "pacing":
        config["tushare_calls_per_minute"] = 39
    elif case == "safety_ceiling":
        config["tushare_operator_safety_ceiling_calls_per_minute"] = 39
    elif case == "runtime_root":
        config["data_cache_dir"] = "relative/cache"
    elif case == "mapping_error":
        class ExplodingMapping(dict):
            def get(self, _key, _default=None):
                raise ValueError(
                    "https://user:password@example.test/data?token=fixture-token"
                )

        config = ExplodingMapping(config)
        inputs["config"] = config
    elif case == "unsafe_output":
        import tradingagents.qualified_routing_composition as composition_module

        def unsafe_blueprint(**_kwargs):
            raise ValueError(
                "raw provider exception at "
                "https://user:password@example.test/data?token=fixture-token"
            )

        monkeypatch.setattr(
            composition_module,
            "ProductionQualifiedRoutingCompositionBlueprint",
            unsafe_blueprint,
        )
    else:  # pragma: no cover - the parametrization is closed above
        raise AssertionError(case)

    token = "fixture-token"
    unsafe_url = "https://user:password@example.test/data?token=fixture-token"
    raw_error = "provider exception fixture-token"
    config.update(backend_url=unsafe_url, raw_provider_error=raw_error)

    activity = {
        "provider": 0,
        "model": 0,
        "graph": 0,
        "store": 0,
        "coordinator": 0,
        "cache": 0,
        "artifact": 0,
        "checkpoint": 0,
        "report": 0,
        "composition": 0,
        "attempt": 0,
    }

    def forbidden(kind: str):
        def call(*_args, **_kwargs):
            activity[kind] += 1
            raise AssertionError(f"{kind} work must not run")

        return call

    import sqlite3

    import cli.main as cli_main
    import tradingagents.graph.checkpointer as checkpointer
    import tradingagents.graph.trading_graph as trading_graph
    from cli.runtime_artifacts import RuntimeArtifactWriter
    from tradingagents.dataflows.akshare_sina_statements import (
        AkshareSinaStatementAdapter,
    )
    from tradingagents.dataflows.baostock_capabilities import (
        BaoStockCapabilityAdapter,
    )
    from tradingagents.dataflows.baostock_ratios import BaoStockRatioFamilyAdapter
    from tradingagents.dataflows.provider_subrequests import (
        ProviderSubrequestArtifactStore,
        ProviderSubrequestCache,
    )
    from tradingagents.dataflows.tushare_factors_names import (
        TushareAdjustmentFactorAdapter,
        TushareNameEventAdapter,
    )
    from tradingagents.dataflows.tushare_indicators import (
        TushareFinancialIndicatorAdapter,
    )
    from tradingagents.dataflows.tushare_statements import TushareStatementAdapter
    from tradingagents.graph.financial_tools import (
        QualifiedFinancialRoutingComposition,
    )
    from tradingagents.market_history.coordinator import (
        ProviderPhysicalAttemptEvent,
        ProviderRequestCoordinator,
    )
    from tradingagents.market_history.store import MarketHistoryStore

    monkeypatch.setattr(
        ProviderRequestCoordinator,
        "execute_retry_sequence",
        forbidden("provider"),
    )
    monkeypatch.setattr(trading_graph, "create_llm_client", forbidden("model"))
    monkeypatch.setattr(
        trading_graph.TradingAgentsGraph,
        "__init__",
        forbidden("graph"),
    )
    monkeypatch.setattr(MarketHistoryStore, "open", forbidden("store"))
    monkeypatch.setattr(sqlite3, "connect", forbidden("store"))
    monkeypatch.setattr(ProviderRequestCoordinator, "__init__", forbidden("coordinator"))
    monkeypatch.setattr(ProviderSubrequestCache, "__init__", forbidden("cache"))
    monkeypatch.setattr(
        ProviderSubrequestArtifactStore,
        "__init__",
        forbidden("artifact"),
    )
    monkeypatch.setattr(
        QualifiedFinancialRoutingComposition,
        "__init__",
        forbidden("composition"),
    )
    monkeypatch.setattr(
        ProviderPhysicalAttemptEvent,
        "__init__",
        forbidden("attempt"),
    )
    monkeypatch.setattr(RuntimeArtifactWriter, "__init__", forbidden("artifact"))
    for adapter_type in (
        AkshareSinaStatementAdapter,
        BaoStockCapabilityAdapter,
        BaoStockRatioFamilyAdapter,
        TushareAdjustmentFactorAdapter,
        TushareNameEventAdapter,
        TushareFinancialIndicatorAdapter,
        TushareStatementAdapter,
    ):
        monkeypatch.setattr(adapter_type, "__init__", forbidden("provider"))
    direct_provider_seams = {
        "tushare": ("pro_api",),
        "baostock": (
            "login",
            "logout",
            "query_history_k_data_plus",
            "query_adjust_factor",
            "query_stock_basic",
            "query_profit_data",
            "query_operation_data",
            "query_growth_data",
            "query_balance_data",
            "query_cash_flow_data",
            "query_dupont_data",
        ),
        "akshare": (
            "stock_financial_report_sina",
            "stock_financial_analysis_indicator",
            "stock_zh_a_hist",
        ),
        "yfinance": ("Ticker", "download"),
        "requests": ("Session",),
    }
    for module_name, entry_points in direct_provider_seams.items():
        existing_provider_module = sys.modules.get(module_name)
        if existing_provider_module is not None:
            for entry_point in entry_points:
                monkeypatch.setattr(
                    existing_provider_module,
                    entry_point,
                    forbidden("provider"),
                    raising=False,
                )
        provider_module = ModuleType(module_name)
        for entry_point in entry_points:
            setattr(provider_module, entry_point, forbidden("provider"))
        monkeypatch.setitem(sys.modules, module_name, provider_module)
    monkeypatch.setattr(Path, "mkdir", forbidden("artifact"))
    monkeypatch.setattr(Path, "open", forbidden("artifact"))
    monkeypatch.setattr(checkpointer, "get_checkpointer", forbidden("checkpoint"))
    monkeypatch.setattr(cli_main, "_write_run_reports", forbidden("report"))

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        build_production_qualified_routing_composition_blueprint(**inputs)

    assert raised.value.failure.reason.value == expected_reason
    assert raised.value.failure.reason is raised.value.failure.diagnostic_code
    assert activity == dict.fromkeys(activity, 0)
    assert not any(tmp_path.rglob("*"))
    rendered = "\n".join(
        (
            str(raised.value),
            repr(raised.value),
            raised.value.failure.model_dump_json(),
        )
    )
    for forbidden_text in (token, unsafe_url, raw_error, str(tmp_path), "?"):
        assert forbidden_text not in rendered

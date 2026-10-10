"""Offline production selection and sealing of the qualified CNINFO contracts."""

from __future__ import annotations

import copy
import importlib.util
import json
import socket
from hashlib import sha256

import pytest
import requests

import cli.main as cli_main
import tradingagents.qualified_routing_composition as composition
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingPreflightError,
    MainlandCapabilityRoutingRunProjection,
    capability_routing_configuration_is_explicit,
    preflight_mainland_capability_routing,
    prepare_mainland_listing_publication_candidate_plan,
    prepare_mainland_temporal_capability_routing_plan,
)
from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides
from tradingagents.evidence import EvidenceCapability, InstrumentKind, capability_profile_for
from tradingagents.terminal_contract import configuration_digest

_PROFILE = "cninfo-listing-publication-candidate-v1"
_POLICY = "cninfo-source-policy-v1"
# Preserve the explicit one-second OFFLINE configuration in the qualified
# e385883 acceptance fixture. It must never become a production default.
_FIXTURE_INTERVAL = 1
_SIGNATURES = {
    "1.4": "mainland-routing-plan:v1:b8444100b0d8810fb714cb35f633711e1ddc6dc04925098ec195de57a0892e86",
    "1.5": "mainland-routing-plan:v1:dd69d38fd71be9d852e2e312d04447d0bbbc7594952de8c72b532c7581776320",
}
_CNINFO_KEYS = (
    "cninfo_listing_publication_profile",
    "cninfo_source_policy_version",
    "cninfo_operator_minimum_interval_seconds",
)


@pytest.fixture(autouse=True)
def offline_boundaries(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("external_io")
        raise AssertionError("production binding must not perform external I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "0")
    # Optional dependency discovery is an environment boundary, not a provider.
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    yield
    assert attempts == []


def _config(tmp_path, version="1.5"):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        mainland_capability_routing_mode="qualified_v1_shadow",
        tushare_enabled_capabilities=(
            "statements",
            "financial_indicators",
            "adjustment_factors",
            "name_events",
        ),
        tushare_qualification_profile="cn-a-2000-20260729-v1",
        tushare_account_scope_label="personal-research-primary",
        tushare_calls_per_minute=40,
        tushare_operator_safety_ceiling_calls_per_minute=40,
        data_usage_mode="personal_research",
        data_cache_dir=str(tmp_path / "cache"),
        market_history_database_path=str(tmp_path / "history.sqlite3"),
        market_history_payload_root=str(tmp_path / "payloads"),
        market_history_backup_root=str(tmp_path / "backups"),
        results_dir=str(tmp_path / "results"),
    )
    if version is not None:
        config["mainland_capability_routing_plan_version"] = version
    if version == "1.5":
        config.update(
            cninfo_listing_publication_profile=_PROFILE,
            cninfo_source_policy_version=_POLICY,
            cninfo_operator_minimum_interval_seconds=_FIXTURE_INTERVAL,
        )
    return config


def _preflight(config, symbol="601328.SS"):
    asset = resolve_run_asset_configuration(symbol, config=config)
    return asset, preflight_mainland_capability_routing(asset, config=config)


def _blueprint(config, symbol="601328.SS"):
    asset, result = _preflight(config, symbol)
    assert result.passed, result.failure
    assert result.plan is not None
    config = dict(
        config,
        asset_configuration_signature=asset.asset_configuration_signature,
        mainland_capability_routing_plan_signature=result.plan.plan_signature,
    )
    managed = cli_main.build_qualified_financial_routing_composition(
        plan=result.plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )
    assert isinstance(managed, composition.RegisteredProductionQualifiedRoutingComposition)
    return managed.blueprint


@pytest.mark.parametrize("version", ("1.4", "1.5"))
def test_explicit_production_plan_preserves_exact_qualified_signature(tmp_path, version):
    _asset, result = _preflight(_config(tmp_path, version))
    assert result.passed
    assert result.plan.plan_version == version
    assert result.plan.plan_signature == _SIGNATURES[version]


@pytest.mark.parametrize("version", ("1.2", "1.3", "1.6", "latest", "", True, 1.5))
def test_unsupported_production_plan_selection_fails_closed(tmp_path, version):
    _asset, result = _preflight(_config(tmp_path, version))
    assert not result.passed
    assert result.plan is None


def test_absent_selection_keeps_historical_preflight_and_plan14_cli_semantics(tmp_path):
    config = _config(tmp_path, None)
    _asset, result = _preflight(config)
    assert result.plan.plan_version == "1.2"
    plan = prepare_mainland_temporal_capability_routing_plan(result.plan)
    assert plan.plan_signature == _SIGNATURES["1.4"]
    assert plan.listing_publication_candidate_profile is None
    assert all(DEFAULT_CONFIG.get(key) is None for key in _CNINFO_KEYS)
    assert DEFAULT_CONFIG.get("mainland_capability_routing_plan_version") is None


@pytest.mark.parametrize("version", (None, "1.4"))
def test_cninfo_fields_cannot_implicitly_upgrade_an_old_selection(tmp_path, version):
    config = _config(tmp_path)
    if version is None:
        config.pop("mainland_capability_routing_plan_version")
    else:
        config["mainland_capability_routing_plan_version"] = version
    _asset, result = _preflight(config)
    assert not result.passed


@pytest.mark.parametrize("missing", _CNINFO_KEYS)
def test_plan15_requires_each_explicit_policy_configuration_field(tmp_path, missing):
    config = _config(tmp_path)
    config.pop(missing)
    _asset, result = _preflight(config)
    assert not result.passed


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("cninfo_listing_publication_profile", "cn-a-2000-20260729-v1"),
        ("cninfo_listing_publication_profile", "equity.v1"),
        ("cninfo_source_policy_version", "cninfo-source-policy-v2"),
        ("cninfo_operator_minimum_interval_seconds", 0),
        ("cninfo_operator_minimum_interval_seconds", 61),
        ("cninfo_operator_minimum_interval_seconds", True),
        ("cninfo_operator_minimum_interval_seconds", float("nan")),
        ("cninfo_operator_minimum_interval_seconds", "unlimited"),
        ("data_usage_mode", "production"),
        ("data_usage_mode", "commercial"),
        ("data_usage_mode", "other"),
        ("mainland_capability_routing_plan_signature", _SIGNATURES["1.4"]),
    ),
)
def test_invalid_cninfo_profile_policy_or_usage_is_rejected_before_construction(
    tmp_path, key, value
):
    config = _config(tmp_path)
    config[key] = value
    _asset, result = _preflight(config)
    assert not result.passed


def test_qualified_v1_authority_attempt_remains_rejected(tmp_path):
    config = _config(tmp_path)
    config["mainland_capability_routing_mode"] = "qualified_v1"
    _asset, result = _preflight(config)
    assert not result.passed
    assert result.failure.reason.value == "qualified_v1_authority_disabled"


def test_environment_allowlist_reproduces_explicit_plan_and_policy(tmp_path, monkeypatch):
    config = _config(tmp_path, None)
    for name, value in (
        ("TRADINGAGENTS_MAINLAND_CAPABILITY_ROUTING_PLAN_VERSION", "1.5"),
        ("TRADINGAGENTS_CNINFO_LISTING_PUBLICATION_PROFILE", _PROFILE),
        ("TRADINGAGENTS_CNINFO_SOURCE_POLICY_VERSION", _POLICY),
        ("TRADINGAGENTS_CNINFO_OPERATOR_MINIMUM_INTERVAL_SECONDS", str(_FIXTURE_INTERVAL)),
    ):
        monkeypatch.setenv(name, value)
    applied = _apply_env_overrides(config)
    _asset, result = _preflight(applied)
    assert result.passed
    assert result.plan.plan_signature == _SIGNATURES["1.5"]
    assert (
        _blueprint(
            applied
        ).factory_contract.cninfo_binding.profile.source_policy.operator_minimum_interval_seconds
        == _FIXTURE_INTERVAL
    )


def test_production_factory_seals_the_exact_cninfo_profile_and_policy(tmp_path):
    config = _config(tmp_path)
    blueprint = _blueprint(config)
    binding = blueprint.factory_contract.cninfo_binding
    assert binding is not None
    assert binding.profile.profile_id == _PROFILE
    assert binding.profile.capability == "listing_publication_evidence"
    assert binding.profile.provider_id == "cninfo"
    assert binding.profile.source_policy.contract_version == _POLICY
    assert binding.profile.source_policy.data_usage_mode.value == "personal_research"
    assert binding.profile.source_policy.operator_minimum_interval_seconds == _FIXTURE_INTERVAL
    assert binding.profile.source_policy.max_physical_attempts_per_operation == 1
    assert binding.profile.source_policy.max_operations_per_acquisition == 3
    assert binding.profile.source_policy.allow_pdf_text_extraction is False
    assert binding.profile.source_policy.allow_crawling is False
    assert binding.profile.source_policy.allow_prewarming is False
    assert tuple(item.provider_scope for item in binding.profile.upstream_pacing) == (
        "cninfo_metadata",
        "cninfo_documents",
    )
    assert tuple(item.upstream_service_id for item in binding.profile.upstream_pacing) == (
        "upstream-service=sha256:c40b4b6eeac076e4b1c8d1b821184683db9679d339013ff10c0e0855f095fbac",
        "upstream-service=sha256:7141ee1725e1ff17e1c6b4ee4c918c2bb9f7cfe67f6b615be637c3023742cc3e",
    )
    assert all(
        item.minimum_interval_seconds == _FIXTURE_INTERVAL
        for item in binding.profile.upstream_pacing
    )
    assert binding.plan_signature == _SIGNATURES["1.5"]
    assert "cninfo" in blueprint.factory_contract.required_provider_source_ids
    assert binding.profile.profile_identity in blueprint.model_dump_json()
    assert not any(tmp_path.iterdir())


def test_old_profile_and_plan14_blueprint_do_not_silently_admit_cninfo(tmp_path):
    blueprint = _blueprint(_config(tmp_path, "1.4"))
    assert "cninfo_binding" not in blueprint.factory_contract.model_dump(mode="json")
    assert "cninfo" not in blueprint.factory_contract.required_provider_source_ids
    assert (
        EvidenceCapability.LISTING_PUBLICATION_EVIDENCE
        not in capability_profile_for(InstrumentKind.EQUITY).all_capabilities
    )
    assert blueprint.routing_plan.plan_signature == _SIGNATURES["1.4"]


def test_dormant_plan_cannot_bypass_explicit_production_selection(tmp_path):
    config = _config(tmp_path, None)
    asset, result = _preflight(config)
    dormant = prepare_mainland_listing_publication_candidate_plan(
        prepare_mainland_temporal_capability_routing_plan(result.plan)
    )
    config.update(
        asset_configuration_signature=asset.asset_configuration_signature,
        mainland_capability_routing_plan_signature=dormant.plan_signature,
    )
    with pytest.raises(MainlandCapabilityRoutingPreflightError):
        cli_main.build_qualified_financial_routing_composition(
            plan=dormant, asset_configuration=asset, config=config, validation_workload=True
        )


def test_missing_cninfo_source_coverage_blocks_before_runtime_exposure(tmp_path, monkeypatch):
    coverage = composition.production_qualified_routing_provider_source_coverage
    monkeypatch.setattr(
        cli_main,
        "production_qualified_routing_provider_source_coverage",
        lambda plan: dict(coverage(plan), cninfo=False),
    )
    with pytest.raises(MainlandCapabilityRoutingPreflightError):
        _blueprint(_config(tmp_path))


def _registry_config(tmp_path, symbol, venue):
    config = _config(tmp_path)
    payload = {
        "schema_version": "1.0",
        "registry_id": "offline-board-registry-v1",
        "rows": [
            {
                "canonical_symbol": symbol,
                "aliases": [],
                "venue": venue,
                "instrument_kind": "equity",
                "currency": "CNY",
                "display_name": "Offline board fixture",
                "provenance": {
                    "provider": "Offline exchange fixture",
                    "source_ref": "fixture:exchange-identity",
                    "retrieved_at": "2026-09-01T00:00:00Z",
                },
            }
        ],
    }
    encoded = json.dumps(payload).encode()
    path = tmp_path / "registry.json"
    path.write_bytes(encoded)
    config.update(
        instrument_identity_registry_path=str(path),
        instrument_identity_registry_sha256=sha256(encoded).hexdigest(),
    )
    return config


@pytest.mark.parametrize(
    ("symbol", "venue", "board"),
    (
        ("601328.SS", "XSHG", "xshg_main"),
        ("300750.SZ", "XSHE", "xshe_chinext"),
        ("688981.SS", "XSHG", "xshg_star"),
    ),
)
def test_production_board_comes_from_the_pinned_instrument_identity(tmp_path, symbol, venue, board):
    blueprint = _blueprint(_registry_config(tmp_path, symbol, venue), symbol)
    binding = blueprint.factory_contract.cninfo_binding
    assert binding.authoritative_listing_board.value == board
    assert binding.instrument_identity.symbol == symbol
    assert binding.instrument_identity.venue == venue
    assert binding.asset_configuration_signature == blueprint.asset_configuration_signature


@pytest.mark.parametrize(
    ("symbol", "venue"),
    (("601328.SZ", "XSHE"), ("300750.SS", "XSHG"), ("000001.SZ", "XSHE")),
)
def test_unsupported_board_or_identity_venue_mismatch_fails_closed(tmp_path, symbol, venue):
    config = _registry_config(tmp_path, symbol, venue)
    _asset, result = _preflight(config, symbol)
    assert not result.passed


def test_plan15_authority_projection_keeps_legacy_authoritative(tmp_path):
    _asset, result = _preflight(_config(tmp_path))
    assert result.plan.plan_version == "1.5"
    projection = MainlandCapabilityRoutingRunProjection.from_plan(result.plan)
    assert projection.disposition.value == "shadow_non_authoritative"
    assert projection.mode.value == "qualified_v1_shadow"


@pytest.mark.parametrize("field,value", (("provider_id", "baostock"), ("profile_id", "equity.v1")))
def test_serialized_cninfo_profile_rejects_unsupported_provider_and_old_profile(
    tmp_path, field, value
):
    blueprint = _blueprint(_config(tmp_path))
    contract = blueprint.factory_contract
    payload = contract.model_dump(mode="json")
    payload["cninfo_binding"]["profile"][field] = value
    with pytest.raises(ValueError):
        type(contract).model_validate(payload)


def test_plan15_production_binding_rejects_unmanaged_injection(tmp_path):
    from tradingagents.dataflows.financial_capability_routing import (
        MainlandFinancialCapabilityRouter,
    )
    from tradingagents.graph.financial_tools import QualifiedFinancialRoutingComposition

    config = _config(tmp_path)
    asset, result = _preflight(config)
    unbound = QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(routing_plan=result.plan, statement_sources={}),
        statement_request_factory=lambda _request: None,
        indicator_request_factory=lambda _request: None,
    )
    config.update(
        asset_configuration_signature=asset.asset_configuration_signature,
        mainland_capability_routing_plan_signature=result.plan.plan_signature,
    )
    with pytest.raises(MainlandCapabilityRoutingPreflightError):
        cli_main.build_qualified_financial_routing_composition(
            plan=result.plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
            qualified_financial_routing=unbound,
        )


def _source_binding():
    # Synthetic identities for an in-memory preparation contract test only.
    return {
        "commit_id": "1" * 40,
        "tree_hash": "2" * 40,
        "production_inventory_identity": "production-python-modules:v1:sha256:" + "3" * 64,
        "qualified_source_identity": "qualified-source-ready-for-ticket07-preparation:v1:sha256:"
        + "4" * 64,
    }


def test_preparation_seals_source_profile_policy_board_and_plan_without_io(tmp_path):
    blueprint = _blueprint(_config(tmp_path))
    source = _source_binding()
    prepare = composition.seal_production_qualified_routing_preparation
    prepared = prepare(blueprint=blueprint, qualified_source=source, current_source=source)
    assert prepared.plan_signature == _SIGNATURES["1.5"]
    assert prepared.plan_version == "1.5"
    assert prepared.qualified_source_identity == source["qualified_source_identity"]
    assert prepared.factory_contract == blueprint.factory_contract
    validate = composition.validate_production_qualified_routing_preparation
    validate(
        prepared.model_dump(mode="json"),
        blueprint=blueprint,
        qualified_source=source,
        current_source=source,
    )
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("version", ("1.4", "1.5"))
def test_preparation_rejects_stale_source_chain(tmp_path, version):
    source = _source_binding()
    with pytest.raises(ValueError):
        composition.seal_production_qualified_routing_preparation(
            blueprint=_blueprint(_config(tmp_path, version)),
            qualified_source=source,
            current_source=dict(source, commit_id="5" * 40),
        )


@pytest.mark.parametrize(
    "change", ("signature", "profile", "policy", "ceiling", "board", "runtime")
)
def test_preparation_rejects_missing_mismatched_or_plan14_runtime_bindings(tmp_path, change):
    blueprint = _blueprint(_config(tmp_path))
    source = _source_binding()
    prepared = composition.seal_production_qualified_routing_preparation(
        blueprint=blueprint, qualified_source=source, current_source=source
    ).model_dump(mode="json")
    binding = prepared["factory_contract"]["cninfo_binding"]
    if change == "signature":
        prepared["plan_signature"] = _SIGNATURES["1.4"]
    elif change == "profile":
        binding["profile"]["profile_id"] = "equity.v1"
    elif change == "policy":
        binding["profile"].pop("source_policy_identity")
    elif change == "ceiling":
        binding["profile"]["upstream_pacing"][0]["minimum_interval_seconds"] = 0
    elif change == "board":
        binding.pop("authoritative_listing_board")
    else:
        blueprint = _blueprint(_config(tmp_path, "1.4"))
    with pytest.raises(ValueError):
        composition.validate_production_qualified_routing_preparation(
            prepared, blueprint=blueprint, qualified_source=source, current_source=source
        )


def test_plan14_and_plan15_preparation_and_configuration_identities_differ(tmp_path):
    source = _source_binding()
    configs = [_config(tmp_path, version) for version in ("1.4", "1.5")]
    prepared = [
        composition.seal_production_qualified_routing_preparation(
            blueprint=_blueprint(config), qualified_source=source, current_source=source
        )
        for config in configs
    ]
    assert prepared[0].preparation_identity != prepared[1].preparation_identity
    assert configuration_digest(configs[0]) != configuration_digest(configs[1])


@pytest.mark.parametrize(
    ("plan_version", "symbol", "cninfo_operations"),
    (
        ("1.4", "601328.SS", ()),
        ("1.5", "601328.SS", ("post_form", "post_form")),
        ("1.5", "600895.SS", ("post_form", "post_form", "get_document")),
    ),
)
def test_offline_production_assembly_and_shadow_keep_model_facing_legacy_result(
    tmp_path, monkeypatch, plan_version, symbol, cninfo_operations
):
    from tests.test_qualified_routing_composition_acceptance import (
        _fresh_builtin_run,
        _legacy_vendor_methods,
    )
    from tradingagents.graph.financial_tools import FinancialDispatchToolNode

    fresh = _fresh_builtin_run(
        tmp_path,
        monkeypatch,
        symbol=symbol,
        reverse_input_order=False,
        plan_version=plan_version,
    )
    try:
        assert all(provider.calls == [] for provider in fresh.providers.values())
        state = fresh.state(tool_names=("get_balance_sheet",))
        state["messages"][0].id = "offline-production-binding-message"
        legacy_config = dict(fresh.config, mainland_capability_routing_mode="legacy")
        for key in (
            *_CNINFO_KEYS,
            "mainland_capability_routing_plan_version",
            "mainland_capability_routing_plan_signature",
        ):
            legacy_config.pop(key, None)
        legacy_result = FinancialDispatchToolNode(
            config=legacy_config, vendor_methods=_legacy_vendor_methods()
        )(copy.deepcopy(state))
        result = fresh.invoke(state)
        assert result["messages"][-1].content == legacy_result["messages"][0].content
        assert (
            tuple(operation for operation, _kwargs in fresh.providers["cninfo"].calls)
            == cninfo_operations
        )
        if plan_version == "1.5":
            assert "financial_dispatch_shadow_failure" not in result
            assert result["financial_dispatch_shadow_ledger"] is not None
            proof = fresh.owner.context.listing_composite_temporal_proof
            assert proof.effective_evidence.provider_id == "baostock"
            assert proof.publication_evidence.provider_id == "cninfo"
            assert (
                proof.document_evidence is not None
                if symbol == "600895.SS"
                else proof.document_evidence is None
            )
            assert (
                fresh.composition.blueprint.factory_contract.cninfo_binding.authoritative_listing_board.value
                == "xshg_main"
            )
        else:
            assert "cninfo_binding" not in fresh.composition.blueprint.factory_contract.model_dump(
                mode="json"
            )
    finally:
        fresh.close()


def test_cli_selects_plan15_and_seals_it_before_graph_or_provider_work(tmp_path, monkeypatch):
    from tests.test_qualified_routing_composition_acceptance import (
        _DeterministicDateTime,
        _publish_pinned_temporal_calendar,
    )
    from tests.test_qualified_routing_static_registration import _cli_selections
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.store import MarketHistoryStore

    config = _config(tmp_path)
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config)) as store:
        calendar_revision_id = _publish_pinned_temporal_calendar(store)
    _DeterministicDateTime.reset()
    monkeypatch.setattr(cli_main.datetime, "datetime", _DeterministicDateTime)
    selections = _cli_selections()
    selections["analysis_date"] = "2026-09-01"
    received = []

    class GraphBoundaryReached(RuntimeError):
        pass

    def graph_boundary(*_args, **kwargs):
        received.append(kwargs["qualified_financial_routing"].blueprint)
        raise GraphBoundaryReached

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_boundary)
    with pytest.raises(GraphBoundaryReached):
        cli_main.run_analysis(
            qualified_routing_validation=True,
            qualified_routing_calendar_revision_id=calendar_revision_id,
        )
    assert len(received) == 1
    assert received[0].plan_signature == _SIGNATURES["1.5"]
    assert received[0].factory_contract.cninfo_binding.profile.profile_id == _PROFILE


def _programmatic_graph(config, monkeypatch, *, pass_plan):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    blueprint = _blueprint(config)
    asset, result = _preflight(config)
    managed = composition.build_registered_production_qualified_routing_composition(
        blueprint=blueprint
    )
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: MagicMock()),
    )
    arguments = {"capability_routing_plan": result.plan} if pass_plan else {}
    return cli_main.TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=config,
        asset_configuration=asset,
        qualified_financial_routing=managed,
        **arguments,
    )


@pytest.mark.parametrize("version", ("1.4", "1.5"))
def test_programmatic_graph_uses_explicit_selected_plan_without_another_upgrade(
    tmp_path, monkeypatch, version
):
    graph = _programmatic_graph(_config(tmp_path, version), monkeypatch, pass_plan=False)
    try:
        assert graph.capability_routing_plan.plan_signature == _SIGNATURES[version]
    finally:
        graph.close_qualified_routing_runtime()


def test_fresh_run_identity_binds_the_selected_cninfo_policy_instance(tmp_path, monkeypatch):
    from tradingagents.evidence import EvidenceState

    run_ids = []
    profile_ids = []
    # Both are existing qualified offline fixture intervals (acceptance and
    # retrieval-timing fixtures); neither selects an operator's production value.
    for interval in (1, 2):
        config = _config(tmp_path)
        config["cninfo_operator_minimum_interval_seconds"] = interval
        graph = _programmatic_graph(config, monkeypatch, pass_plan=True)
        try:
            state = graph.create_initial_state(
                "601328.SS",
                "2026-09-01",
                evidence_state=EvidenceState(
                    instrument_identity=graph.asset_configuration.instrument_identity,
                    financial_eligibility_binding=graph.capability_routing_plan.financial_eligibility_binding,
                ),
            )
            run_ids.append(state["run_id"])
            profile_ids.append(
                graph.qualified_financial_routing.blueprint.factory_contract.cninfo_binding.profile.profile_identity
            )
        finally:
            graph.close_qualified_routing_runtime()
    assert profile_ids[0] != profile_ids[1]
    assert run_ids[0] != run_ids[1]


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("mainland_capability_routing_plan_version", "1.5"),
        ("cninfo_listing_publication_profile", _PROFILE),
        ("cninfo_source_policy_version", _POLICY),
        ("cninfo_operator_minimum_interval_seconds", _FIXTURE_INTERVAL),
    ),
)
def test_partial_forward_configuration_requires_identity_preflight(tmp_path, key, value):
    config = _config(tmp_path, None)
    config.update(mainland_capability_routing_mode="legacy", tushare_enabled_capabilities=())
    config[key] = value
    assert capability_routing_configuration_is_explicit(config)
    _asset, result = _preflight(config)
    assert not result.passed


@pytest.mark.parametrize("case", ("missing_composition", "changed_policy", "invalid_policy"))
def test_programmatic_binding_failure_is_typed_before_resources_or_models(
    tmp_path, monkeypatch, case
):
    config = _config(tmp_path)
    blueprint = _blueprint(config)
    asset, result = _preflight(config)
    managed = composition.build_registered_production_qualified_routing_composition(
        blueprint=blueprint
    )
    if case == "missing_composition":
        managed = None
    elif case == "changed_policy":
        config["cninfo_operator_minimum_interval_seconds"] = 2
    else:
        config["data_usage_mode"] = "production"

    def forbidden_model(**_kwargs):
        pytest.fail("invalid construction reached a model client")

    monkeypatch.setattr("tradingagents.graph.trading_graph.create_llm_client", forbidden_model)
    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.TradingAgentsGraph(
            selected_analysts=("fundamentals",),
            config=config,
            asset_configuration=asset,
            capability_routing_plan=result.plan,
            qualified_financial_routing=managed,
        )
    assert raised.value.__cause__ is None
    assert not any(tmp_path.iterdir())

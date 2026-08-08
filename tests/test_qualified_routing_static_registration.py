from __future__ import annotations

import copy
import importlib.util
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli.main as cli_main
import tradingagents.qualified_routing_composition as production_composition
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingFailureReason,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPreflightError,
    mainland_capability_routing_failure,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_capability_routing import (
    MainlandFinancialCapabilityRouter,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.financial_tools import QualifiedFinancialRoutingComposition
from tradingagents.qualified_routing_composition import (
    ProductionQualifiedRoutingAssemblyBinding,
    RegisteredProductionQualifiedRoutingComposition,
)


def _valid_shadow_inputs(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, object, dict[str, object]]:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setenv("TUSHARE_TOKEN", "deterministic-fixture-secret")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": ["statements"],
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
    asset = resolve_run_asset_configuration("601328.SS", config=config)
    preflight = preflight_mainland_capability_routing(asset, config=config)
    assert preflight.plan is not None
    plan = preflight.plan
    config["asset_configuration_signature"] = asset.asset_configuration_signature
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    return plan, asset, config


def test_static_builtin_registration_is_immutable_and_deterministic() -> None:
    registration = cli_main.BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY

    assert registration.identity == (
        "production-qualified-routing-composition-factory-v1"
    )
    assert callable(registration.factory)
    with pytest.raises(FrozenInstanceError):
        registration.identity = "operator-override"  # type: ignore[misc]


def test_importing_static_registration_performs_no_external_or_durable_work(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    script = """
import os
import pathlib
import socket
import sqlite3

def forbidden(*_args, **_kwargs):
    raise AssertionError("import attempted external or durable work")

os.makedirs = forbidden
pathlib.Path.mkdir = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden

import cli.main as cli_main

registration = cli_main.BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY
print(registration.identity)
"""
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(repository_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == (
        "production-qualified-routing-composition-factory-v1"
    )
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("selection", ["direct", "operator", "builtin"])
def test_public_builder_precedence_is_direct_then_operator_then_builtin(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    direct = (
        QualifiedFinancialRoutingComposition(
            router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
            ),
            statement_request_factory=lambda _request: None,
        )
        if selection == "direct"
        else None
    )
    operator_result = QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        statement_request_factory=lambda _request: None,
    )
    calls: list[str] = []

    def operator_factory(**_kwargs):
        calls.append("operator")
        return operator_result

    def builtin_factory(**kwargs):
        calls.append("builtin")
        return RegisteredProductionQualifiedRoutingComposition(
            blueprint=kwargs["blueprint"]
        )

    monkeypatch.setattr(
        cli_main.importlib,
        "import_module",
        lambda module_name: (
            SimpleNamespace(build=operator_factory)
            if module_name == "operator_fixture"
            else pytest.fail("unexpected dynamic import")
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=builtin_factory,
        ),
    )
    if selection == "operator":
        config["_qualified_routing_composition_factory"] = (
            "operator_fixture:build"
        )

    resolved = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
        qualified_financial_routing=direct,
    )

    expected_result = {
        "direct": (direct, []),
        "operator": (operator_result, ["operator"]),
        "builtin": (resolved, ["builtin"]),
    }[selection]
    assert (resolved, calls) == expected_result
    if selection == "builtin":
        assert isinstance(resolved, RegisteredProductionQualifiedRoutingComposition)


def test_invalid_direct_composition_does_not_fall_through(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    invalid_direct = object()
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=lambda **_kwargs: pytest.fail("built-in must not run"),
        ),
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
            qualified_financial_routing=invalid_direct,
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
    )


def test_invalid_operator_factory_does_not_fall_through_to_builtin(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    config["_qualified_routing_composition_factory"] = "missing_fixture:build"
    monkeypatch.setattr(
        cli_main.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(build=None),
    )
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=lambda **_kwargs: pytest.fail("built-in must not run"),
        ),
    )

    with pytest.raises(
        ValueError,
        match="qualified routing composition factory is unavailable",
    ):
        cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
        )


def test_invalid_operator_output_is_typed_and_does_not_use_builtin(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    config["_qualified_routing_composition_factory"] = "operator_fixture:build"
    monkeypatch.setattr(
        cli_main.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(build=lambda **_kwargs: object()),
    )
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=lambda **_kwargs: pytest.fail("built-in must not run"),
        ),
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
    )


def test_builtin_consumes_a_validated_deterministic_blueprint_without_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)

    first = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )
    second = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=dict(reversed(tuple(config.items()))),
        validation_workload=True,
    )

    assert isinstance(first, RegisteredProductionQualifiedRoutingComposition)
    assert isinstance(second, RegisteredProductionQualifiedRoutingComposition)
    assert first.factory_identity == (
        "production-qualified-routing-composition-factory-v1"
    )
    assert first.blueprint == second.blueprint
    assert first.blueprint.routing_plan is plan
    assert not any(tmp_path.rglob("*"))


def test_builtin_is_not_invoked_when_blueprint_validation_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    monkeypatch.delenv("TUSHARE_TOKEN")
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=lambda **_kwargs: pytest.fail("built-in must not run"),
        ),
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING
    )


def test_invalid_builtin_output_is_a_typed_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=lambda **_kwargs: object(),
        ),
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
    )


def test_malformed_recognized_composition_is_translated_to_typed_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)

    class ExplosivePlan:
        @property
        def plan_signature(self):
            raise RuntimeError("secret-canary raw failure")

    malformed = object.__new__(RegisteredProductionQualifiedRoutingComposition)
    object.__setattr__(
        malformed,
        "blueprint",
        SimpleNamespace(
            routing_plan=ExplosivePlan(),
        ),
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
            qualified_financial_routing=malformed,
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
    )
    assert "secret-canary" not in str(raised.value)


def test_builtin_exception_is_translated_without_raw_material(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    canary = "secret-canary https://example.invalid/data?token=secret C:\\private"

    def failing_factory(**_kwargs):
        raise RuntimeError(canary)

    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=failing_factory,
        ),
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
    )
    assert canary not in str(raised.value)
    assert raised.value.__cause__ is None


def test_operator_factory_receives_only_safe_configuration(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    config.update(
        {
            "_qualified_routing_composition_factory": "operator_fixture:build",
            "api_token": "secret-canary",
            "provider_payload": {"private": "value"},
            "raw_provider_error": "secret-canary raw failure",
            "credential_url": "https://example.invalid/data?token=secret-canary",
        }
    )
    received: dict[str, object] = {}
    expected = QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        statement_request_factory=lambda _request: None,
    )

    def operator_factory(**kwargs):
        received.update(kwargs)
        return expected

    monkeypatch.setattr(
        cli_main.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(build=operator_factory),
    )

    resolved = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )

    assert resolved is expected
    safe_config = received["config"]
    assert isinstance(safe_config, dict)
    assert not {
        "_qualified_routing_composition_factory",
        "api_token",
        "provider_payload",
        "raw_provider_error",
        "credential_url",
    }.intersection(safe_config)
    assert "secret-canary" not in repr(safe_config)


def test_operator_safe_configuration_redacts_nested_unsafe_material(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    config.update(
        {
            "_qualified_routing_composition_factory": "operator_fixture:build",
            "operator_options": {
                "safe": "retained",
                "authorization": "Bearer secret-canary",
                "custom": SimpleNamespace(token="secret-canary"),
                "nested": [
                    "https://example.invalid/data?token=secret-canary",
                    {"raw_provider_error": RuntimeError("secret-canary")},
                ],
            },
        }
    )
    received: dict[str, object] = {}
    expected = QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        statement_request_factory=lambda _request: None,
    )

    def operator_factory(**kwargs):
        received.update(kwargs)
        return expected

    monkeypatch.setattr(
        cli_main.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(build=operator_factory),
    )

    resolved = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )

    assert resolved is expected
    safe_config = received["config"]
    assert isinstance(safe_config, dict)
    assert safe_config["operator_options"] == {
        "safe": "retained",
        "nested": [],
    }
    assert "secret-canary" not in repr(safe_config)


def test_invalid_operator_import_is_payload_free_and_does_not_use_builtin(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    config["_qualified_routing_composition_factory"] = "operator_fixture:build"
    canary = "secret-canary https://example.invalid/data?token=secret C:\\private"

    def import_failure(_module_name):
        raise RuntimeError(canary)

    monkeypatch.setattr(cli_main.importlib, "import_module", import_failure)
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=lambda **_kwargs: pytest.fail("built-in must not run"),
        ),
    )

    with pytest.raises(
        ValueError,
        match="qualified routing composition factory is unavailable",
    ) as raised:
        cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
        )

    assert canary not in str(raised.value)
    assert raised.value.__cause__ is None


def test_registered_composition_binds_exact_blueprint_through_ticket_02_assembly(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    registered = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )
    assert isinstance(registered, RegisteredProductionQualifiedRoutingComposition)
    assembled = SimpleNamespace(router=SimpleNamespace(routing_plan=plan))
    received: dict[str, object] = {}

    def assembly(**kwargs):
        received.update(kwargs)
        return assembled

    monkeypatch.setattr(
        "tradingagents.qualified_routing_composition.assemble_production_qualified_provider_sources",
        assembly,
    )
    def statement_factory(_request):
        return None

    binding = ProductionQualifiedRoutingAssemblyBinding(
        dependencies=object(),
        source_registrations=(),
        statement_request_factory=statement_factory,
        indicator_request_factory=None,
    )

    bound = registered.bind_run(binding)

    assert received == {
        "blueprint": registered.blueprint,
        "dependencies": binding.dependencies,
        "source_registrations": (),
    }
    assert bound.router is assembled.router
    assert bound.statement_request_factory is statement_factory


def _cli_selections() -> dict[str, object]:
    return {
        "ticker": "601328.SS",
        "analysis_date": "2026-07-30",
        "asset_type": "stock",
        "analysts": [SimpleNamespace(value="fundamentals")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": "fixture-quick",
        "deep_thinker": "fixture-deep",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }


def test_cli_and_programmatic_callers_resolve_same_builtin_identity_and_blueprint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    programmatic = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )
    assert isinstance(programmatic, RegisteredProductionQualifiedRoutingComposition)
    received = None

    class GraphReached(RuntimeError):
        pass

    def graph_constructor(*_args, **kwargs):
        nonlocal received
        received = kwargs["qualified_financial_routing"]
        raise GraphReached("stop before graph, model, or provider work")

    monkeypatch.setattr(cli_main, "get_user_selections", _cli_selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)

    with pytest.raises(GraphReached):
        cli_main.run_analysis(qualified_routing_validation=True)

    assert isinstance(received, RegisteredProductionQualifiedRoutingComposition)
    assert received.factory_identity == programmatic.factory_identity
    assert received.blueprint == programmatic.blueprint
    assert received.blueprint.routing_plan.plan_signature == plan.plan_signature


@pytest.mark.parametrize("entrypoint", ["default_analysis_command", "analyze"])
def test_typer_entrypoints_resolve_same_builtin_as_programmatic_builder(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    programmatic = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )
    assert isinstance(programmatic, RegisteredProductionQualifiedRoutingComposition)
    received = None

    class GraphReached(RuntimeError):
        pass

    def graph_constructor(*_args, **kwargs):
        nonlocal received
        received = kwargs["qualified_financial_routing"]
        raise GraphReached("stop before graph, model, or provider work")

    monkeypatch.setattr(cli_main, "get_user_selections", _cli_selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)
    command = getattr(cli_main, entrypoint)

    with pytest.raises(GraphReached):
        if entrypoint == "default_analysis_command":
            command(
                SimpleNamespace(invoked_subcommand=None),
                checkpoint=False,
                clear_checkpoints=False,
                qualified_routing_validation=True,
                qualified_routing_composition_factory=None,
            )
        else:
            command(
                checkpoint=False,
                clear_checkpoints=False,
                qualified_routing_validation=True,
                qualified_routing_composition_factory=None,
            )

    assert isinstance(received, RegisteredProductionQualifiedRoutingComposition)
    assert received.factory_identity == programmatic.factory_identity
    assert received.blueprint == programmatic.blueprint


def test_real_graph_accepts_registered_builtin_without_provider_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    registered = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=True,
    )
    assert isinstance(registered, RegisteredProductionQualifiedRoutingComposition)
    provider_calls: list[str] = []

    def no_provider_assembly(**_kwargs):
        provider_calls.append("assembly")
        pytest.fail("graph construction must not bind or invoke providers")

    monkeypatch.setattr(
        "tradingagents.qualified_routing_composition.assemble_production_qualified_provider_sources",
        no_provider_assembly,
    )
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )

    graph = cli_main.TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=config,
        asset_configuration=asset,
        capability_routing_plan=plan,
        qualified_financial_routing=registered,
    )

    assert graph.qualified_financial_routing is registered
    assert provider_calls == []
    assert (
        graph.tool_nodes["fundamentals"]._qualified_statement_router is None
    )


def test_run_analysis_uses_shared_builder_for_direct_composition(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _asset_configuration, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    direct = QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        statement_request_factory=lambda _request: None,
    )
    builder_calls: list[object] = []

    class GraphReached(RuntimeError):
        pass

    def builder(**kwargs):
        builder_calls.append(kwargs["qualified_financial_routing"])
        return kwargs["qualified_financial_routing"]

    def graph_constructor(*_args, **_kwargs):
        raise GraphReached("stop before graph, model, or provider work")

    monkeypatch.setattr(cli_main, "get_user_selections", _cli_selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(
        cli_main,
        "build_qualified_financial_routing_composition",
        builder,
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)

    with pytest.raises(GraphReached):
        cli_main.run_analysis(
            qualified_financial_routing=direct,
            qualified_routing_validation=True,
        )

    assert builder_calls == [direct]


@pytest.mark.parametrize("case", ["ordinary_shadow", "empty_plan", "legacy"])
def test_non_applicable_paths_do_not_validate_or_invoke_builtin(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    validation_workload = case != "ordinary_shadow"
    if case == "empty_plan":
        config["tushare_enabled_capabilities"] = []
        preflight = preflight_mainland_capability_routing(asset, config=config)
        assert preflight.plan is not None
        plan = preflight.plan
    elif case == "legacy":
        config = copy.deepcopy(DEFAULT_CONFIG)
        asset = resolve_run_asset_configuration("601328.SS", config=config)
        preflight = preflight_mainland_capability_routing(asset, config=config)
        assert preflight.plan is not None
        plan = preflight.plan
    monkeypatch.setattr(
        cli_main,
        "build_production_qualified_routing_composition_blueprint",
        lambda **_kwargs: pytest.fail("blueprint validation must not run"),
    )
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=lambda **_kwargs: pytest.fail("built-in must not run"),
        ),
    )
    qualified_resource_calls: list[str] = []

    def unexpected_resource(label):
        def fail(*_args, **_kwargs):
            qualified_resource_calls.append(label)
            pytest.fail(f"non-applicable path constructed {label}")

        return fail

    for label, constructor in (
        ("coordinator", production_composition.ProviderRequestCoordinator),
        ("cache", production_composition.ProviderSubrequestCache),
        ("artifact_store", production_composition.ProviderSubrequestArtifactStore),
        (
            "run_dependencies",
            production_composition.ProductionQualifiedRoutingRunDependencies,
        ),
        ("tushare_statement", production_composition.TushareStatementAdapter),
        ("tushare_indicator", production_composition.TushareFinancialIndicatorAdapter),
        ("baostock", production_composition.BaoStockCapabilityAdapter),
        ("baostock_ratio", production_composition.BaoStockRatioFamilyAdapter),
    ):
        monkeypatch.setattr(constructor, "__init__", unexpected_resource(label))
    monkeypatch.setattr(
        cli_main,
        "TradingAgentsGraph",
        unexpected_resource("graph_or_model"),
    )

    resolved = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=asset,
        config=config,
        validation_workload=validation_workload,
    )

    assert resolved is None
    assert qualified_resource_calls == []
    assert not any(tmp_path.rglob("*"))


def test_authoritative_qualified_plan_is_rejected_before_builtin_invocation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    authoritative = plan.model_copy(
        update={"mode": MainlandCapabilityRoutingMode.QUALIFIED_V1}
    )
    monkeypatch.setattr(
        cli_main,
        "BUILTIN_QUALIFIED_ROUTING_COMPOSITION_FACTORY",
        SimpleNamespace(
            identity="production-qualified-routing-composition-factory-v1",
            factory=lambda **_kwargs: pytest.fail("built-in must not run"),
        ),
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.build_qualified_financial_routing_composition(
            plan=authoritative,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_V1_AUTHORITY_DISABLED
    )


@pytest.mark.parametrize(
    "failure_origin",
    ["routing_preflight", "composition_builder"],
)
def test_explicit_validation_preflight_failure_stops_before_run_artifacts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure_origin: str,
) -> None:
    _plan, _asset, config = _valid_shadow_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_main, "get_user_selections", _cli_selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    expected_reason = MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING
    if failure_origin == "routing_preflight":
        monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    else:
        expected_reason = (
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
        )

        def fail_composition_builder(**_kwargs):
            raise MainlandCapabilityRoutingPreflightError(
                mainland_capability_routing_failure(expected_reason)
            )

        monkeypatch.setattr(
            cli_main,
            "build_qualified_financial_routing_composition",
            fail_composition_builder,
        )

    artifact_calls: list[str] = []

    def unexpected_run_artifacts(*_args, **_kwargs):
        artifact_calls.append("prepare")
        raise AssertionError("pre-open validation must not create run artifacts")

    monkeypatch.setattr(cli_main, "_prepare_run_artifacts", unexpected_run_artifacts)

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        cli_main.run_analysis(qualified_routing_validation=True)

    assert raised.value.failure.reason is expected_reason
    assert artifact_calls == []
    assert not any(tmp_path.rglob("*"))

from __future__ import annotations

import copy
import importlib.util
import json
from datetime import date
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph

import cli.main as cli_main
import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.interface as vendor_interface
from tests.test_financial_capability_routing_selection import (
    _ANNUAL_PERIODS,
    _REPORTING_PERIODS,
    _candidate,
)
from tests.test_financial_graph_dispatch import (
    _analysis_outcome_state,
    _financial_vendor_methods,
)
from tests.test_financial_manifest_evidence_reporting import _attempt_event
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingCheckpointAction,
    MainlandCapabilityRoutingCheckpointError,
    MainlandCapabilityRoutingCheckpointFailureReason,
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingFailureReason,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPreflightError,
    MainlandCapabilityRoutingRunProjection,
    TushareCapability,
    preflight_mainland_capability_routing,
    validate_mainland_capability_routing_checkpoint,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialProviderPeriodResponse,
    FinancialStatementRoutingRequest,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialReportingFrequency,
    FinancialStatementType,
)
from tradingagents.decision_audit import prepare_decision_audit
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    EvidenceState,
    SourceAcquisitionAvailable,
    SourceArtifact,
    stable_acquisition_source_ref,
)
from tradingagents.graph.checkpointer import get_checkpointer, thread_id
from tradingagents.graph.financial_tools import (
    FinancialDispatchToolNode,
    QualifiedFinancialRoutingComposition,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree

_QUALIFICATION_PROFILE = "cn-a-2000-20260729-v1"
_LEGACY_PLAN_SIGNATURE = (
    "mainland-routing-plan:v1:"
    "800bd0a8c8015537d4a3b9a0ebaa1209f6bc01a031163e1dd97f34440b8c0d22"
)


def _asset():
    return resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )


def _rollout_config(
    mode: str,
    *,
    enabled: object = ("statements",),
) -> dict[str, object]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": mode,
            "tushare_enabled_capabilities": enabled,
            "tushare_qualification_profile": _QUALIFICATION_PROFILE,
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    return config


def _preflight(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    enabled: object = ("statements",),
):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    return preflight_mainland_capability_routing(
        _asset(),
        config=_rollout_config(mode, enabled=enabled),
        environment={"TUSHARE_TOKEN": "deterministic-fixture-secret"},
    )


def _composition(plan, asset=None) -> QualifiedFinancialRoutingComposition:
    resolved_asset = asset or _asset()
    return QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        statement_request_factory=lambda _request: _statement_request(
            resolved_asset
        ),
        indicator_request_factory=lambda _request: None,
    )


def test_default_rollout_remains_byte_compatible_legacy() -> None:
    result = preflight_mainland_capability_routing(
        _asset(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )

    assert result.plan is not None
    assert DEFAULT_CONFIG["mainland_capability_routing_mode"] == "legacy"
    assert result.plan.mode is MainlandCapabilityRoutingMode.LEGACY
    assert result.plan.plan_signature == _LEGACY_PLAN_SIGNATURE
    assert result.plan.enabled_tushare_capabilities == ()
    assert "tushare" not in {
        provider
        for route in result.plan.routes
        for provider in route.providers
    }


def test_shadow_plan_is_explicit_qualified_routing_but_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _preflight(
        monkeypatch,
        "qualified_v1_shadow",
        enabled=("statements", "financial_indicators"),
    )

    assert result.failure is None
    assert result.plan is not None
    assert result.plan.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
    assert result.plan.route_for(MainlandCapability.BALANCE_SHEET) == (
        "tushare",
        "akshare_sina",
        "yfinance",
    )
    projection = MainlandCapabilityRoutingRunProjection.from_plan(result.plan)
    assert projection.disposition is (
        MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
    )
    assert projection.rollback_compatible is True
    assert projection.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
    assert projection.plan_signature == result.plan.plan_signature
    assert projection.plan_version == result.plan.plan_version
    assert projection.qualification_profile == _QUALIFICATION_PROFILE
    assert projection.enabled_tushare_capabilities == (
        TushareCapability.STATEMENTS,
        TushareCapability.FINANCIAL_INDICATORS,
    )


@pytest.mark.parametrize("mode", ["qualified_v1", "qualified_v1_shadow"])
def test_empty_enablement_activates_no_tushare_provider_route(
    mode: str,
) -> None:
    config = _rollout_config(mode, enabled=())
    result = preflight_mainland_capability_routing(
        _asset(),
        config=config,
        environment={},
    )
    assert result.plan is not None
    assert result.plan.enabled_tushare_capabilities == ()
    assert "tushare" not in {
        provider
        for capability in MainlandCapability
        for provider in result.plan.route_for(capability)
    }


def test_checkpoint_state_identifies_rollout_mode_and_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None

    state = Propagator().create_initial_state(
        "601328.SS",
        "2026-07-30",
        asset_configuration=_asset(),
        capability_routing_plan=plan,
    )

    restored = MainlandCapabilityRoutingRunProjection.model_validate(
        state["capability_routing_rollout"]
    )
    assert restored == MainlandCapabilityRoutingRunProjection.from_plan(plan)
    assert restored.disposition is (
        MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
    )


@pytest.mark.parametrize(
    "capability",
    [
        TushareCapability.STATEMENTS,
        TushareCapability.FINANCIAL_INDICATORS,
        TushareCapability.ADJUSTMENT_FACTORS,
        TushareCapability.NAME_EVENTS,
    ],
)
@pytest.mark.parametrize("mode", ["qualified_v1", "qualified_v1_shadow"])
def test_each_allowed_tushare_capability_activates_independently(
    monkeypatch: pytest.MonkeyPatch,
    capability: TushareCapability,
    mode: str,
) -> None:
    result = _preflight(monkeypatch, mode, enabled=(capability.value,))

    assert result.plan is not None
    assert result.plan.enabled_tushare_capabilities == (capability,)
    plan_json = json.dumps(result.plan.model_dump(mode="json"), sort_keys=True)
    assert "deterministic-fixture-secret" not in plan_json
    assert "TUSHARE_TOKEN" not in plan_json


@pytest.mark.parametrize("mode", ["qualified_v1", "qualified_v1_shadow"])
def test_suspension_status_enablement_fails_typed_before_work(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    result = _preflight(monkeypatch, mode, enabled=("suspension_status",))

    assert result.plan is None
    assert result.failure is not None
    assert result.failure.reason is (
        MainlandCapabilityRoutingFailureReason.UNSUPPORTED_TUSHARE_CAPABILITY
    )
    rendered = result.failure.model_dump_json()
    assert "deterministic-fixture-secret" not in rendered
    assert "TUSHARE_TOKEN" not in rendered


def test_shadow_requires_exact_qualification_profile_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = _rollout_config("qualified_v1_shadow")
    config["tushare_qualification_profile"] = "wrong-profile"

    result = preflight_mainland_capability_routing(
        _asset(),
        config=config,
        environment={"TUSHARE_TOKEN": "deterministic-fixture-secret"},
    )

    assert result.plan is None
    assert result.failure is not None
    assert result.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFICATION_PROFILE_MISMATCH
    )


@pytest.mark.parametrize(
    ("mutation", "environment", "dependency_available", "reason"),
    [
        ({}, {}, True, MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING),
        (
            {},
            {"TUSHARE_TOKEN": "fixture-secret"},
            False,
            MainlandCapabilityRoutingFailureReason.TUSHARE_DEPENDENCY_MISSING,
        ),
        (
            {"data_usage_mode": "production"},
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.DATA_USAGE_MODE_NOT_PERSONAL_RESEARCH,
        ),
        (
            {"tushare_calls_per_minute": 0},
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.TUSHARE_PACING_INVALID,
        ),
        (
            {"tushare_operator_safety_ceiling_calls_per_minute": 41},
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.TUSHARE_OPERATOR_SAFETY_CEILING_INVALID,
        ),
        (
            {"tushare_account_scope_label": "token-primary"},
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.TUSHARE_ACCOUNT_SCOPE_INVALID,
        ),
    ],
)
def test_shadow_configuration_failures_are_typed_before_work(
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
    environment: dict[str, str],
    dependency_available: bool,
    reason: MainlandCapabilityRoutingFailureReason,
) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: object() if dependency_available else None,
    )
    config = _rollout_config("qualified_v1_shadow")
    config.update(mutation)
    provider_calls = 0
    model_calls = 0

    result = preflight_mainland_capability_routing(
        _asset(),
        config=config,
        environment=environment,
    )

    assert result.plan is None
    assert result.failure is not None
    assert result.failure.reason is reason
    assert provider_calls == 0
    assert model_calls == 0
    serialized = result.failure.model_dump_json()
    assert "fixture-secret" not in serialized


def test_shadow_cli_configuration_failure_stops_before_graph_or_provider_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    config = _rollout_config("qualified_v1_shadow")
    config["results_dir"] = str(tmp_path)
    graph_calls = 0
    provider_calls = 0

    def graph_constructor(*_args, **_kwargs):
        nonlocal graph_calls
        graph_calls += 1
        raise AssertionError("graph/model construction must not run")

    selections = {
        "ticker": "601328.SS",
        "analysis_date": "2026-07-30",
        "asset_type": "stock",
        "analysts": [SimpleNamespace(value="market")],
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
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)

    final_state = cli_main.run_analysis()

    assert graph_calls == 0
    assert provider_calls == 0
    assert final_state["analysis_outcome_contract"]["reason"] == "preflight_blocked"
    assert final_state["capability_routing_failure"]["reason"] == (
        "tushare_token_missing"
    )


def test_qualified_graph_requires_explicit_composition_before_model_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1").plan
    assert plan is not None
    model_calls = 0

    def model_client(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        return SimpleNamespace(get_llm=lambda: object())

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        model_client,
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as caught:
        TradingAgentsGraph(
            selected_analysts=("fundamentals",),
            config=_graph_config(tmp_path, plan),
            asset_configuration=_asset(),
            capability_routing_plan=plan,
        )

    assert model_calls == 0
    assert caught.value.failure.reason.value == (
        "qualified_routing_composition_missing"
    )


def test_contradictory_composition_fails_typed_before_model_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_plan = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("statements",),
    ).plan
    contradictory_plan = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("financial_indicators",),
    ).plan
    assert active_plan is not None
    assert contradictory_plan is not None
    model_calls = 0

    def model_client(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        return SimpleNamespace(get_llm=lambda: object())

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        model_client,
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as caught:
        TradingAgentsGraph(
            selected_analysts=("fundamentals",),
            config=_graph_config(tmp_path, active_plan),
            asset_configuration=_asset(),
            capability_routing_plan=active_plan,
            qualified_financial_routing=_composition(contradictory_plan),
        )

    assert model_calls == 0
    assert caught.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_COMPOSITION_MISSING
    )


def test_cli_passes_explicit_qualified_composition_to_graph(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setenv("TUSHARE_TOKEN", "deterministic-fixture-secret")
    config = _rollout_config("qualified_v1")
    config["results_dir"] = str(tmp_path)
    plan = preflight_mainland_capability_routing(
        _asset(),
        config=config,
        environment={"TUSHARE_TOKEN": "deterministic-fixture-secret"},
    ).plan
    assert plan is not None
    composition = _composition(plan)
    selections = {
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
    received = None

    class GraphReached(RuntimeError):
        pass

    def graph_constructor(*_args, **kwargs):
        nonlocal received
        received = kwargs.get("qualified_financial_routing")
        raise GraphReached("stop before model/provider execution")

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)

    with pytest.raises(GraphReached):
        cli_main.run_analysis(qualified_financial_routing=composition)

    assert received is composition


def test_cli_resolves_qualified_composition_before_graph_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setenv("TUSHARE_TOKEN", "deterministic-fixture-secret")
    config = _rollout_config("qualified_v1")
    config["results_dir"] = str(tmp_path)
    selections = {
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
    resolved = None
    received = None

    class GraphReached(RuntimeError):
        pass

    def composition_builder(*, plan, asset_configuration, config, validation_workload):
        nonlocal resolved
        assert asset_configuration == _asset()
        assert config["mainland_capability_routing_plan_signature"] == (
            plan.plan_signature
        )
        assert validation_workload is False
        resolved = _composition(plan, asset_configuration)
        return resolved

    def graph_constructor(*_args, **kwargs):
        nonlocal received
        received = kwargs.get("qualified_financial_routing")
        raise GraphReached("stop before model/provider execution")

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(
        cli_main,
        "build_qualified_financial_routing_composition",
        composition_builder,
        raising=False,
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)

    with pytest.raises(GraphReached):
        cli_main.run_analysis()

    assert resolved is not None
    assert received is resolved


def test_shipped_composition_builder_loads_explicit_operator_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1").plan
    assert plan is not None
    composition = _composition(plan)
    received: dict[str, object] = {}

    def factory(**kwargs):
        received.update(kwargs)
        return composition

    monkeypatch.setattr(
        cli_main.importlib,
        "import_module",
        lambda module_name: (
            SimpleNamespace(build=factory)
            if module_name == "operator_fixture"
            else pytest.fail("unexpected composition module")
        ),
    )
    config = _rollout_config("qualified_v1")
    config["_qualified_routing_composition_factory"] = "operator_fixture:build"

    resolved = cli_main.build_qualified_financial_routing_composition(
        plan=plan,
        asset_configuration=_asset(),
        config=config,
        validation_workload=False,
    )

    assert resolved is composition
    assert received["plan"] is plan
    assert received["asset_configuration"] == _asset()
    assert received["validation_workload"] is False
    assert "_qualified_routing_composition_factory" not in received["config"]


@pytest.mark.parametrize("entrypoint", ["default_analysis_command", "analyze"])
def test_typer_analysis_entrypoints_forward_explicit_shadow_validation_workload(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    received: dict[str, object] = {}

    def run_analysis_spy(**kwargs):
        received.update(kwargs)

    monkeypatch.setattr(cli_main, "run_analysis", run_analysis_spy)
    command = getattr(cli_main, entrypoint)
    if entrypoint == "default_analysis_command":
        command(
            SimpleNamespace(invoked_subcommand=None),
            checkpoint=False,
            clear_checkpoints=False,
            qualified_routing_validation=True,
            qualified_routing_composition_factory="operator_fixture:build",
        )
    else:
        command(
            checkpoint=False,
            clear_checkpoints=False,
            qualified_routing_validation=True,
            qualified_routing_composition_factory="operator_fixture:build",
        )

    assert received == {
        "checkpoint": False,
        "qualified_routing_validation": True,
        "qualified_routing_composition_factory": "operator_fixture:build",
    }


def test_capability_only_composition_needs_no_financial_request_factory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("adjustment_factors",),
    ).plan
    assert plan is not None
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    composition = QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        )
    )

    graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, plan),
        asset_configuration=_asset(),
        capability_routing_plan=plan,
        qualified_financial_routing=composition,
    )

    assert graph.qualified_financial_routing is composition
    assert graph.tool_nodes["fundamentals"]._qualified_statement_router is None


def test_resolved_shadow_plan_ignores_later_environment_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _preflight(monkeypatch, "qualified_v1_shadow")
    assert result.plan is not None
    plan = result.plan
    original = plan.model_dump_json()
    monkeypatch.setenv("TUSHARE_TOKEN", "mutated-after-resolution")
    monkeypatch.setenv(
        "TRADINGAGENTS_MAINLAND_CAPABILITY_ROUTING_MODE",
        "legacy",
    )

    assert plan.model_dump_json() == original
    assert plan.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
    assert plan.route_for(MainlandCapability.BALANCE_SHEET)[0] == "tushare"


def test_rollback_changes_new_runs_immediately_and_reenable_revalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualified = _preflight(monkeypatch, "qualified_v1").plan
    assert qualified is not None
    retained_projection = MainlandCapabilityRoutingRunProjection.from_plan(qualified)

    rolled_back = preflight_mainland_capability_routing(
        _asset(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )

    assert rolled_back.plan is not None
    assert rolled_back.plan.mode is MainlandCapabilityRoutingMode.LEGACY
    assert rolled_back.plan.plan_signature == _LEGACY_PLAN_SIGNATURE
    assert retained_projection.plan_signature == qualified.plan_signature
    assert retained_projection.disposition is (
        MainlandCapabilityRoutingDisposition.QUALIFIED_AUTHORITATIVE
    )

    reenable = preflight_mainland_capability_routing(
        _asset(),
        config=_rollout_config("qualified_v1"),
        environment={},
    )
    assert reenable.plan is None
    assert reenable.failure is not None
    assert reenable.failure.reason is (
        MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING
    )


def test_rollback_preserves_qualified_historical_state_byte_for_byte(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualified = _preflight(monkeypatch, "qualified_v1").plan
    assert qualified is not None
    retained_files = {
        "provider-artifacts/candidate.json": b'{"artifact":"qualified"}\n',
        "manifests/financial.json": b'{"manifest":"retained"}\n',
        "reports/decision.md": b"# Historical qualified report\n",
        "audits/decision.json": b'{"audit":"immutable"}\n',
        "checkpoints/run.db": b"deterministic-checkpoint-bytes",
        "attempts/events.jsonl": b'{"attempt":"coordinated"}\n',
        "snapshots/identity.txt": b"snapshot:unchanged\n",
        "coordinator/history.json": b'{"history":"unchanged"}\n',
    }
    for relative_path, content in retained_files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    before = {
        relative_path: (tmp_path / relative_path).read_bytes()
        for relative_path in retained_files
    }

    rolled_back = preflight_mainland_capability_routing(
        _asset(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )

    assert rolled_back.plan is not None
    assert rolled_back.plan.mode is MainlandCapabilityRoutingMode.LEGACY
    assert MainlandCapabilityRoutingRunProjection.from_plan(
        qualified
    ).plan_signature == qualified.plan_signature
    assert {
        relative_path: (tmp_path / relative_path).read_bytes()
        for relative_path in retained_files
    } == before


def test_qualified_checkpoint_exact_plan_resumes_without_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1").plan
    assert plan is not None
    provider_calls = 0

    compatibility = validate_mainland_capability_routing_checkpoint(
        active_plan=plan,
        checkpoint_plan=plan.model_dump(mode="json"),
    )

    assert provider_calls == 0
    assert compatibility.action is MainlandCapabilityRoutingCheckpointAction.RESUME


def test_pre_rollout_qualified_checkpoint_exact_plan_resumes_without_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1").plan
    assert plan is not None
    provider_calls = 0
    state = {
        "capability_routing_plan": plan.model_dump(mode="json"),
    }

    compatibility = FinancialDispatchToolNode().validate_checkpoint_state(
        state,
        expected_asset_configuration=_asset(),
        expected_capability_routing_plan=plan,
    )

    assert provider_calls == 0
    assert compatibility is not None
    assert compatibility.action is MainlandCapabilityRoutingCheckpointAction.RESUME
    assert compatibility.checkpoint == (
        MainlandCapabilityRoutingRunProjection.from_plan(plan)
    )
    assert compatibility.reason is None
    assert compatibility.provider_io_performed is False
    assert compatibility.checkpoint_rewrite_permitted is False


def test_legacy_checkpoint_under_qualified_starts_new_without_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = preflight_mainland_capability_routing(
        _asset(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    ).plan
    qualified = _preflight(monkeypatch, "qualified_v1").plan
    assert legacy is not None
    assert qualified is not None
    checkpoint_payload = legacy.model_dump(mode="json")
    original_bytes = json.dumps(
        checkpoint_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    compatibility = validate_mainland_capability_routing_checkpoint(
        active_plan=qualified,
        checkpoint_plan=checkpoint_payload,
    )

    assert compatibility.action is (
        MainlandCapabilityRoutingCheckpointAction.START_NEW
    )
    assert compatibility.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.LEGACY_PLAN_REQUIRES_NEW_RUN
    )
    assert compatibility.historical_state_preserved is True
    assert compatibility.checkpoint_rewrite_permitted is False
    assert json.dumps(
        checkpoint_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode() == original_bytes


def test_unversioned_legacy_checkpoint_starts_new_without_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualified = _preflight(monkeypatch, "qualified_v1").plan
    assert qualified is not None

    compatibility = validate_mainland_capability_routing_checkpoint(
        active_plan=qualified,
        checkpoint_plan=None,
    )

    assert compatibility.action is (
        MainlandCapabilityRoutingCheckpointAction.START_NEW
    )
    assert compatibility.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.UNVERSIONED_LEGACY_REQUIRES_NEW_RUN
    )
    assert compatibility.checkpoint is None
    assert compatibility.provider_io_performed is False
    assert compatibility.checkpoint_rewrite_permitted is False


def test_financial_checkpoint_validation_classifies_plan_before_ledger_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = preflight_mainland_capability_routing(
        _asset(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    ).plan
    qualified = _preflight(monkeypatch, "qualified_v1").plan
    assert legacy is not None
    assert qualified is not None
    state = {
        "capability_routing_plan": legacy.model_dump(mode="json"),
        "financial_dispatch_ledger": {"tampered": "must-not-be-read"},
    }

    compatibility = FinancialDispatchToolNode().validate_checkpoint_state(
        state,
        expected_asset_configuration=_asset(),
        expected_capability_routing_plan=qualified,
    )

    assert compatibility is not None
    assert compatibility.action is (
        MainlandCapabilityRoutingCheckpointAction.START_NEW
    )


def test_checkpoint_tampered_rollout_fails_before_ledger_or_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1").plan
    assert plan is not None
    state = {
        "capability_routing_plan": plan.model_dump(mode="json"),
        "capability_routing_rollout": (
            MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(
                mode="json"
            )
        ),
        "financial_dispatch_ledger": {"tampered": "must-not-be-read"},
    }
    state["capability_routing_rollout"]["disposition"] = (
        "shadow_non_authoritative"
    )
    provider_calls = 0

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        FinancialDispatchToolNode().validate_checkpoint_state(
            state,
            expected_asset_configuration=_asset(),
            expected_capability_routing_plan=plan,
        )

    assert provider_calls == 0
    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
    )


def test_checkpoint_tampered_compatibility_fails_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1").plan
    assert plan is not None
    projection = MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(
        mode="json"
    )
    state = {
        "capability_routing_plan": plan.model_dump(mode="json"),
        "capability_routing_rollout": projection,
        "capability_routing_checkpoint_compatibility": {
            "contract_version": "1.0",
            "action": "start_new",
            "reason": "plan_mismatch",
            "active": projection,
            "checkpoint": projection,
            "provider_io_performed": False,
            "checkpoint_rewrite_permitted": False,
            "historical_state_preserved": True,
        },
    }

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        FinancialDispatchToolNode().validate_checkpoint_state(
            state,
            expected_asset_configuration=_asset(),
            expected_capability_routing_plan=plan,
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
    )


def test_checkpoint_tampered_historical_rollout_fails_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _preflight(monkeypatch, "qualified_v1").plan
    legacy = preflight_mainland_capability_routing(
        _asset(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    ).plan
    assert active is not None
    assert legacy is not None
    compatibility = validate_mainland_capability_routing_checkpoint(
        active_plan=active,
        checkpoint_plan=legacy,
    ).model_dump(mode="json")
    compatibility["checkpoint"]["plan_signature"] = (
        "mainland-routing-plan:v1:" + "f" * 64
    )
    state = {
        "capability_routing_plan": active.model_dump(mode="json"),
        "capability_routing_rollout": (
            MainlandCapabilityRoutingRunProjection.from_plan(active).model_dump(
                mode="json"
            )
        ),
        "capability_routing_checkpoint_compatibility": compatibility,
    }

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        FinancialDispatchToolNode().validate_checkpoint_state(
            state,
            expected_asset_configuration=_asset(),
            expected_capability_routing_plan=active,
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
    )


def test_rollout_projection_rejects_mode_profile_and_enablement_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("statements", "financial_indicators"),
    ).plan
    assert plan is not None
    projection = MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(
        mode="json"
    )

    wrong_profile = dict(projection, qualification_profile="legacy")
    duplicate_enablement = dict(
        projection,
        enabled_tushare_capabilities=["statements", "statements"],
    )

    with pytest.raises(ValueError):
        MainlandCapabilityRoutingRunProjection.model_validate(wrong_profile)
    with pytest.raises(ValueError):
        MainlandCapabilityRoutingRunProjection.model_validate(
            duplicate_enablement
        )


def test_checkpoint_tampered_shadow_failure_fails_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    state = {
        "capability_routing_plan": plan.model_dump(mode="json"),
        "capability_routing_rollout": (
            MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(
                mode="json"
            )
        ),
        "financial_dispatch_shadow_failure": {
            "contract_version": "1.0",
            "diagnostic_code": "shadow_dispatch_failed",
            "failure_count": 1,
            "raw_provider_error": "must-not-survive",
        },
    }

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        FinancialDispatchToolNode().validate_checkpoint_state(
            state,
            expected_asset_configuration=_asset(),
            expected_capability_routing_plan=plan,
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
    )
    assert "must-not-survive" not in str(caught.value)


def test_shadow_only_checkpoint_ledger_is_validated_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    state = {
        "capability_routing_plan": plan.model_dump(mode="json"),
        "capability_routing_rollout": (
            MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(
                mode="json"
            )
        ),
        "financial_dispatch_shadow_ledger": {"tampered": "must-be-read"},
    }
    node = FinancialDispatchToolNode(
        config=_rollout_config("qualified_v1_shadow"),
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        qualified_statement_request_factory=lambda _request: _statement_request(
            _asset()
        ),
    )

    with pytest.raises(ValueError):
        node.validate_checkpoint_state(
            state,
            expected_asset_configuration=_asset(),
            expected_capability_routing_plan=plan,
        )


def _graph_config(tmp_path, plan) -> dict[str, object]:
    config = (
        copy.deepcopy(DEFAULT_CONFIG)
        if plan.mode is MainlandCapabilityRoutingMode.LEGACY
        else _rollout_config(plan.mode.value, enabled=("statements",))
    )
    config.update(
        {
            "checkpoint_enabled": True,
            "data_cache_dir": str(tmp_path / "cache"),
            "results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "memory" / "memory.md"),
        }
    )
    if plan.mode is not MainlandCapabilityRoutingMode.LEGACY:
        config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    return config


def _save_graph_checkpoint(graph, asset, *, ticker: str, trade_date: str) -> dict:
    state = graph.create_initial_state(
        ticker,
        trade_date,
        evidence_state=EvidenceState(instrument_identity=asset.instrument_identity),
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("checkpoint", lambda _state: {})
    workflow.set_entry_point("checkpoint")
    workflow.add_edge("checkpoint", END)
    graph_config = {
        "configurable": {
            "thread_id": thread_id(
                ticker,
                trade_date,
                graph._run_signature("stock"),
            )
        }
    }
    with get_checkpointer(graph.config["data_cache_dir"], ticker) as saver:
        workflow.compile(checkpointer=saver).invoke(state, config=graph_config)
    return graph_config


def test_graph_starts_new_for_legacy_checkpoint_without_rewrite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    legacy = preflight_mainland_capability_routing(
        asset,
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    ).plan
    qualified = _preflight(monkeypatch, "qualified_v1").plan
    assert legacy is not None
    assert qualified is not None
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-07-30"
    legacy_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, legacy),
        asset_configuration=asset,
        capability_routing_plan=legacy,
    )
    legacy_checkpoint_config = _save_graph_checkpoint(
        legacy_graph,
        asset,
        ticker=ticker,
        trade_date=trade_date,
    )
    with get_checkpointer(legacy_graph.config["data_cache_dir"], ticker) as saver:
        before = saver.get_tuple(legacy_checkpoint_config).checkpoint

    qualified_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, qualified),
        asset_configuration=asset,
        capability_routing_plan=qualified,
        qualified_financial_routing=_composition(qualified, asset),
    )
    with qualified_graph.checkpoint_scope(ticker, trade_date) as session:
        assert session.resume_from_checkpoint is False
        assert session.routing_compatibility is not None
        assert session.routing_compatibility.action is (
            MainlandCapabilityRoutingCheckpointAction.START_NEW
        )
        assert session.routing_compatibility.reason is (
            MainlandCapabilityRoutingCheckpointFailureReason.LEGACY_PLAN_REQUIRES_NEW_RUN
        )

    with get_checkpointer(legacy_graph.config["data_cache_dir"], ticker) as saver:
        after = saver.get_tuple(legacy_checkpoint_config).checkpoint
    assert after == before


def test_graph_rollback_starts_legacy_without_opening_qualified_checkpoint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    qualified = _preflight(monkeypatch, "qualified_v1").plan
    legacy = preflight_mainland_capability_routing(
        asset,
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    ).plan
    assert qualified is not None
    assert legacy is not None
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-07-30"
    qualified_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, qualified),
        asset_configuration=asset,
        capability_routing_plan=qualified,
        qualified_financial_routing=_composition(qualified, asset),
    )
    qualified_checkpoint_config = _save_graph_checkpoint(
        qualified_graph,
        asset,
        ticker=ticker,
        trade_date=trade_date,
    )
    with get_checkpointer(qualified_graph.config["data_cache_dir"], ticker) as saver:
        before = saver.get_tuple(qualified_checkpoint_config).checkpoint
    legacy_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, legacy),
        asset_configuration=asset,
        capability_routing_plan=legacy,
    )

    with legacy_graph.checkpoint_scope(ticker, trade_date) as session:
        assert session.resume_from_checkpoint is False
        assert session.routing_compatibility is None

    with get_checkpointer(qualified_graph.config["data_cache_dir"], ticker) as saver:
        after = saver.get_tuple(qualified_checkpoint_config).checkpoint
    assert after == before


def test_start_new_cleanup_targets_new_thread_and_preserves_old_checkpoint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1").plan
    assert plan is not None
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-07-30"
    graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, plan),
        asset_configuration=asset,
        capability_routing_plan=plan,
        qualified_financial_routing=_composition(plan, asset),
    )
    old_state = graph.create_initial_state(
        ticker,
        trade_date,
        evidence_state=EvidenceState(
            instrument_identity=asset.instrument_identity
        ),
    )
    old_state.pop("capability_routing_plan", None)
    old_state.pop("capability_routing_rollout", None)
    old_config = {
        "configurable": {
            "thread_id": thread_id(
                ticker,
                trade_date,
                graph._run_signature("stock"),
            )
        }
    }
    workflow = StateGraph(AgentState)
    workflow.add_node("checkpoint", lambda _state: {})
    workflow.set_entry_point("checkpoint")
    workflow.add_edge("checkpoint", END)
    with get_checkpointer(graph.config["data_cache_dir"], ticker) as saver:
        workflow.compile(checkpointer=saver).invoke(old_state, config=old_config)
        old_before = saver.get_tuple(old_config).checkpoint

    with graph.checkpoint_scope(ticker, trade_date) as session:
        assert session.resume_from_checkpoint is False
        assert session.routing_compatibility is not None
        assert session.routing_compatibility.reason is (
            MainlandCapabilityRoutingCheckpointFailureReason.UNVERSIONED_LEGACY_REQUIRES_NEW_RUN
        )
        assert session.run_signature != graph._run_signature("stock")
        new_config = session.graph_config
        new_state = graph.create_initial_state(
            ticker,
            trade_date,
            evidence_state=EvidenceState(
                instrument_identity=asset.instrument_identity
            ),
            checkpoint_thread_id=new_config["configurable"]["thread_id"],
        )
        with get_checkpointer(graph.config["data_cache_dir"], ticker) as saver:
            workflow.compile(checkpointer=saver).invoke(
                new_state,
                config=new_config,
            )
        graph.clear_run_checkpoint(
            ticker,
            trade_date,
            "stock",
            run_signature=session.run_signature,
        )

    with get_checkpointer(graph.config["data_cache_dir"], ticker) as saver:
        assert saver.get_tuple(old_config).checkpoint == old_before
        assert saver.get_tuple(new_config) is None


@pytest.mark.parametrize(
    ("active_mode", "checkpoint_mode", "reason"),
    [
        (
            "qualified_v1",
            "qualified_v1",
            MainlandCapabilityRoutingCheckpointFailureReason.PLAN_MISMATCH,
        ),
        (
            "qualified_v1",
            "qualified_v1_shadow",
            MainlandCapabilityRoutingCheckpointFailureReason.ROLLOUT_MODE_MISMATCH,
        ),
        (
            "qualified_v1_shadow",
            "qualified_v1",
            MainlandCapabilityRoutingCheckpointFailureReason.ROLLOUT_MODE_MISMATCH,
        ),
    ],
)
def test_graph_rejects_incompatible_qualified_checkpoint_before_execution(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    active_mode: str,
    checkpoint_mode: str,
    reason: MainlandCapabilityRoutingCheckpointFailureReason,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    checkpoint_enabled = (
        ("financial_indicators",)
        if active_mode == checkpoint_mode
        else ("statements",)
    )
    checkpoint_plan = _preflight(
        monkeypatch,
        checkpoint_mode,
        enabled=checkpoint_enabled,
    ).plan
    active_plan = _preflight(
        monkeypatch,
        active_mode,
        enabled=("statements",),
    ).plan
    assert checkpoint_plan is not None
    assert active_plan is not None
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-07-30"
    checkpoint_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, checkpoint_plan),
        asset_configuration=asset,
        capability_routing_plan=checkpoint_plan,
        qualified_financial_routing=_composition(checkpoint_plan, asset),
    )
    _save_graph_checkpoint(
        checkpoint_graph,
        asset,
        ticker=ticker,
        trade_date=trade_date,
    )
    active_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, active_plan),
        asset_configuration=asset,
        capability_routing_plan=active_plan,
        qualified_financial_routing=_composition(active_plan, asset),
    )

    with (
        pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught,
        active_graph.checkpoint_scope(ticker, trade_date),
    ):
        pass

    assert caught.value.reason is reason


@pytest.mark.parametrize("legacy_saved_last", [False, True])
def test_mixed_checkpoint_history_never_masks_qualified_plan_mismatch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_saved_last: bool,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    active = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("statements",),
    ).plan
    mismatch = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("financial_indicators",),
    ).plan
    legacy = preflight_mainland_capability_routing(
        asset,
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    ).plan
    assert active is not None
    assert mismatch is not None
    assert legacy is not None
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-07-30"

    def save(plan) -> dict:
        graph = TradingAgentsGraph(
            selected_analysts=("fundamentals",),
            config=_graph_config(tmp_path, plan),
            asset_configuration=asset,
            capability_routing_plan=plan,
            qualified_financial_routing=(
                _composition(plan, asset)
                if plan.mode is not MainlandCapabilityRoutingMode.LEGACY
                else None
            ),
        )
        return _save_graph_checkpoint(
            graph,
            asset,
            ticker=ticker,
            trade_date=trade_date,
        )

    saved_configs = (
        (save(mismatch), save(legacy))
        if legacy_saved_last
        else (save(legacy), save(mismatch))
    )
    with get_checkpointer(str(tmp_path / "cache"), ticker) as saver:
        before = [saver.get_tuple(config).checkpoint for config in saved_configs]
    active_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, active),
        asset_configuration=asset,
        capability_routing_plan=active,
        qualified_financial_routing=_composition(active, asset),
    )

    with (
        pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught,
        active_graph.checkpoint_scope(ticker, trade_date),
    ):
        pass

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.PLAN_MISMATCH
    )
    with get_checkpointer(str(tmp_path / "cache"), ticker) as saver:
        after = [saver.get_tuple(config).checkpoint for config in saved_configs]
    assert after == before


def test_checkpoint_scan_ignores_different_routing_neutral_graph_identity(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    active = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("statements",),
    ).plan
    mismatch = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("financial_indicators",),
    ).plan
    assert active is not None
    assert mismatch is not None
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-07-30"
    other_graph = TradingAgentsGraph(
        selected_analysts=("market",),
        config=_graph_config(tmp_path, mismatch),
        asset_configuration=asset,
        capability_routing_plan=mismatch,
        qualified_financial_routing=_composition(mismatch, asset),
    )
    _save_graph_checkpoint(
        other_graph,
        asset,
        ticker=ticker,
        trade_date=trade_date,
    )
    active_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, active),
        asset_configuration=asset,
        capability_routing_plan=active,
        qualified_financial_routing=_composition(active, asset),
    )

    with active_graph.checkpoint_scope(ticker, trade_date) as session:
        assert session.resume_from_checkpoint is False
        assert session.routing_compatibility is None


def test_checkpoint_scan_ignores_untagged_qualified_checkpoint_from_other_graph(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    active = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("statements",),
    ).plan
    mismatch = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("financial_indicators",),
    ).plan
    assert active is not None
    assert mismatch is not None
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-07-30"
    other_graph = TradingAgentsGraph(
        selected_analysts=("market",),
        config=_graph_config(tmp_path, mismatch),
        asset_configuration=asset,
        capability_routing_plan=mismatch,
        qualified_financial_routing=_composition(mismatch, asset),
    )
    state = other_graph.create_initial_state(
        ticker,
        trade_date,
        evidence_state=EvidenceState(
            instrument_identity=asset.instrument_identity
        ),
    )
    state.pop("checkpoint_graph_identity", None)
    workflow = StateGraph(AgentState)
    workflow.add_node("checkpoint", lambda _state: {})
    workflow.set_entry_point("checkpoint")
    workflow.add_edge("checkpoint", END)
    other_config = {
        "configurable": {
            "thread_id": thread_id(
                ticker,
                trade_date,
                other_graph._run_signature("stock"),
            )
        }
    }
    with get_checkpointer(other_graph.config["data_cache_dir"], ticker) as saver:
        workflow.compile(checkpointer=saver).invoke(state, config=other_config)

    active_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, active),
        asset_configuration=asset,
        capability_routing_plan=active,
        qualified_financial_routing=_composition(active, asset),
    )

    with active_graph.checkpoint_scope(ticker, trade_date) as session:
        assert session.resume_from_checkpoint is False
        assert session.routing_compatibility is None


def test_qualified_checkpoint_cannot_be_downgraded_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualified = _preflight(monkeypatch, "qualified_v1").plan
    legacy = preflight_mainland_capability_routing(
        _asset(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    ).plan
    assert qualified is not None
    assert legacy is not None

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        validate_mainland_capability_routing_checkpoint(
            active_plan=legacy,
            checkpoint_plan=qualified.model_dump(mode="json"),
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.QUALIFIED_PLAN_DOWNGRADE_FORBIDDEN
    )


def test_mismatched_qualified_checkpoint_fails_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("statements",),
    ).plan
    checkpoint = _preflight(
        monkeypatch,
        "qualified_v1",
        enabled=("financial_indicators",),
    ).plan
    assert active is not None
    assert checkpoint is not None

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        validate_mainland_capability_routing_checkpoint(
            active_plan=active,
            checkpoint_plan=checkpoint.model_dump(mode="json"),
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.PLAN_MISMATCH
    )


@pytest.mark.parametrize(
    ("active_mode", "checkpoint_mode"),
    [
        ("qualified_v1", "qualified_v1_shadow"),
        ("qualified_v1_shadow", "qualified_v1"),
    ],
)
def test_shadow_and_authoritative_checkpoint_states_cannot_be_confused(
    monkeypatch: pytest.MonkeyPatch,
    active_mode: str,
    checkpoint_mode: str,
) -> None:
    active = _preflight(monkeypatch, active_mode).plan
    checkpoint = _preflight(monkeypatch, checkpoint_mode).plan
    assert active is not None
    assert checkpoint is not None

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        validate_mainland_capability_routing_checkpoint(
            active_plan=active,
            checkpoint_plan=checkpoint.model_dump(mode="json"),
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.ROLLOUT_MODE_MISMATCH
    )


def test_tampered_checkpoint_plan_fails_typed_without_secret_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1").plan
    assert plan is not None
    tampered = plan.model_dump(mode="json")
    tampered["qualification_profile"] = "legacy"
    tampered["unsafe_token"] = "deterministic-fixture-secret"

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        validate_mainland_capability_routing_checkpoint(
            active_plan=plan,
            checkpoint_plan=tampered,
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
    )
    assert "deterministic-fixture-secret" not in str(caught.value)


def _statement_request(asset) -> FinancialStatementRoutingRequest:
    return FinancialStatementRoutingRequest(
        instrument_identity=asset.instrument_identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=_ANNUAL_PERIODS,
        eligible_reporting_period_ends=_REPORTING_PERIODS,
    )


def _financial_tool_state(asset, plan) -> dict[str, object]:
    identity_source_ref = stable_acquisition_source_ref(
        "identity-registry",
        asset.registry_source_ref,
        asset.registry_digest,
    )
    identity_artifact = SourceArtifact(
        artifact_sha256=asset.registry_digest,
        source_ref=identity_source_ref,
        tool_call_id="identity-registry",
        tool_name="instrument_identity_registry",
        raw_text=asset.registry_artifact,
    )
    return {
        "messages": [
            AIMessage(
                content="",
                id="shadow-model-message",
                tool_calls=[
                    {
                        "name": "get_balance_sheet",
                        "args": {
                            "ticker": asset.instrument_identity.symbol,
                            "freq": "quarterly",
                            "curr_date": "2026-07-30",
                        },
                        "id": "shadow-balance-sheet",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "trade_date": "2026-07-30",
        "run_id": "run:" + "b" * 64,
        "asset_configuration": asset.model_dump(mode="json"),
        "capability_routing_plan": plan.model_dump(mode="json"),
        "evidence_state": EvidenceState(
            instrument_identity=asset.instrument_identity,
            source_artifacts=(identity_artifact,),
            acquisition_outcomes=(
                SourceAcquisitionAvailable(
                    provider="instrument-identity-registry",
                    capability="instrument_identity",
                    source_ref=identity_source_ref,
                    attempt=1,
                    retrieved_at=(
                        asset.instrument_identity.provenance.retrieved_at
                    ),
                    artifact=identity_artifact,
                ),
            ),
        ).model_dump(mode="json"),
    }


def test_shadow_mode_without_validation_composition_uses_only_legacy_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    legacy_calls = 0

    def legacy_balance_sheet(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        nonlocal legacy_calls
        legacy_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    config = _rollout_config("qualified_v1_shadow")
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "scripted",
    )
    vendor_methods = _financial_vendor_methods(legacy_balance_sheet)
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
    )

    result = node(_financial_tool_state(asset, plan))

    assert legacy_calls == 1
    assert "LegacyTotal,100" in result["messages"][0].content
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.0"
    assert "financial_dispatch_shadow_ledger" not in result


@pytest.mark.parametrize("mode", ["qualified_v1", "qualified_v1_shadow"])
def test_disabled_statement_capability_keeps_legacy_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    asset = _asset()
    plan = _preflight(
        monkeypatch,
        mode,
        enabled=("adjustment_factors",),
    ).plan
    assert plan is not None
    legacy_calls = 0
    qualified_factory_calls = 0

    def legacy_balance_sheet(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        nonlocal legacy_calls
        legacy_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    def statement_factory(_request):
        nonlocal qualified_factory_calls
        qualified_factory_calls += 1
        return _statement_request(asset)

    config = _rollout_config(mode, enabled=("adjustment_factors",))
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "scripted",
    )
    vendor_methods = _financial_vendor_methods(legacy_balance_sheet)
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        qualified_statement_request_factory=statement_factory,
    )

    result = node(_financial_tool_state(asset, plan))

    assert legacy_calls == 1
    assert qualified_factory_calls == 0
    assert "LegacyTotal,100" in result["messages"][0].content
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.0"
    assert "financial_dispatch_shadow_ledger" not in result


def test_shadow_dispatch_failure_cannot_change_legacy_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None

    def legacy_balance_sheet(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    def failing_source(_request):
        raise RuntimeError("raw-provider-secret-must-not-escape")

    config = _rollout_config("qualified_v1_shadow")
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "scripted",
    )
    vendor_methods = _financial_vendor_methods(legacy_balance_sheet)
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": failing_source},
        ),
        qualified_statement_request_factory=lambda _request: _statement_request(
            asset
        ),
    )

    result = node(_financial_tool_state(asset, plan))

    assert "LegacyTotal,100" in result["messages"][0].content
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.0"
    assert result["financial_dispatch_shadow_failure"] == {
        "contract_version": "1.0",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
    }
    assert "raw-provider-secret-must-not-escape" not in json.dumps(
        result,
        default=str,
    )


def test_shadow_request_factory_failure_cannot_change_legacy_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None

    def legacy_balance_sheet(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    def failing_factory(_request):
        raise RuntimeError("raw-shadow-factory-secret")

    config = _rollout_config("qualified_v1_shadow")
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "scripted",
    )
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=_financial_vendor_methods(legacy_balance_sheet),
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        qualified_statement_request_factory=failing_factory,
    )

    result = node(_financial_tool_state(asset, plan))

    assert "LegacyTotal,100" in result["messages"][0].content
    assert result["financial_dispatch_shadow_failure"]["failure_count"] == 1
    assert "raw-shadow-factory-secret" not in json.dumps(result, default=str)


def test_later_shadow_failure_preserves_prior_manifest_and_legacy_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    legacy_calls = 0
    shadow_calls = 0

    def legacy_statement(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        nonlocal legacy_calls
        legacy_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    candidates = tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.ANNUAL,
            instrument_identity=asset.instrument_identity,
        )
        for period in _ANNUAL_PERIODS
    ) + tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
            instrument_identity=asset.instrument_identity,
        )
        for period in _REPORTING_PERIODS
    )
    event = _attempt_event()

    def qualified_source(_request):
        nonlocal shadow_calls
        shadow_calls += 1
        if shadow_calls == 2:
            raise RuntimeError("raw-second-shadow-secret")
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=candidates[0].artifact,
            retained_artifacts=tuple(candidate.artifact for candidate in candidates),
            candidates=candidates,
            subrequest_keys=(event.request_key,),
            physical_attempt_ids=(event.attempt_event_id,),
            physical_attempt_events=(event,),
        )

    config = _rollout_config("qualified_v1_shadow")
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "scripted",
    )
    vendor_methods = {
        tool_name: {"scripted": legacy_statement}
        for tool_name in (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        )
    }
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": qualified_source},
        ),
        qualified_statement_request_factory=lambda request: (
            _statement_request(asset).model_copy(
                update={"statement_type": request.statement_type}
            )
        ),
    )
    state = _financial_tool_state(asset, plan)
    state["messages"] = [
        AIMessage(
            content="",
            id="two-shadow-requests",
            tool_calls=[
                {
                    "name": "get_balance_sheet",
                    "args": {
                        "ticker": asset.instrument_identity.symbol,
                        "freq": "quarterly",
                        "curr_date": "2026-07-30",
                    },
                    "id": "shadow-success",
                    "type": "tool_call",
                },
                {
                    "name": "get_income_statement",
                    "args": {
                        "ticker": asset.instrument_identity.symbol,
                        "freq": "quarterly",
                        "curr_date": "2026-07-30",
                    },
                    "id": "shadow-failure",
                    "type": "tool_call",
                },
            ],
        )
    ]

    result = node(state)

    assert legacy_calls == 2
    assert shadow_calls == 2
    assert len(result["messages"]) == 2
    assert "LegacyTotal,100" in result["messages"][0].content
    assert [message.tool_call_id for message in result["messages"]] == [
        "shadow-success",
        "shadow-failure",
    ]
    shadow_ledger = result["financial_dispatch_shadow_ledger"]
    assert len(shadow_ledger["qualified_statement_selections"]) == 1
    manifest = shadow_ledger["qualified_statement_selections"][0]["terminal"][
        "routing_result"
    ]["manifest"]
    assert manifest["selections"]
    assert result["financial_dispatch_shadow_failure"] == {
        "contract_version": "1.0",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
    }
    assert "raw-second-shadow-secret" not in json.dumps(result, default=str)


def test_earlier_shadow_failure_does_not_skip_later_independent_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    shadow_calls = 0
    event = _attempt_event()

    def legacy_statement(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    def qualified_source(request):
        nonlocal shadow_calls
        shadow_calls += 1
        if shadow_calls == 1:
            raise RuntimeError("raw-first-shadow-secret")
        candidates = tuple(
            _candidate(
                provider_id="tushare",
                statement_type=request.statement_type,
                period_end=period,
                frequency=FinancialReportingFrequency.ANNUAL,
                instrument_identity=asset.instrument_identity,
            )
            for period in _ANNUAL_PERIODS
        ) + tuple(
            _candidate(
                provider_id="tushare",
                statement_type=request.statement_type,
                period_end=period,
                frequency=FinancialReportingFrequency.QUARTERLY,
                instrument_identity=asset.instrument_identity,
            )
            for period in _REPORTING_PERIODS
        )
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=candidates[0].artifact,
            retained_artifacts=tuple(candidate.artifact for candidate in candidates),
            candidates=candidates,
            subrequest_keys=(event.request_key,),
            physical_attempt_ids=(event.attempt_event_id,),
            physical_attempt_events=(event,),
        )

    config = _rollout_config("qualified_v1_shadow")
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "scripted",
    )
    vendor_methods = {
        tool_name: {"scripted": legacy_statement}
        for tool_name in config["tool_vendors"]
    }
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": qualified_source},
        ),
        qualified_statement_request_factory=lambda request: (
            _statement_request(asset).model_copy(
                update={"statement_type": request.statement_type}
            )
        ),
    )
    state = _financial_tool_state(asset, plan)
    state["messages"] = [
        AIMessage(
            content="",
            id="failure-then-success",
            tool_calls=[
                {
                    "name": "get_balance_sheet",
                    "args": {
                        "ticker": asset.instrument_identity.symbol,
                        "freq": "quarterly",
                        "curr_date": "2026-07-30",
                    },
                    "id": "first-shadow-failure",
                    "type": "tool_call",
                },
                {
                    "name": "get_income_statement",
                    "args": {
                        "ticker": asset.instrument_identity.symbol,
                        "freq": "quarterly",
                        "curr_date": "2026-07-30",
                    },
                    "id": "later-shadow-success",
                    "type": "tool_call",
                },
            ],
        )
    ]

    result = node(state)

    assert shadow_calls == 2
    assert len(result["messages"]) == 2
    shadow_ledger = result["financial_dispatch_shadow_ledger"]
    assert len(shadow_ledger["qualified_statement_selections"]) == 1
    assert shadow_ledger["qualified_statement_selections"][0]["terminal"][
        "selection_request"
    ]["statement_type"] == "income_statement"
    assert result["financial_dispatch_shadow_failure"]["failure_count"] == 1
    assert "raw-first-shadow-secret" not in json.dumps(result, default=str)


def test_shadow_financial_dispatch_retains_qualified_manifest_without_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    legacy_calls = 0
    shadow_calls = 0

    def legacy_balance_sheet(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        nonlocal legacy_calls
        legacy_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    candidates = tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.ANNUAL,
            instrument_identity=asset.instrument_identity,
        )
        for period in _ANNUAL_PERIODS
    ) + tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
            instrument_identity=asset.instrument_identity,
        )
        for period in _REPORTING_PERIODS
    )
    event = _attempt_event()

    def qualified_source(_request):
        nonlocal shadow_calls
        shadow_calls += 1
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=candidates[0].artifact,
            retained_artifacts=tuple(candidate.artifact for candidate in candidates),
            candidates=candidates,
            subrequest_keys=(event.request_key,),
            physical_attempt_ids=(event.attempt_event_id,),
            physical_attempt_events=(event,),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={"tushare": qualified_source},
    )
    config = _rollout_config("qualified_v1_shadow")
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "scripted",
    )
    vendor_methods = _financial_vendor_methods(legacy_balance_sheet)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    monkeypatch.setattr(config_module, "_config", config)
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=router,
        qualified_statement_request_factory=lambda _request: _statement_request(asset),
    )

    result = node(_financial_tool_state(asset, plan))

    assert legacy_calls == 1
    assert shadow_calls == 1
    assert "LegacyTotal,100" in result["messages"][0].content
    assert "financial_dispatch_ledger" in result
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.0"
    assert "financial_dispatch_shadow_ledger" in result
    shadow_ledger = result["financial_dispatch_shadow_ledger"]
    assert shadow_ledger["contract_version"] == "1.2"
    manifest = shadow_ledger["qualified_statement_selections"][0]["terminal"][
        "routing_result"
    ]["manifest"]
    assert manifest["capability_routing_plan_signature"] == plan.plan_signature
    assert manifest["selections"]
    evidence = EvidenceState.model_validate(result["evidence_state"])
    assert evidence.source_facts == ()
    assert len(evidence.source_artifacts) == 1
    assert evidence.source_artifacts[0].tool_name == "instrument_identity_registry"

    initial_legacy_calls = legacy_calls
    initial_shadow_calls = shadow_calls
    calls_forbidden = True

    def fail_if_legacy_repeats(*_args, **_kwargs):
        if calls_forbidden:
            raise AssertionError("legacy provider I/O occurred during checkpoint resume")

    def fail_if_shadow_repeats(_request):
        if calls_forbidden:
            raise AssertionError("shadow provider I/O occurred during checkpoint resume")
        return qualified_source(_request)

    resumed_vendor_methods = _financial_vendor_methods(fail_if_legacy_repeats)
    resumed_router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={"tushare": fail_if_shadow_repeats},
    )
    resumed_node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=resumed_vendor_methods,
        qualified_statement_router=resumed_router,
        qualified_statement_request_factory=lambda _request: _statement_request(asset),
    )
    resumed_state = _financial_tool_state(asset, plan)
    resumed_state["financial_dispatch_ledger"] = result[
        "financial_dispatch_ledger"
    ]
    resumed_state["financial_dispatch_shadow_ledger"] = shadow_ledger

    resumed = resumed_node(resumed_state)

    assert legacy_calls == initial_legacy_calls
    assert shadow_calls == initial_shadow_calls
    assert "LegacyTotal,100" in resumed["messages"][0].content
    resumed_shadow = resumed["financial_dispatch_shadow_ledger"]
    assert (
        resumed_shadow["qualified_statement_selections"][0]["terminal"]
        == shadow_ledger["qualified_statement_selections"][0]["terminal"]
    )
    assert resumed_shadow["qualified_statement_selections"][0]["reuse_count"] == 1

    final_state = _analysis_outcome_state(
        evidence,
        ticker=asset.instrument_identity.symbol,
    )
    final_state.update(
        {
            "run_id": "run:" + "b" * 64,
            "graph_signature": "|".join(
                (
                    "asset_configuration="
                    + asset.asset_configuration_signature,
                    "capability_routing=" + plan.plan_signature,
                )
            ),
            "asset_configuration": asset.model_dump(mode="json"),
            "capability_routing_plan": plan.model_dump(mode="json"),
            "capability_routing_rollout": (
                MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(
                    mode="json"
                )
            ),
            "financial_dispatch_ledger": result["financial_dispatch_ledger"],
            "financial_dispatch_shadow_ledger": shadow_ledger,
            "decision_audit_created_at": "2026-07-30T12:00:00+00:00",
        }
    )

    audit = prepare_decision_audit(
        final_state,
        tmp_path / "audit-artifacts",
        config=config,
    )

    assert audit["capability_routing_rollout"]["mode"] == "qualified_v1_shadow"
    assert audit["capability_routing_rollout"]["disposition"] == (
        "shadow_non_authoritative"
    )
    assert audit["financial_dispatch"]["contract_version"] == "1.0"
    assert audit["financial_dispatch_shadow"]["contract_version"] == "2.0"
    serialized_audit = json.dumps(audit, sort_keys=True)
    assert "deterministic-fixture-secret" not in serialized_audit
    assert "token_digest" not in serialized_audit
    assert "raw_payload" not in serialized_audit
    assert "raw_error" not in serialized_audit

    tampered_rollout_state = copy.deepcopy(final_state)
    tampered_rollout_state["capability_routing_rollout"]["disposition"] = (
        "qualified_authoritative"
    )
    with pytest.raises(ValueError):
        prepare_decision_audit(
            tampered_rollout_state,
            tmp_path / "tampered-rollout-audit",
            config=config,
        )

    qualified_plan = _preflight(monkeypatch, "qualified_v1").plan
    assert qualified_plan is not None
    contradictory_checkpoint = validate_mainland_capability_routing_checkpoint(
        active_plan=qualified_plan,
        checkpoint_plan=qualified_plan,
    )
    tampered_checkpoint_state = copy.deepcopy(final_state)
    tampered_checkpoint_state[
        "capability_routing_checkpoint_compatibility"
    ] = contradictory_checkpoint.model_dump(mode="json")
    with pytest.raises(
        ValueError,
        match="checkpoint compatibility contradicts the active rollout",
    ):
        prepare_decision_audit(
            tampered_checkpoint_state,
            tmp_path / "tampered-checkpoint-audit",
            config=config,
        )

    report_path = write_report_tree(
        final_state,
        asset.instrument_identity.symbol,
        tmp_path / "report",
    )
    report = report_path.read_text(encoding="utf-8")
    assert "Qualified routing shadow comparison" in report
    assert "non-authoritative" in report
    assert "Legacy-selected financial output remains authoritative" in report
    assert "financial-period-candidate" not in report

    failed_shadow_state = copy.deepcopy(final_state)
    failed_shadow_state["financial_dispatch_shadow_failure"] = {
        "contract_version": "1.0",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
    }
    failed_shadow_audit = prepare_decision_audit(
        failed_shadow_state,
        tmp_path / "failed-shadow-audit",
        config=config,
    )
    assert failed_shadow_audit["financial_dispatch_shadow_failure"] == {
        "contract_version": "1.0",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
    }
    failed_shadow_report_path = write_report_tree(
        failed_shadow_state,
        asset.instrument_identity.symbol,
        tmp_path / "failed-shadow-report",
    )
    failed_shadow_report = failed_shadow_report_path.read_text(encoding="utf-8")
    assert "Sanitized shadow failures:** 1" in failed_shadow_report
    assert "raw-provider-secret" not in json.dumps(
        failed_shadow_audit,
        sort_keys=True,
    )

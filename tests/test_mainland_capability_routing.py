from __future__ import annotations

import copy
import importlib.util
import json
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langgraph.graph import END, StateGraph
from pydantic import ValidationError

import cli.main as cli_main
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingFailureReason,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
    MainlandCapabilityRoutingPreflightError,
    TushareCapability,
    preflight_mainland_capability_routing,
)
from tradingagents.decision_audit import prepare_decision_audit
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    EvidenceState,
    SourceAcquisitionAvailable,
    SourceArtifact,
    render_analysis_outcome,
    stable_acquisition_source_ref,
)
from tradingagents.graph.checkpointer import get_checkpointer
from tradingagents.graph.trading_graph import TradingAgentsGraph


def _mainland_asset_configuration():
    return resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )


def _qualified_config(*, enabled: object) -> dict[str, object]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": enabled,
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    return config


def _resign_plan_payload(payload: dict[str, object]) -> None:
    signature_payload = dict(payload)
    signature_payload.pop("plan_signature", None)
    encoded = json.dumps(
        signature_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["plan_signature"] = (
        "mainland-routing-plan:v1:" + sha256(encoded).hexdigest()
    )


def test_default_configuration_resolves_the_legacy_plan_without_tushare() -> None:
    result = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )
    assert result.passed is True
    assert result.failure is None
    assert result.plan is not None
    assert result.plan.mode is MainlandCapabilityRoutingMode.LEGACY
    assert result.plan.enabled_tushare_capabilities == ()
    assert result.plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT) == (
        "akshare",
        "baostock",
        "yfinance",
    )


def test_mainland_plan_requires_an_authoritative_mainland_equity_identity() -> None:
    fund_configuration = resolve_run_asset_configuration(
        "510500.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    result = preflight_mainland_capability_routing(
        fund_configuration,
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )

    assert result.failure is not None
    assert result.failure.reason is (
        MainlandCapabilityRoutingFailureReason.INSTRUMENT_NOT_MAINLAND_EQUITY
    )


def test_qualified_v1_resolves_closed_versioned_routes_and_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": [
                "name_events",
                "adjustment_factors",
                "financial_indicators",
                "statements",
            ],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )

    result = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-secret"},
    )

    assert result.passed is True
    assert result.plan is not None
    plan = result.plan
    assert plan.plan_version == "1.0"
    assert plan.policy_version == "mainland-capability-routing-policy-v1"
    assert plan.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1
    assert plan.qualification_profile == "cn-a-2000-20260729-v1"
    assert plan.enabled_tushare_capabilities == (
        TushareCapability.STATEMENTS,
        TushareCapability.FINANCIAL_INDICATORS,
        TushareCapability.ADJUSTMENT_FACTORS,
        TushareCapability.NAME_EVENTS,
    )
    assert plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT) == (
        "akshare",
        "baostock",
        "yfinance",
    )
    assert plan.route_for(MainlandCapability.BALANCE_SHEET) == (
        "tushare",
        "akshare_sina",
        "yfinance",
    )
    assert plan.route_for(MainlandCapability.INCOME_STATEMENT) == (
        "tushare",
        "akshare_sina",
        "yfinance",
    )
    assert plan.route_for(MainlandCapability.CASH_FLOW) == (
        "tushare",
        "akshare_sina",
        "yfinance",
    )
    assert plan.route_for(MainlandCapability.FINANCIAL_INDICATORS) == (
        "tushare",
        "akshare",
        "baostock_qualified_families",
        "yfinance",
    )
    assert plan.route_for(MainlandCapability.ADJUSTMENT_FACTORS) == (
        "baostock",
        "tushare",
        "akshare",
        "yfinance_derived",
    )
    assert plan.route_for(MainlandCapability.SUSPENSION_STATUS) == ("baostock",)
    assert plan.route_for(MainlandCapability.NAME_EVENTS) == (
        "tushare",
        "akshare",
    )
    assert plan.route_for(MainlandCapability.ISSUER_LIFECYCLE) == ("baostock",)
    assert plan.normalizer_version == "mainland-financial-normalizer-v1"
    assert plan.completeness_policy_version == "mainland-financial-completeness-v1"
    assert plan.account_scope_label == "personal-research-primary"
    assert plan.artifact_contract_version == "source-artifact-v1"
    assert plan.manifest_contract_version == "financial-manifest-v1"
    assert plan.selection_contract_version == "financial-period-selection-v1"
    assert plan.degradation_contract_version == "financial-degradation-v1"
    assert tuple(item.endpoint_id for item in plan.endpoint_pacing_identities) == (
        "adj_factor",
        "balancesheet",
        "cashflow",
        "fina_indicator",
        "income",
        "namechange",
    )
    assert tuple(item.capability for item in plan.request_budget_identities) == tuple(
        MainlandCapability
    )
    assert plan.plan_signature.startswith("mainland-routing-plan:v1:")


def test_self_signed_policy_invalid_plan_is_rejected_on_restore() -> None:
    result = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )
    assert result.plan is not None
    payload = result.plan.model_dump(mode="json")
    payload["routes"][-2]["providers"] = ["tushare"]
    _resign_plan_payload(payload)

    with pytest.raises(ValidationError, match="policy routes"):
        MainlandCapabilityRoutingPlan.model_validate(payload)


@pytest.mark.parametrize(
    ("overrides", "environment", "dependency_available", "expected_reason"),
    [
        (
            {"mainland_capability_routing_mode": "legacy"},
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.TUSHARE_REQUIRES_QUALIFIED_V1,
        ),
        (
            {"tushare_enabled_capabilities": ["suspension_status"]},
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.UNSUPPORTED_TUSHARE_CAPABILITY,
        ),
        (
            {"tushare_qualification_profile": "wrong-profile"},
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.QUALIFICATION_PROFILE_MISMATCH,
        ),
        (
            {},
            {},
            True,
            MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING,
        ),
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
            {"tushare_operator_safety_ceiling_calls_per_minute": 0},
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.TUSHARE_OPERATOR_SAFETY_CEILING_INVALID,
        ),
        (
            {
                "tushare_calls_per_minute": 40,
                "tushare_operator_safety_ceiling_calls_per_minute": 41,
            },
            {"TUSHARE_TOKEN": "fixture-secret"},
            True,
            MainlandCapabilityRoutingFailureReason.TUSHARE_OPERATOR_SAFETY_CEILING_INVALID,
        ),
    ],
)
def test_invalid_tushare_configuration_returns_a_typed_secret_safe_preflight(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    environment: dict[str, str],
    dependency_available: bool,
    expected_reason: MainlandCapabilityRoutingFailureReason,
) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if dependency_available else None,
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": ["statements"],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    config.update(overrides)

    result = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=config,
        environment=environment,
    )

    assert result.passed is False
    assert result.plan is None
    assert result.failure is not None
    assert result.failure.reason is expected_reason
    assert result.failure.analysis_outcome.reason is AnalysisOutcomeReason.PREFLIGHT_BLOCKED
    assert result.failure.analysis_outcome.diagnostic_codes == (
        AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID,
    )
    serialized = result.model_dump_json()
    assert "fixture-secret" not in serialized
    assert "token_digest" not in serialized.casefold()
    assert "raw_error" not in serialized.casefold()


@pytest.mark.parametrize(
    ("missing_key", "expected_reason"),
    [
        (
            "tushare_calls_per_minute",
            MainlandCapabilityRoutingFailureReason.TUSHARE_PACING_INVALID,
        ),
        (
            "tushare_operator_safety_ceiling_calls_per_minute",
            MainlandCapabilityRoutingFailureReason.TUSHARE_OPERATOR_SAFETY_CEILING_INVALID,
        ),
    ],
)
def test_enabled_tushare_requires_explicit_pacing_and_safety_configuration(
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
    expected_reason: MainlandCapabilityRoutingFailureReason,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    config = _qualified_config(enabled=["statements"])
    del config[missing_key]

    result = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-secret"},
    )

    assert result.failure is not None
    assert result.failure.reason is expected_reason


@pytest.mark.parametrize(
    ("label", "token"),
    (
        ("fixture-secret", "fixture-secret"),
        ("credential-primary", "different-fixture-token"),
    ),
)
def test_account_scope_label_cannot_persist_secret_material(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    token: str,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    config = _qualified_config(enabled=["statements"])
    config["tushare_account_scope_label"] = label

    result = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=config,
        environment={"TUSHARE_TOKEN": token},
    )

    assert result.plan is None
    assert result.failure is not None
    assert result.failure.reason is (
        MainlandCapabilityRoutingFailureReason.TUSHARE_ACCOUNT_SCOPE_INVALID
    )
    assert token not in result.model_dump_json()


def test_semantically_equal_configuration_and_different_tokens_share_a_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    first_config = _qualified_config(
        enabled=["name_events", "statements", "financial_indicators"]
    )
    second_config = dict(reversed(tuple(first_config.items())))
    second_config["tushare_enabled_capabilities"] = (
        "financial_indicators",
        "statements",
        "name_events",
    )

    first = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=first_config,
        environment={"TUSHARE_TOKEN": "first-fixture-secret"},
    )
    second = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=second_config,
        environment={"TUSHARE_TOKEN": "second-fixture-secret"},
    )

    assert first.plan is not None
    assert second.plan is not None
    assert first.plan == second.plan
    assert first.plan.plan_signature == second.plan.plan_signature


def test_material_routes_and_pacing_change_the_plan_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    baseline = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=_qualified_config(enabled=["statements"]),
        environment={"TUSHARE_TOKEN": "fixture-secret"},
    )
    changed_route = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=_qualified_config(enabled=["statements", "name_events"]),
        environment={"TUSHARE_TOKEN": "fixture-secret"},
    )
    changed_pacing_config = _qualified_config(enabled=["statements"])
    changed_pacing_config["tushare_calls_per_minute"] = 41
    changed_pacing = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=changed_pacing_config,
        environment={"TUSHARE_TOKEN": "fixture-secret"},
    )

    assert baseline.plan is not None
    assert changed_route.plan is not None
    assert changed_pacing.plan is not None
    assert len(
        {
            baseline.plan.plan_signature,
            changed_route.plan.plan_signature,
            changed_pacing.plan.plan_signature,
        }
    ) == 3


def test_plan_round_trips_and_environment_mutation_cannot_change_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    environment = {"TUSHARE_TOKEN": "fixture-secret"}
    result = preflight_mainland_capability_routing(
        _mainland_asset_configuration(),
        config=_qualified_config(enabled=["adjustment_factors"]),
        environment=environment,
    )
    assert result.plan is not None
    plan = result.plan

    environment["TUSHARE_TOKEN"] = "rotated-secret"
    restored = MainlandCapabilityRoutingPlan.model_validate_json(
        plan.model_dump_json()
    )

    assert restored == plan
    assert restored.plan_signature == plan.plan_signature
    serialized = json.dumps(restored.model_dump(mode="json"), sort_keys=True)
    for forbidden in (
        "fixture-secret",
        "rotated-secret",
        "token",
        "credential",
        "raw_error",
        "raw_payload",
    ):
        assert forbidden not in serialized.casefold()


def test_invalid_configuration_stops_graph_and_model_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    model_factory_calls = 0

    def model_factory(*args, **kwargs):
        nonlocal model_factory_calls
        model_factory_calls += 1
        raise AssertionError("model construction must not run")

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        model_factory,
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        TradingAgentsGraph(
            selected_analysts=("fundamentals",),
            config=_qualified_config(enabled=["statements"]),
            asset_configuration=_mainland_asset_configuration(),
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING
    )
    assert raised.value.failure.analysis_outcome.reason is (
        AnalysisOutcomeReason.PREFLIGHT_BLOCKED
    )
    assert model_factory_calls == 0


def test_qualified_programmatic_construction_requires_authoritative_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_factory_calls = 0

    def model_factory(*args, **kwargs):
        nonlocal model_factory_calls
        model_factory_calls += 1
        raise AssertionError("model construction must not run")

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        model_factory,
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        TradingAgentsGraph(
            selected_analysts=("market",),
            config=_qualified_config(enabled=["statements"]),
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.INSTRUMENT_NOT_MAINLAND_EQUITY
    )
    assert model_factory_calls == 0


def test_cli_configuration_failure_returns_an_outcome_without_graph_or_provider_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    selections = {
        "ticker": "601328.SS",
        "analysis_date": "2026-07-29",
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
    config = _qualified_config(enabled=["statements"])
    config["results_dir"] = str(tmp_path)
    graph_construction_calls = 0

    def graph_constructor(*args, **kwargs):
        nonlocal graph_construction_calls
        graph_construction_calls += 1
        raise AssertionError("graph construction must not run")

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)

    final_state = cli_main.run_analysis()

    assert graph_construction_calls == 0
    assert final_state["analysis_outcome_contract"]["reason"] == "preflight_blocked"
    assert final_state["capability_routing_failure"] == {
        "contract_version": "1.0",
        "reason": "tushare_token_missing",
        "diagnostic_code": "tushare_token_missing",
    }
    status_files = tuple(tmp_path.rglob("run_status.json"))
    assert len(status_files) == 1
    status = json.loads(status_files[0].read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert status["terminal_outcome_kind"] == "analysis_outcome"


def test_explicit_qualified_fund_cli_failure_completes_before_graph_construction(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selections = {
        "ticker": "510500.SS",
        "analysis_date": "2026-07-29",
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
    config = _qualified_config(enabled=())
    config["results_dir"] = str(tmp_path)
    graph_construction_calls = 0

    def graph_constructor(*args, **kwargs):
        nonlocal graph_construction_calls
        graph_construction_calls += 1
        raise AssertionError("graph construction must not run")

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)

    final_state = cli_main.run_analysis()

    assert graph_construction_calls == 0
    assert final_state["analysis_outcome_contract"]["reason"] == "preflight_blocked"
    assert final_state["capability_routing_failure"]["reason"] == (
        "instrument_not_mainland_equity"
    )


def test_default_mainland_market_only_cli_resolves_legacy_plan_before_graph(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoundaryReached(RuntimeError):
        pass

    selections = {
        "ticker": "601328.SS",
        "analysis_date": "2026-07-29",
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
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["results_dir"] = str(tmp_path)
    captured: dict[str, object] = {}

    def graph_constructor(*args, **kwargs):
        captured.update(kwargs)
        raise BoundaryReached

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_constructor)

    with pytest.raises(BoundaryReached):
        cli_main.run_analysis()

    asset_configuration = captured["asset_configuration"]
    plan = captured["capability_routing_plan"]
    assert asset_configuration.instrument_identity.symbol == "601328.SS"
    assert plan.mode is MainlandCapabilityRoutingMode.LEGACY
    assert plan.enabled_tushare_capabilities == ()


def test_resolved_plan_is_checkpointed_and_environment_changes_are_ignored(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    asset_configuration = _mainland_asset_configuration()
    config = _qualified_config(enabled=["statements"])
    preflight = preflight_mainland_capability_routing(
        asset_configuration,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-secret"},
    )
    assert preflight.plan is not None
    plan = preflight.plan
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    llm = MagicMock()
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **kwargs: SimpleNamespace(get_llm=lambda: llm),
    )

    graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=config,
        asset_configuration=asset_configuration,
        capability_routing_plan=plan,
    )
    state = graph.create_initial_state(
        "601328.SS",
        "2026-07-29",
        evidence_state=EvidenceState(
            instrument_identity=asset_configuration.instrument_identity,
        ),
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("routing_plan_checkpoint", lambda _state: {})
    workflow.set_entry_point("routing_plan_checkpoint")
    workflow.add_edge("routing_plan_checkpoint", END)
    checkpoint_config = {"configurable": {"thread_id": "routing-plan-roundtrip"}}
    with get_checkpointer(str(tmp_path), "601328.SS") as saver:
        workflow.compile(checkpointer=saver).invoke(state, config=checkpoint_config)
    with get_checkpointer(str(tmp_path), "601328.SS") as saver:
        saved = saver.get_tuple(checkpoint_config)
        restored_payload = saved.checkpoint["channel_values"][
            "capability_routing_plan"
        ]
    restored = MainlandCapabilityRoutingPlan.model_validate(restored_payload)

    assert restored == plan
    assert plan.plan_signature in graph._run_signature("stock")


def test_audit_projection_round_trips_the_safe_plan(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    asset_configuration = _mainland_asset_configuration()
    config = _qualified_config(enabled=["financial_indicators"])
    preflight = preflight_mainland_capability_routing(
        asset_configuration,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-secret"},
    )
    assert preflight.plan is not None
    plan = preflight.plan
    identity_source_ref = stable_acquisition_source_ref(
        "identity-registry",
        asset_configuration.registry_source_ref,
        asset_configuration.registry_digest,
    )
    identity_artifact = SourceArtifact(
        artifact_sha256=asset_configuration.registry_digest,
        source_ref=identity_source_ref,
        tool_call_id="identity-registry",
        tool_name="instrument_identity_registry",
        raw_text=asset_configuration.registry_artifact,
    )
    evidence = EvidenceState(
        instrument_identity=asset_configuration.instrument_identity,
        source_artifacts=(identity_artifact,),
        acquisition_outcomes=(
            SourceAcquisitionAvailable(
                provider="instrument-identity-registry",
                capability="instrument_identity",
                source_ref=identity_source_ref,
                attempt=1,
                retrieved_at=(
                    asset_configuration.instrument_identity.provenance.retrieved_at
                ),
                artifact=identity_artifact,
            ),
        ),
    )
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
        diagnostic_codes=(AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE,),
    )
    state = {
        "company_of_interest": "601328.SS",
        "trade_date": "2026-07-29",
        "asset_type": "stock",
        "graph_signature": "|".join(
            (
                "asset_configuration="
                + asset_configuration.asset_configuration_signature,
                "capability_routing=" + plan.plan_signature,
            )
        ),
        "evidence_gate_mode": "enforce",
        "asset_configuration": asset_configuration.model_dump(mode="json"),
        "capability_routing_plan": plan.model_dump(mode="json"),
        "evidence_state": evidence.model_dump(mode="json"),
        "evidence_preflight": {
            "passed": False,
            "readiness": "insufficient",
            "blockers": ["authoritative market snapshot is missing"],
            "diagnostic_codes": ["snapshot_unavailable"],
        },
        "analysis_outcome_contract": outcome.model_dump(mode="json"),
        "analysis_outcome": render_analysis_outcome(outcome),
        "decision_audit_created_at": "2026-07-29T12:00:00+00:00",
    }

    payload = prepare_decision_audit(state, tmp_path, config=config)

    restored = MainlandCapabilityRoutingPlan.model_validate(
        payload["capability_routing_plan"]
    )
    assert restored == plan
    serialized = json.dumps(payload, sort_keys=True)
    assert "fixture-secret" not in serialized
    assert "token_digest" not in serialized
    assert "raw_error" not in serialized
    assert "raw_payload" not in serialized

    unsafe_state = dict(state)
    unsafe_state["capability_routing_plan"] = None
    unsafe_state["capability_routing_failure"] = {
        "contract_version": "1.0",
        "reason": "raw provider error token=fixture-secret",
        "diagnostic_code": "traceback credentials",
    }
    with pytest.raises(ValidationError):
        prepare_decision_audit(unsafe_state, tmp_path / "unsafe", config=config)

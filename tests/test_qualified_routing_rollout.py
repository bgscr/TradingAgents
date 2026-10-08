from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph

import cli.main as cli_main
import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.interface as vendor_interface
import tradingagents.qualified_routing_composition as composition_module
from tests.test_financial_capability_routing_selection import (
    _ANNUAL_PERIODS,
    _REPORTING_PERIODS,
    _candidate,
    _historical_plan,
)
from tests.test_financial_graph_dispatch import (
    _analysis_outcome_state,
    _financial_vendor_methods,
)
from tests.test_financial_manifest_evidence_reporting import _attempt_event
from tests.test_market_session_calendar_publication import _acquisition
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_COUNT_MAX,
    MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_RETENTION_BOUND,
    MainlandCapability,
    MainlandCapabilityRoutingCheckpointAction,
    MainlandCapabilityRoutingCheckpointError,
    MainlandCapabilityRoutingCheckpointFailureReason,
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingFailureReason,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
    MainlandCapabilityRoutingPreflight,
    MainlandCapabilityRoutingPreflightError,
    MainlandCapabilityRoutingRunProjection,
    MainlandCapabilityRoutingShadowFailure,
    TushareCapability,
    preflight_mainland_capability_routing,
    prepare_mainland_temporal_capability_routing_plan,
    start_new_mainland_capability_routing_run,
    validate_mainland_capability_routing_checkpoint,
)
from tradingagents.dataflows.baostock_calendar import MainlandSessionCalendarMembership
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
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialToolDispatcher,
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
from tradingagents.market_history import (
    MarketHistoryConfig,
    MarketHistoryStore,
    publication_from_baostock_session_calendar_acquisition,
)
from tradingagents.market_history.config import provider_request_authority_config
from tradingagents.qualified_routing_composition import (
    ProductionQualifiedEligibilityCalendarReader,
    RegisteredProductionQualifiedRoutingComposition,
)
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


def _publish_qualified_validation_calendar(config: dict[str, object]) -> str:
    membership = tuple(
        MainlandSessionCalendarMembership(
            calendar_date=date(2026, 8, day),
            is_trading_day=day not in {8, 9, 11},
        )
        for day in range(7, 15)
    )
    acquisition = _acquisition(
        requested_start_date=membership[0].calendar_date,
        requested_end_date=membership[-1].calendar_date,
        membership=membership,
    )
    history_config = MarketHistoryConfig.from_mapping(config)
    with MarketHistoryStore.open(history_config) as primary:
        published = primary.publish_session_calendar(
            publication_from_baostock_session_calendar_acquisition(acquisition)
        )
        readiness = primary.assess_mainland_session_calendar_readiness(
            published.calendar_revision_id,
            requested_start_date=membership[0].calendar_date,
            requested_end_date=membership[-1].calendar_date,
            evaluated_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
        )
    assert readiness.latest_completed_session == date(2026, 8, 12)
    return published.calendar_revision_id


def _preflight(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    enabled: object = ("statements",),
):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    resolved_mode = "qualified_v1_shadow" if mode == "qualified_v1" else mode
    result = preflight_mainland_capability_routing(
        _asset(),
        config=_rollout_config(resolved_mode, enabled=enabled),
        environment={"TUSHARE_TOKEN": "deterministic-fixture-secret"},
    )
    if mode != "qualified_v1" or result.plan is None:
        return result
    historical_payload = result.plan.model_dump(mode="json")
    historical_payload["mode"] = "qualified_v1"
    historical_payload["manifest_contract_version"] = "financial-manifest-v1"
    historical_payload.pop("eligibility_policy_version")
    historical_payload.pop("financial_eligibility_policy")
    historical_payload.pop("financial_eligibility_binding")
    _resign_plan_payload(historical_payload)
    return MainlandCapabilityRoutingPreflight(
        passed=True,
        plan=MainlandCapabilityRoutingPlan.model_validate(historical_payload),
    )


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


def test_rollout_fixture_resolves_identity_with_dotenv_disabled(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "RUN_LIVE_CUSTOM_TESTS": "0",
            "RUN_LIVE_DEEPSEEK_TESTS": "0",
            "RUN_LIVE_ENVIRONMENT": "0",
            "RUN_LIVE_PROVIDER_TESTS": "0",
            "RUN_LIVE_TUSHARE_TESTS": "0",
        }
    )
    environment.pop("TRADINGAGENTS_IDENTITY_REGISTRY_PATH", None)
    environment.pop("TRADINGAGENTS_IDENTITY_REGISTRY_SHA256", None)
    for name in tuple(environment):
        if any(
            marker in name.upper()
            for marker in (
                "API_KEY",
                "TOKEN",
                "PASSWORD",
                "SECRET",
                "CREDENTIAL",
                "COOKIE",
            )
        ):
            environment.pop(name)

    probe = subprocess.run(
        (
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import copy",
                    "import sys",
                    "from pathlib import Path",
                    "import tests.conftest as fixtures",
                    "from tradingagents.asset_configuration import "
                    "resolve_run_asset_configuration",
                    "config = fixtures._isolated_runtime_config(Path(sys.argv[1]))",
                    "resolve_run_asset_configuration('601328.SS', "
                    "config=copy.deepcopy(config))",
                )
            ),
            str(tmp_path / "isolated-runtime"),
        ),
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr


def test_default_rollout_remains_byte_compatible_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "false")

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
    assert result.plan.eligibility_policy_version == (
        "mainland-financial-period-eligibility-v2"
    )
    assert result.plan.financial_eligibility_policy is not None
    assert projection.eligibility_policy_version == (
        result.plan.eligibility_policy_version
    )
    assert projection.financial_eligibility_policy == (
        result.plan.financial_eligibility_policy
    )
    assert projection.financial_eligibility_policy.authority_disposition == (
        "shadow_non_authoritative"
    )


def test_shadow_plan_signature_binds_every_v2_policy_and_projection_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1_shadow", enabled=()).plan
    assert plan is not None
    assert plan.financial_eligibility_policy is not None
    policy = plan.financial_eligibility_policy

    assert policy.eligibility_policy_version == plan.eligibility_policy_version
    assert policy.reporting_calendar.calendar_version == (
        "mainland-financial-reporting-calendar-v2"
    )
    assert policy.completeness_policy_version == (
        "mainland-financial-completeness-v2"
    )
    assert policy.candidate_selection_contract_version == (
        "mainland-financial-candidate-selection-v2"
    )
    assert policy.manifest_projection_version == "financial-manifest-v2"
    assert plan.manifest_contract_version == policy.manifest_projection_version
    assert policy.checkpoint_projection_version == (
        "mainland-financial-checkpoint-projection-v2"
    )
    assert policy.audit_projection_version == (
        "mainland-financial-audit-projection-v2"
    )
    assert policy.report_projection_version == (
        "mainland-financial-report-projection-v2"
    )
    assert policy.authority_disposition == "shadow_non_authoritative"
    assert policy.qualified_v1_authority_enabled is False
    assert plan.financial_eligibility_policy.model_dump(mode="json") == (
        plan.signature_payload()["financial_eligibility_policy"]
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("eligibility_policy_version", "mainland-financial-period-eligibility-v3"),
        ("reporting_calendar", {"calendar_version": "changed"}),
        ("completeness_policy_version", "mainland-financial-completeness-v3"),
        ("candidate_selection_contract_version", "financial-candidate-selection-v3"),
        ("manifest_projection_version", "financial-manifest-v3"),
        ("checkpoint_projection_version", "financial-checkpoint-projection-v3"),
        ("audit_projection_version", "financial-audit-projection-v3"),
        ("report_projection_version", "financial-report-projection-v3"),
        ("authority_disposition", "qualified_authoritative"),
    ),
)
def test_each_v2_policy_binding_is_material_to_the_plan_signature(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    plan = _preflight(monkeypatch, "qualified_v1_shadow", enabled=()).plan
    assert plan is not None
    payload = plan.model_dump(mode="json")
    payload["financial_eligibility_policy"][field] = replacement

    _resign_plan_payload(payload)

    assert payload["plan_signature"] != plan.plan_signature


def test_authoritative_qualified_v1_preflight_remains_disabled_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "false")
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    provider_calls = 0
    model_calls = 0

    result = preflight_mainland_capability_routing(
        _asset(),
        config=_rollout_config("qualified_v1", enabled=()),
        environment={},
    )

    assert result.plan is None
    assert result.failure is not None
    assert result.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_V1_AUTHORITY_DISABLED
    )
    assert provider_calls == 0
    assert model_calls == 0


def test_programmatic_qualified_v1_graph_injection_is_disabled_before_model_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical = _preflight(monkeypatch, "qualified_v1", enabled=()).plan
    assert historical is not None
    model_calls = 0

    def model_factory(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("model construction must remain unreachable")

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        model_factory,
    )

    with pytest.raises(MainlandCapabilityRoutingPreflightError) as caught:
        TradingAgentsGraph(
            selected_analysts=("fundamentals",),
            config=_graph_config(tmp_path, historical),
            asset_configuration=_asset(),
            capability_routing_plan=historical,
            qualified_financial_routing=_composition(historical),
        )

    assert caught.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_V1_AUTHORITY_DISABLED
    )
    assert model_calls == 0


def test_pre_v2_qualified_checkpoint_cannot_resume_as_v2_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _preflight(monkeypatch, "qualified_v1_shadow", enabled=()).plan
    assert active is not None
    historical_payload = active.model_dump(mode="json")
    historical_payload["mode"] = "qualified_v1"
    historical_payload["manifest_contract_version"] = "financial-manifest-v1"
    historical_payload.pop("eligibility_policy_version")
    historical_payload.pop("financial_eligibility_policy")
    historical_payload.pop("financial_eligibility_binding")
    _resign_plan_payload(historical_payload)
    historical = MainlandCapabilityRoutingPlan.model_validate(historical_payload)

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        validate_mainland_capability_routing_checkpoint(
            active_plan=active,
            checkpoint_plan=historical.model_dump(mode="json"),
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
    )
    assert caught.value.diagnostic_code == "financial_eligibility_policy_mismatch"


@pytest.mark.parametrize("contradiction", ["plan", "policy"])
def test_contradictory_plan_and_policy_identities_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
    contradiction: str,
) -> None:
    active = _preflight(monkeypatch, "qualified_v1_shadow", enabled=()).plan
    assert active is not None
    checkpoint = active.model_dump(mode="json")
    if contradiction == "plan":
        checkpoint["eligibility_policy_version"] = (
            "mainland-financial-period-eligibility-v3"
        )
    else:
        checkpoint["financial_eligibility_policy"]["eligibility_policy_version"] = (
            "mainland-financial-period-eligibility-v3"
        )

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        validate_mainland_capability_routing_checkpoint(
            active_plan=active,
            checkpoint_plan=checkpoint,
        )

    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
    )


def test_legacy_plan_projection_remains_without_synthetic_v2_fields() -> None:
    result = preflight_mainland_capability_routing(
        _asset(),
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )
    assert result.plan is not None
    serialized = result.plan.model_dump(mode="json")

    assert result.plan.plan_signature == _LEGACY_PLAN_SIGNATURE
    assert "eligibility_policy_version" not in serialized
    assert "financial_eligibility_policy" not in serialized


@pytest.mark.parametrize("mode", ["qualified_v1_shadow"])
def test_empty_enablement_activates_no_ordinary_tushare_provider_route(
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
            if capability
            not in {
                MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE,
                MainlandCapability.LISTING_PUBLICATION_EVIDENCE,
            }
            for provider in result.plan.route_for(capability)
    }
    assert result.plan.route_for(
        MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
    ) == ("tushare", "akshare_sina", "yfinance")


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


@pytest.mark.parametrize("symbol", ("600895.SS", "601328.SS", "510500.SS"))
def test_checkpointed_asset_identity_opaques_query_bearing_provenance(
    symbol: str,
) -> None:
    asset = resolve_run_asset_configuration(
        symbol,
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    state = Propagator().create_initial_state(
        symbol,
        "2026-07-30",
        asset_configuration=asset,
    )

    assert "?" in asset.registry_artifact
    checkpoint_text = json.dumps(state, sort_keys=True)
    checkpoint_source_ref = state["asset_configuration"]["instrument_identity"][
        "provenance"
    ]["source_ref"]
    assert checkpoint_source_ref.startswith("acq.v1:identity.provenance:")
    assert "?" not in checkpoint_source_ref
    assert "?" not in checkpoint_text


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


def test_qualified_graph_is_disabled_before_model_work(
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
    assert caught.value.failure.reason.value == "qualified_v1_authority_disabled"


def test_contradictory_composition_fails_typed_before_model_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_plan = _preflight(
        monkeypatch,
        "qualified_v1_shadow",
        enabled=("statements",),
    ).plan
    contradictory_plan = _preflight(
        monkeypatch,
        "qualified_v1_shadow",
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


def test_cli_passes_explicit_shadow_composition_to_graph(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setenv("TUSHARE_TOKEN", "deterministic-fixture-secret")
    config = _rollout_config("qualified_v1_shadow")
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


def test_cli_resolves_shadow_composition_before_graph_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setenv("TUSHARE_TOKEN", "deterministic-fixture-secret")
    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "false")
    config = _rollout_config("qualified_v1_shadow")
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
        assert validation_workload is True
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
        cli_main.run_analysis(qualified_routing_validation=True)

    assert resolved is not None
    assert received is resolved


def test_live_calendar_reader_preflight_rejects_sidecar_drift_and_accepts_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setenv("TUSHARE_TOKEN", "deterministic-fixture-secret")
    config = _rollout_config("qualified_v1_shadow")
    config.update(
        {
            "data_cache_dir": str(tmp_path / "cache"),
            "market_history_database_path": str(tmp_path / "history.sqlite3"),
            "market_history_payload_root": str(tmp_path / "payloads"),
            "market_history_backup_root": str(tmp_path / "backups"),
            "results_dir": str(tmp_path / "results"),
        }
    )
    expected_revision_id = _publish_qualified_validation_calendar(config)
    history_config = MarketHistoryConfig.from_mapping(config)
    sidecar_config = provider_request_authority_config(history_config)
    with MarketHistoryStore.open_provider_request_authority(history_config) as sidecar:
        assert sidecar._connection.execute(
            "SELECT COUNT(*) FROM market_session_calendars"
        ).fetchone() == (0,)
        assert sidecar._connection.execute(
            "SELECT COUNT(*) FROM market_sessions"
        ).fetchone() == (0,)

    asset = resolve_run_asset_configuration("601328.SS", config=config)
    routing_preflight = preflight_mainland_capability_routing(asset, config=config)
    assert routing_preflight.plan is not None
    plan = prepare_mainland_temporal_capability_routing_plan(routing_preflight.plan)
    config["asset_configuration_signature"] = asset.asset_configuration_signature
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    monkeypatch.setattr(
        cli_main,
        "production_qualified_routing_dependency_availability",
        lambda: dict.fromkeys(("akshare", "baostock", "tushare", "yfinance"), True),
    )
    production_factories = (
        composition_module.production_qualified_routing_runtime_factories()
    )

    def build(runtime_factories):
        monkeypatch.setattr(
            composition_module,
            "production_qualified_routing_runtime_factories",
            lambda: runtime_factories,
        )
        built = cli_main.build_qualified_financial_routing_composition(
            plan=plan,
            asset_configuration=asset,
            config=config,
            validation_workload=True,
        )
        assert isinstance(built, RegisteredProductionQualifiedRoutingComposition)
        return built

    drifted = build(
        replace(
            production_factories,
            open_eligibility_calendar_reader=lambda _blueprint: (
                ProductionQualifiedEligibilityCalendarReader(sidecar_config)
            ),
        )
    )
    with pytest.raises(
        RuntimeError,
        match="qualified_routing_calendar_reader_not_primary",
    ):
        composition_module.preflight_production_qualified_calendar_reader(
            drifted,
            authorized_calendar_revision_id=expected_revision_id,
            requested_session=date(2026, 8, 12),
            evaluated_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
        )

    corrected = build(production_factories)
    receipt = composition_module.preflight_production_qualified_calendar_reader(
        corrected,
        authorized_calendar_revision_id=expected_revision_id,
        requested_session=date(2026, 8, 12),
        evaluated_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
    )

    assert receipt.calendar_revision_id == expected_revision_id
    assert receipt.latest_completed_session == date(2026, 8, 12)
    assert receipt.reader_uses_primary_database is True
    assert receipt.exact_revision_verified is True
    with pytest.raises(
        composition_module.ProductionQualifiedCalendarPreflightError,
        match="qualified_routing_calendar_exact_revision_mismatch",
    ):
        composition_module.preflight_production_qualified_calendar_reader(
            corrected,
            authorized_calendar_revision_id=expected_revision_id,
            requested_session=date(2026, 8, 12),
            evaluated_at=datetime(2026, 8, 12, 8, 0, 1, tzinfo=timezone.utc),
        )

    selections = {
        "ticker": "601328.SS",
        "analysis_date": "2026-08-12",
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
    run_allocations = 0
    graph_constructions = 0

    def forbidden_run_allocation(*_args, **_kwargs):
        nonlocal run_allocations
        run_allocations += 1
        pytest.fail("live-reader drift reached run allocation")

    def forbidden_graph_construction(*_args, **_kwargs):
        nonlocal graph_constructions
        graph_constructions += 1
        pytest.fail("live-reader drift reached provider/model construction")

    monkeypatch.setenv("TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED", "false")
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(
        cli_main,
        "preflight_production_qualified_calendar_reader",
        lambda composition, *, authorized_calendar_revision_id, requested_session, evaluated_at: (
            composition_module.preflight_production_qualified_calendar_reader(
                composition,
                authorized_calendar_revision_id=authorized_calendar_revision_id,
                requested_session=requested_session,
                evaluated_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
            )
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "build_qualified_financial_routing_composition",
        lambda **_kwargs: drifted,
    )
    monkeypatch.setattr(cli_main, "_prepare_run_artifacts", forbidden_run_allocation)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", forbidden_graph_construction)

    with pytest.raises(
        composition_module.ProductionQualifiedCalendarPreflightError,
        match="qualified_routing_calendar_reader_not_primary",
    ):
        cli_main.run_analysis(
            qualified_routing_validation=True,
            qualified_routing_calendar_revision_id=expected_revision_id,
        )

    assert run_allocations == 0
    assert graph_constructions == 0
    with MarketHistoryStore.open(sidecar_config) as sidecar:
        assert sidecar._connection.execute(
            "SELECT COUNT(*) FROM market_session_calendars"
        ).fetchone() == (0,)
        assert sidecar._connection.execute(
            "SELECT COUNT(*) FROM market_sessions"
        ).fetchone() == (0,)
    corrected.close()


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
    calendar_revision_id = "market-session-calendar=sha256:" + "a" * 64

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
            qualified_routing_calendar_revision_id=calendar_revision_id,
        )
    else:
        command(
            checkpoint=False,
            clear_checkpoints=False,
            qualified_routing_validation=True,
            qualified_routing_composition_factory="operator_fixture:build",
            qualified_routing_calendar_revision_id=calendar_revision_id,
        )

    assert received == {
        "checkpoint": False,
        "qualified_routing_validation": True,
        "qualified_routing_composition_factory": "operator_fixture:build",
        "qualified_routing_calendar_revision_id": calendar_revision_id,
    }


def test_capability_only_composition_needs_no_financial_request_factory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _preflight(
        monkeypatch,
        "qualified_v1_shadow",
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


def test_shadow_rollback_changes_new_runs_immediately_and_authority_stays_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualified = _preflight(monkeypatch, "qualified_v1_shadow").plan
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
        MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
    )

    reenable = preflight_mainland_capability_routing(
        _asset(),
        config=_rollout_config("qualified_v1"),
        environment={},
    )
    assert reenable.plan is None
    assert reenable.failure is not None
    assert reenable.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_V1_AUTHORITY_DISABLED
    )


def test_rollback_preserves_qualified_historical_state_byte_for_byte(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualified = _preflight(monkeypatch, "qualified_v1_shadow").plan
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
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
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
    wrong_contract_versions = dict(
        projection,
        normalizer_version="mainland-financial-normalizer-v1",
        completeness_policy_version="mainland-financial-completeness-v1",
        selection_contract_version="financial-period-selection-v1",
    )

    with pytest.raises(ValueError):
        MainlandCapabilityRoutingRunProjection.model_validate(wrong_profile)
    with pytest.raises(ValueError):
        MainlandCapabilityRoutingRunProjection.model_validate(
            duplicate_enablement
        )
    with pytest.raises(ValueError, match="contract versions"):
        MainlandCapabilityRoutingRunProjection.model_validate(
            wrong_contract_versions
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


def test_shadow_failure_v21_distinguishes_candidate_stages_and_reads_v1() -> None:
    historical_v1 = {
        "contract_version": "1.0",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
    }
    candidate_stages = (
        "context_result",
        "context_exception",
        "dispatcher_construction",
    )

    historical_candidate_projections = tuple(
        MainlandCapabilityRoutingShadowFailure.model_validate(
            copy.deepcopy(historical_v1)
        ).model_dump(mode="json")
        for _stage in candidate_stages
    )

    assert historical_candidate_projections == (
        historical_v1,
        historical_v1,
        historical_v1,
    )

    current_entries = (
        {
            "stage": "context_result",
            "failure_kind": "typed_result",
            "typed_acquisition_reason": "temporal_eligibility_rejected",
            "temporal_rejection_reasons": ["ann_date_missing"],
            "selection_kind": "indicator",
            "context_constructed": False,
            "dispatcher_constructed": False,
        },
        {
            "stage": "context_exception",
            "failure_kind": "exception",
            "exception_family": "builtins",
            "exception_class": "RuntimeError",
            "selection_kind": "indicator",
            "context_constructed": False,
            "dispatcher_constructed": False,
        },
        {
            "stage": "dispatcher_construction",
            "failure_kind": "exception",
            "exception_family": "financial_dispatch",
            "exception_class": "FinancialDispatchCheckpointError",
            "typed_reason": "financial_dispatch_checkpoint_malformed",
            "selection_kind": "indicator",
            "context_constructed": True,
            "dispatcher_constructed": False,
        },
    )
    current_payloads = tuple(
        {
            "contract_version": "2.1",
            "diagnostic_code": "shadow_dispatch_failed",
            "failure_count": 1,
            "retained_failure_count": 1,
            "dropped_failure_count": 0,
            "failures": [entry],
        }
        for entry in current_entries
    )
    restored_v21 = tuple(
        MainlandCapabilityRoutingShadowFailure.model_validate(
            json.loads(json.dumps(payload, sort_keys=True))
        ).model_dump(mode="json")
        for payload in current_payloads
    )

    assert tuple(
        payload["failures"][0]["stage"] for payload in restored_v21
    ) == candidate_stages
    assert restored_v21 == current_payloads

    compatibility_v20 = {
        "contract_version": "2.0",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
        **current_entries[1],
    }
    assert MainlandCapabilityRoutingShadowFailure.model_validate(
        compatibility_v20
    ).model_dump(mode="json") == compatibility_v20


def test_shadow_failure_updates_and_total_count_are_strictly_validated() -> None:
    projection = MainlandCapabilityRoutingShadowFailure.model_validate(
        {
            "contract_version": "2.1",
            "diagnostic_code": "shadow_dispatch_failed",
            "failure_count": 1,
            "retained_failure_count": 1,
            "dropped_failure_count": 0,
            "failures": [
                {
                    "stage": "context_exception",
                    "failure_kind": "exception",
                    "exception_family": "builtins",
                    "exception_class": "RuntimeError",
                    "selection_kind": "statement",
                    "context_constructed": False,
                    "dispatcher_constructed": False,
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="accounting is inconsistent"):
        projection.model_copy(update={"failure_count": 2})
    with pytest.raises(ValueError):
        projection.model_copy(update={"failures": ()})
    with pytest.raises(ValueError):
        MainlandCapabilityRoutingShadowFailure.model_validate(
            {
                **projection.model_dump(mode="json"),
                "failure_count": (
                    MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_COUNT_MAX + 1
                ),
            }
        )


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
    qualified = _preflight(monkeypatch, "qualified_v1_shadow").plan
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


def test_graph_starts_distinct_v2_run_for_pre_v2_qualified_checkpoint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    historical = _preflight(monkeypatch, "qualified_v1").plan
    active = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert historical is not None
    assert active is not None
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-07-30"
    graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, active),
        asset_configuration=asset,
        capability_routing_plan=active,
        qualified_financial_routing=_composition(active, asset),
    )
    old_run_id = "run:" + "a" * 64
    historical_state = graph.create_initial_state(
        ticker,
        trade_date,
        evidence_state=EvidenceState(
            instrument_identity=asset.instrument_identity
        ),
        run_id=old_run_id,
    )
    historical_state["capability_routing_plan"] = historical.model_dump(
        mode="json"
    )
    historical_state["capability_routing_rollout"] = (
        MainlandCapabilityRoutingRunProjection.from_plan(historical).model_dump(
            mode="json"
        )
    )
    historical_state.pop("financial_eligibility_binding", None)
    historical_state["evidence_state"].pop(
        "financial_eligibility_binding",
        None,
    )
    active_signature = graph._run_signature("stock")
    historical_signature = active_signature.replace(
        active.plan_signature,
        historical.plan_signature,
    )
    old_config = {
        "configurable": {
            "thread_id": thread_id(
                ticker,
                trade_date,
                historical_signature,
            )
        }
    }
    workflow = StateGraph(AgentState)
    workflow.add_node("checkpoint", lambda _state: {})
    workflow.set_entry_point("checkpoint")
    workflow.add_edge("checkpoint", END)
    with get_checkpointer(graph.config["data_cache_dir"], ticker) as saver:
        workflow.compile(checkpointer=saver).invoke(
            historical_state,
            config=old_config,
        )
        before = saver.get_tuple(old_config).checkpoint

    with graph.checkpoint_scope(ticker, trade_date) as session:
        compatibility = session.routing_compatibility
        assert session.resume_from_checkpoint is False
        assert compatibility is not None
        assert compatibility.action is (
            MainlandCapabilityRoutingCheckpointAction.START_NEW
        )
        assert compatibility.reason is (
            MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
        )
        assert compatibility.previous_run_id == old_run_id
        assert compatibility.new_run_id is not None
        assert compatibility.new_run_id != old_run_id
        new_state = graph.create_initial_state(
            ticker,
            trade_date,
            evidence_state=EvidenceState(
                instrument_identity=asset.instrument_identity
            ),
            checkpoint_thread_id=session.graph_config["configurable"][
                "thread_id"
            ],
        )
        assert new_state["run_id"] == compatibility.new_run_id
        assert new_state["capability_routing_plan"] == active.model_dump(
            mode="json"
        )

    with get_checkpointer(graph.config["data_cache_dir"], ticker) as saver:
        assert saver.get_tuple(old_config).checkpoint == before


def test_graph_starts_distinct_temporal_run_for_plan_1_2_checkpoint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: SimpleNamespace(get_llm=lambda: object()),
    )
    asset = _asset()
    historical = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert historical is not None
    active = prepare_mainland_temporal_capability_routing_plan(historical)
    ticker = asset.instrument_identity.symbol
    trade_date = "2026-08-10"
    graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=_graph_config(tmp_path, active),
        asset_configuration=asset,
        capability_routing_plan=active,
        qualified_financial_routing=_composition(active, asset),
    )
    old_run_id = "run:" + "c" * 64
    historical_state = graph.create_initial_state(
        ticker,
        trade_date,
        evidence_state=EvidenceState(
            instrument_identity=asset.instrument_identity
        ),
        run_id=old_run_id,
    )
    historical_state["capability_routing_plan"] = historical.model_dump(
        mode="json"
    )
    historical_state["capability_routing_rollout"] = (
        MainlandCapabilityRoutingRunProjection.from_plan(historical).model_dump(
            mode="json"
        )
    )
    historical_signature = graph._run_signature("stock").replace(
        active.plan_signature,
        historical.plan_signature,
    )
    old_config = {
        "configurable": {
            "thread_id": thread_id(
                ticker,
                trade_date,
                historical_signature,
            )
        }
    }
    workflow = StateGraph(AgentState)
    workflow.add_node("checkpoint", lambda _state: {})
    workflow.set_entry_point("checkpoint")
    workflow.add_edge("checkpoint", END)
    with get_checkpointer(graph.config["data_cache_dir"], ticker) as saver:
        workflow.compile(checkpointer=saver).invoke(
            historical_state,
            config=old_config,
        )
        before = saver.get_tuple(old_config).checkpoint

    with graph.checkpoint_scope(ticker, trade_date) as session:
        compatibility = session.routing_compatibility
        assert session.resume_from_checkpoint is False
        assert compatibility is not None
        assert compatibility.action is (
            MainlandCapabilityRoutingCheckpointAction.START_NEW
        )
        assert compatibility.reason is (
            MainlandCapabilityRoutingCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
        )
        assert compatibility.previous_run_id == old_run_id
        assert compatibility.new_run_id is not None
        assert compatibility.new_run_id != old_run_id
        new_state = graph.create_initial_state(
            ticker,
            trade_date,
            evidence_state=EvidenceState(
                instrument_identity=asset.instrument_identity
            ),
            checkpoint_thread_id=session.graph_config["configurable"][
                "thread_id"
            ],
        )
        assert new_state["run_id"] == compatibility.new_run_id
        assert new_state["capability_routing_plan"] == active.model_dump(
            mode="json"
        )
        assert "financial_eligibility_context" not in new_state

    with get_checkpointer(graph.config["data_cache_dir"], ticker) as saver:
        assert saver.get_tuple(old_config).checkpoint == before


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
    qualified = _preflight(monkeypatch, "qualified_v1_shadow").plan
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
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
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
            "qualified_v1_shadow",
            "qualified_v1_shadow",
            MainlandCapabilityRoutingCheckpointFailureReason.PLAN_MISMATCH,
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
        "qualified_v1_shadow",
        enabled=("statements",),
    ).plan
    mismatch = _preflight(
        monkeypatch,
        "qualified_v1_shadow",
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
        "qualified_v1_shadow",
        enabled=("statements",),
    ).plan
    mismatch = _preflight(
        monkeypatch,
        "qualified_v1_shadow",
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
        "qualified_v1_shadow",
        enabled=("statements",),
    ).plan
    mismatch = _preflight(
        monkeypatch,
        "qualified_v1_shadow",
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
def test_v2_shadow_and_pre_v2_qualified_checkpoint_states_cannot_be_confused(
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
        MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
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
        contract_version="financial-statement-routing-request-v2",
        eligibility_policy_version="mainland-financial-period-eligibility-v2",
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
    binding = plan.financial_eligibility_binding
    evidence = EvidenceState(
        financial_eligibility_binding=binding,
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
    )
    state = {
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
        "evidence_state": evidence.model_dump(mode="json"),
    }
    if binding is not None:
        state["financial_eligibility_binding"] = binding.model_dump(mode="json")
    return state


def _authoritative_financial_result_bytes(result: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "messages": [
                message.model_dump(mode="json") for message in result["messages"]
            ],
            "evidence_state": result["evidence_state"],
            "financial_dispatch_ledger": result["financial_dispatch_ledger"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _single_localized_shadow_failure(entry: dict[str, object]) -> dict[str, object]:
    return {
        "contract_version": "2.1",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
        "retained_failure_count": 1,
        "dropped_failure_count": 0,
        "failures": [entry],
    }


def _assert_shadow_authority(plan: MainlandCapabilityRoutingPlan) -> None:
    assert plan.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
    assert plan.financial_eligibility_policy is not None
    assert plan.financial_eligibility_policy.authority_disposition == (
        "shadow_non_authoritative"
    )
    assert plan.financial_eligibility_policy.qualified_v1_authority_enabled is False


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
    _assert_shadow_authority(plan)
    legacy_calls = 0
    shadow_calls = 0
    fixed_clock = lambda: datetime(2026, 7, 30, 12, tzinfo=timezone.utc)  # noqa: E731

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

    def failing_source(_request):
        nonlocal shadow_calls
        shadow_calls += 1
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

    class QualifiedCache:
        def checkpoint(self) -> dict[str, object]:
            return {
                "contract_version": "1.0",
                "run_scope_id": "run:" + "b" * 64,
                "entries": [],
            }

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
        qualified_provider_subrequest_cache=QualifiedCache(),
        clock=fixed_clock,
    )

    state = _financial_tool_state(asset, plan)
    baseline_config = dict(config)
    baseline_config["mainland_capability_routing_mode"] = "legacy"
    baseline_config.pop("mainland_capability_routing_plan_signature", None)
    baseline = FinancialDispatchToolNode(
        config=baseline_config,
        vendor_methods=vendor_methods,
        clock=fixed_clock,
    )(state)
    assert legacy_calls == 1
    legacy_calls = 0

    result = node(state)

    assert legacy_calls == 1
    assert shadow_calls == 1
    assert _authoritative_financial_result_bytes(result) == (
        _authoritative_financial_result_bytes(baseline)
    )
    assert "LegacyTotal,100" in result["messages"][0].content
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.0"
    assert "provider_subrequest_cache" not in result["financial_dispatch_ledger"]
    assert "financial_dispatch_shadow_ledger" not in result
    assert result["financial_dispatch_shadow_failure"] == (
        _single_localized_shadow_failure(
            {
                "stage": "selection_dispatch",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": False,
                "dispatcher_constructed": True,
            }
        )
    )
    assert "raw-provider-secret-must-not-escape" not in json.dumps(
        result,
        default=str,
    )
    checkpoint_state = _financial_tool_state(asset, plan)
    checkpoint_state.update(result)
    compatibility = node.validate_checkpoint_state(
        checkpoint_state,
        expected_asset_configuration=asset,
        expected_capability_routing_plan=plan,
    )
    assert compatibility is not None
    assert compatibility.action is MainlandCapabilityRoutingCheckpointAction.RESUME

    historical_state = _financial_tool_state(asset, plan)
    historical_ledger = copy.deepcopy(result["financial_dispatch_ledger"])
    historical_ledger["provider_subrequest_cache"] = QualifiedCache().checkpoint()
    historical_state["financial_dispatch_ledger"] = historical_ledger

    restored = node(historical_state)

    assert "LegacyTotal,100" in restored["messages"][0].content
    assert "provider_subrequest_cache" not in restored[
        "financial_dispatch_ledger"
    ]


def test_hostile_typed_reason_extraction_cannot_escape_or_change_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    _assert_shadow_authority(plan)
    fixed_clock = lambda: datetime(2026, 7, 30, 12, tzinfo=timezone.utc)  # noqa: E731
    baseline_calls = 0
    observed_calls = 0
    shadow_calls = 0

    def legacy_value(symbol: str, frequency: str) -> str:
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    def baseline_legacy(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        nonlocal baseline_calls
        baseline_calls += 1
        return legacy_value(symbol, frequency)

    def observed_legacy(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        nonlocal observed_calls
        observed_calls += 1
        return legacy_value(symbol, frequency)

    baseline_config = _rollout_config("legacy", enabled=())
    baseline_config["tool_vendors"] = dict.fromkeys(
        (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ),
        "scripted",
    )
    baseline = FinancialDispatchToolNode(
        config=baseline_config,
        vendor_methods=_financial_vendor_methods(baseline_legacy),
        clock=fixed_clock,
    )(_financial_tool_state(asset, plan))
    hostile = FinancialDispatchCheckpointError(
        FinancialDispatchCheckpointFailureReason.MALFORMED
    )
    hostile.args = (
        "token=unsafe-hostile-reason",
        {"provider_payload": "unsafe-hostile-args"},
    )

    def raise_from_reason(_self):
        raise SystemExit("unsafe-hostile-reason-accessor")

    monkeypatch.setattr(
        FinancialDispatchCheckpointError,
        "reason",
        property(raise_from_reason),
        raising=False,
    )

    def failing_source(_request):
        nonlocal shadow_calls
        shadow_calls += 1
        raise hostile

    config = _rollout_config("qualified_v1_shadow")
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = baseline_config["tool_vendors"]
    observed = FinancialDispatchToolNode(
        config=config,
        vendor_methods=_financial_vendor_methods(observed_legacy),
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": failing_source},
        ),
        qualified_statement_request_factory=lambda _request: _statement_request(
            asset
        ),
        clock=fixed_clock,
    )(_financial_tool_state(asset, plan))

    assert observed_calls == baseline_calls == 1
    assert shadow_calls == 1
    assert _authoritative_financial_result_bytes(observed) == (
        _authoritative_financial_result_bytes(baseline)
    )
    failure = observed["financial_dispatch_shadow_failure"]
    assert failure["failures"][0]["stage"] == "selection_dispatch"
    assert failure["failures"][0]["exception_family"] == "financial_dispatch"
    assert failure["failures"][0]["exception_class"] == (
        "FinancialDispatchCheckpointError"
    )
    assert "typed_reason" not in failure["failures"][0]
    serialized = json.dumps(observed, default=str, sort_keys=True)
    for forbidden in (
        "unsafe-hostile-reason",
        "unsafe-hostile-args",
        "unsafe-hostile-reason-accessor",
    ):
        assert forbidden not in serialized


def test_shadow_request_factory_failure_cannot_change_legacy_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    _assert_shadow_authority(plan)
    legacy_calls = 0
    factory_calls = 0
    fixed_clock = lambda: datetime(2026, 7, 30, 12, tzinfo=timezone.utc)  # noqa: E731

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

    def failing_factory(_request):
        nonlocal factory_calls
        factory_calls += 1
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
    vendor_methods = _financial_vendor_methods(legacy_balance_sheet)
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        qualified_statement_request_factory=failing_factory,
        clock=fixed_clock,
    )

    state = _financial_tool_state(asset, plan)
    baseline_config = dict(config)
    baseline_config["mainland_capability_routing_mode"] = "legacy"
    baseline_config.pop("mainland_capability_routing_plan_signature", None)
    baseline = FinancialDispatchToolNode(
        config=baseline_config,
        vendor_methods=vendor_methods,
        clock=fixed_clock,
    )(state)
    assert legacy_calls == 1
    legacy_calls = 0

    result = node(state)

    assert legacy_calls == 1
    assert factory_calls == 1
    assert _authoritative_financial_result_bytes(result) == (
        _authoritative_financial_result_bytes(baseline)
    )
    assert "LegacyTotal,100" in result["messages"][0].content
    assert result["financial_dispatch_shadow_failure"]["failure_count"] == 1
    assert "raw-shadow-factory-secret" not in json.dumps(result, default=str)


def test_shadow_request_enrichment_failure_cannot_change_legacy_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(
        monkeypatch,
        "qualified_v1_shadow",
        enabled=("financial_indicators",),
    ).plan
    assert plan is not None
    _assert_shadow_authority(plan)
    legacy_calls = 0
    qualified_factory_calls = 0
    fixed_clock = lambda: datetime(2026, 7, 30, 12, tzinfo=timezone.utc)  # noqa: E731

    def legacy_fundamentals(*_args, **_kwargs) -> str:
        nonlocal legacy_calls
        legacy_calls += 1
        return (
            "# Company Fundamentals for 601328.SS\n"
            "# Data retrieved on: 2026-08-10 12:00:00\n\n"
            "LegacyMetric: 100"
        )

    def failing_enrichment(*_args, **_kwargs):
        raise RuntimeError("raw-shadow-enrichment-secret")

    def indicator_factory(_request):
        nonlocal qualified_factory_calls
        qualified_factory_calls += 1
        return None

    config = _rollout_config(
        "qualified_v1_shadow",
        enabled=("financial_indicators",),
    )
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
    vendor_methods = _financial_vendor_methods(lambda *_args, **_kwargs: "unused")
    vendor_methods["get_fundamentals"] = {"scripted": legacy_fundamentals}
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={},
        ),
        qualified_indicator_request_factory=indicator_factory,
        clock=fixed_clock,
    )
    monkeypatch.setattr(
        node,
        "_qualified_dispatch_request",
        failing_enrichment,
    )
    state = _financial_tool_state(asset, plan)
    state["messages"] = [
        AIMessage(
            content="",
            id="shadow-enrichment-message",
            tool_calls=[
                {
                    "name": "get_fundamentals",
                    "args": {
                        "ticker": asset.instrument_identity.symbol,
                        "curr_date": "2026-07-30",
                    },
                    "id": "shadow-fundamentals",
                    "type": "tool_call",
                }
            ],
        )
    ]

    baseline_config = dict(config)
    baseline_config["mainland_capability_routing_mode"] = "legacy"
    baseline_config.pop("mainland_capability_routing_plan_signature", None)
    baseline = FinancialDispatchToolNode(
        config=baseline_config,
        vendor_methods=vendor_methods,
        clock=fixed_clock,
    )(state)
    assert legacy_calls == 1
    legacy_calls = 0

    result = node(state)

    assert legacy_calls == 1
    assert qualified_factory_calls == 0
    assert _authoritative_financial_result_bytes(result) == (
        _authoritative_financial_result_bytes(baseline)
    )
    assert "LegacyMetric: 100" in result["messages"][0].content
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.0"
    assert "financial_dispatch_shadow_ledger" not in result
    assert result["financial_dispatch_shadow_failure"] == (
        _single_localized_shadow_failure(
            {
                "stage": "request_enrichment",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "indicator",
                "context_constructed": False,
                "dispatcher_constructed": True,
            }
        )
    )
    assert "raw-shadow-enrichment-secret" not in json.dumps(result, default=str)


def test_shadow_checkpoint_projection_failure_preserves_legacy_and_call_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    _assert_shadow_authority(plan)
    plan = _historical_plan(plan)
    fixed_clock = lambda: datetime(2026, 7, 30, 12, tzinfo=timezone.utc)  # noqa: E731
    baseline_legacy_calls = 0
    observed_legacy_calls = 0
    baseline_qualified_calls = 0
    observed_qualified_calls = 0

    def legacy_value(symbol: str, frequency: str) -> str:
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            "# Data retrieved on: 2026-07-30 12:00:00\n\n"
            "metric,2026-06-30\nLegacyTotal,100"
        )

    def baseline_legacy(symbol: str, frequency: str, _current_date: str) -> str:
        nonlocal baseline_legacy_calls
        baseline_legacy_calls += 1
        return legacy_value(symbol, frequency)

    def observed_legacy(symbol: str, frequency: str, _current_date: str) -> str:
        nonlocal observed_legacy_calls
        observed_legacy_calls += 1
        return legacy_value(symbol, frequency)

    def baseline_source(request):
        nonlocal baseline_qualified_calls
        baseline_qualified_calls += 1
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=request.provider_id,
            reason="no_data",
        )

    def observed_source(request):
        nonlocal observed_qualified_calls
        observed_qualified_calls += 1
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=request.provider_id,
            reason="no_data",
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
    state = _financial_tool_state(asset, plan)
    baseline = FinancialDispatchToolNode(
        config=config,
        vendor_methods=_financial_vendor_methods(baseline_legacy),
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources=dict.fromkeys(
                ("tushare", "akshare_sina", "yfinance"),
                baseline_source,
            ),
        ),
        qualified_statement_request_factory=lambda _request: _statement_request(
            asset
        ),
        clock=fixed_clock,
    )(state)
    assert "financial_dispatch_shadow_failure" not in baseline, baseline.get(
        "financial_dispatch_shadow_failure"
    )

    original_checkpoint_ledger = FinancialToolDispatcher.checkpoint_ledger
    shadow_checkpoint_calls = 0

    def fail_shadow_checkpoint(dispatcher):
        nonlocal shadow_checkpoint_calls
        ledger = original_checkpoint_ledger(dispatcher)
        if ledger.get("capability_routing_plan_signature") == plan.plan_signature:
            shadow_checkpoint_calls += 1
            raise RuntimeError("unsafe-shadow-checkpoint-payload")
        return ledger

    monkeypatch.setattr(
        FinancialToolDispatcher,
        "checkpoint_ledger",
        fail_shadow_checkpoint,
    )
    observed = FinancialDispatchToolNode(
        config=config,
        vendor_methods=_financial_vendor_methods(observed_legacy),
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources=dict.fromkeys(
                ("tushare", "akshare_sina", "yfinance"),
                observed_source,
            ),
        ),
        qualified_statement_request_factory=lambda _request: _statement_request(
            asset
        ),
        clock=fixed_clock,
    )(state)

    assert baseline_legacy_calls == observed_legacy_calls == 1
    assert baseline_qualified_calls == observed_qualified_calls
    assert baseline_qualified_calls > 0
    assert shadow_checkpoint_calls == 1
    assert _authoritative_financial_result_bytes(observed) == (
        _authoritative_financial_result_bytes(baseline)
    )
    assert observed["financial_dispatch_shadow_failure"] == (
        _single_localized_shadow_failure(
            {
                "stage": "checkpoint_projection",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": False,
                "dispatcher_constructed": True,
            }
        )
    )
    assert "unsafe-shadow-checkpoint-payload" not in json.dumps(
        observed,
        default=str,
        sort_keys=True,
    )


def test_multiple_shadow_failures_are_independently_localized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    _assert_shadow_authority(plan)
    legacy_calls = 0
    factory_calls = 0
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

    def request_factory(request):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise RuntimeError("unsafe-first-factory-payload")
        return _statement_request(asset).model_copy(
            update={"statement_type": request.statement_type}
        )

    def qualified_source(_request):
        nonlocal shadow_calls
        shadow_calls += 1
        raise RuntimeError("unsafe-second-selection-payload")

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
        vendor_methods={
            tool_name: {"scripted": legacy_statement}
            for tool_name in config["tool_vendors"]
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": qualified_source},
        ),
        qualified_statement_request_factory=request_factory,
    )
    state = _financial_tool_state(asset, plan)
    state["messages"] = [
        AIMessage(
            content="",
            id="two-distinct-shadow-failures",
            tool_calls=[
                {
                    "name": "get_balance_sheet",
                    "args": {
                        "ticker": asset.instrument_identity.symbol,
                        "freq": "quarterly",
                        "curr_date": "2026-07-30",
                    },
                    "id": "factory-failure",
                    "type": "tool_call",
                },
                {
                    "name": "get_income_statement",
                    "args": {
                        "ticker": asset.instrument_identity.symbol,
                        "freq": "quarterly",
                        "curr_date": "2026-07-30",
                    },
                    "id": "selection-failure",
                    "type": "tool_call",
                },
            ],
        )
    ]

    result = node(state)

    assert legacy_calls == 2
    assert factory_calls == 2
    assert shadow_calls == 1
    assert result["financial_dispatch_shadow_failure"] == {
        "contract_version": "2.1",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 2,
        "retained_failure_count": 2,
        "dropped_failure_count": 0,
        "failures": [
            {
                "stage": "request_factory",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": False,
                "dispatcher_constructed": True,
            },
            {
                "stage": "selection_dispatch",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": False,
                "dispatcher_constructed": True,
            },
        ],
    }
    serialized = json.dumps(result, default=str, sort_keys=True)
    assert "unsafe-first-factory-payload" not in serialized
    assert "unsafe-second-selection-payload" not in serialized


def test_shadow_failure_retention_is_bounded_with_exact_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_RETENTION_BOUND == 7
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    _assert_shadow_authority(plan)
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

    def qualified_source(_request):
        nonlocal shadow_calls
        shadow_calls += 1
        raise RuntimeError("unsafe-overflow-provider-payload")

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
        vendor_methods={
            tool_name: {"scripted": legacy_statement}
            for tool_name in config["tool_vendors"]
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": qualified_source},
        ),
        qualified_statement_request_factory=lambda request: (
            _statement_request(asset).model_copy(
                update={
                    "statement_type": request.statement_type,
                    "as_of_date": request.as_of_date,
                }
            )
        ),
    )
    total_failures = MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_RETENTION_BOUND + 2
    state = _financial_tool_state(asset, plan)
    state["messages"] = [
        AIMessage(
            content="",
            id="bounded-shadow-failures",
            tool_calls=[
                {
                    "name": "get_balance_sheet",
                    "args": {
                        "ticker": asset.instrument_identity.symbol,
                        "freq": "quarterly",
                        "curr_date": f"2026-07-{20 + index:02d}",
                    },
                    "id": f"bounded-failure-{index}",
                    "type": "tool_call",
                }
                for index in range(total_failures)
            ],
        )
    ]

    result = node(state)

    assert legacy_calls == total_failures
    assert shadow_calls == total_failures
    failure = result["financial_dispatch_shadow_failure"]
    assert failure["failure_count"] == total_failures
    assert failure["retained_failure_count"] == 7
    assert failure["dropped_failure_count"] == 2
    assert len(failure["failures"]) == 7
    assert failure["failures"] == [
        {
            "stage": "selection_dispatch",
            "failure_kind": "exception",
            "exception_family": "builtins",
            "exception_class": "RuntimeError",
            "selection_kind": "statement",
            "context_constructed": False,
            "dispatcher_constructed": True,
        }
    ] * 7
    assert "unsafe-overflow-provider-payload" not in json.dumps(
        result,
        default=str,
        sort_keys=True,
    )


def test_later_shadow_failure_preserves_prior_manifest_and_legacy_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    plan = _historical_plan(plan)
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
    assert result["financial_dispatch_shadow_failure"] == (
        _single_localized_shadow_failure(
            {
                "stage": "selection_dispatch",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": False,
                "dispatcher_constructed": True,
            }
        )
    )
    assert "raw-second-shadow-secret" not in json.dumps(result, default=str)


def test_earlier_shadow_failure_does_not_skip_later_independent_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _preflight(monkeypatch, "qualified_v1_shadow").plan
    assert plan is not None
    plan = _historical_plan(plan)
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
    plan = _historical_plan(plan)
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
    assert shadow_ledger["contract_version"] == "1.3"
    assert shadow_ledger["financial_eligibility_binding"] == (
        plan.financial_eligibility_binding.model_dump(mode="json")
    )
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
    missing_state_binding = _financial_tool_state(asset, plan)
    missing_state_binding.pop("financial_eligibility_binding")
    with pytest.raises(FinancialDispatchCheckpointError) as missing_state_error:
        resumed_node(missing_state_binding)
    assert missing_state_error.value.reason is (
        FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
    )

    missing_evidence_binding = _financial_tool_state(asset, plan)
    missing_evidence_binding["evidence_state"].pop(
        "financial_eligibility_binding"
    )
    with pytest.raises(FinancialDispatchCheckpointError) as missing_evidence_error:
        resumed_node(missing_evidence_binding)
    assert missing_evidence_error.value.reason is (
        FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
    )
    assert legacy_calls == initial_legacy_calls
    assert shadow_calls == initial_shadow_calls
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
            "financial_eligibility_binding": (
                plan.financial_eligibility_binding.model_dump(mode="json")
            ),
            "capability_routing_rollout": (
                MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(
                    mode="json"
                )
            ),
            "capability_routing_checkpoint_compatibility": (
                validate_mainland_capability_routing_checkpoint(
                    active_plan=plan,
                    checkpoint_plan=plan,
                ).model_dump(mode="json")
            ),
            "financial_dispatch_ledger": result["financial_dispatch_ledger"],
            "financial_dispatch_shadow_ledger": shadow_ledger,
            "decision_audit_created_at": "2026-07-30T12:00:00+00:00",
        }
    )

    start_new_state = copy.deepcopy(final_state)
    audit = prepare_decision_audit(
        final_state,
        tmp_path / "audit-artifacts",
        config=config,
    )

    assert audit["capability_routing_rollout"]["mode"] == "qualified_v1_shadow"
    assert audit["capability_routing_rollout"]["disposition"] == (
        "shadow_non_authoritative"
    )
    assert audit["capability_routing_checkpoint_compatibility"]["action"] == (
        "resume"
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
    assert "Qualified routing shadow financial operations" in report
    assert "mainland-financial-period-eligibility-v2" in report
    assert "mainland-financial-reporting-calendar-v2" in report
    assert "Reporting target periods" in report
    assert "Selected lineage" in report
    assert "Checkpoint / new-run compatibility" in report
    assert "**Compatibility action:** `resume`" in report
    assert "**Provider I/O during compatibility check:** false" in report
    assert "**Historical state preserved:** true" in report
    assert "financial-period-candidate" not in report

    historical_payload = plan.model_dump(mode="json")
    historical_payload.pop("eligibility_policy_version")
    historical_payload.pop("financial_eligibility_policy")
    historical_payload.pop("financial_eligibility_binding")
    signature_payload = {
        key: value
        for key, value in historical_payload.items()
        if key != "plan_signature"
    }
    historical_payload["plan_signature"] = (
        "mainland-routing-plan:v1:"
        + sha256(
            json.dumps(
                signature_payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    historical_plan = MainlandCapabilityRoutingPlan.model_validate(
        historical_payload
    )
    start_new_compatibility = start_new_mainland_capability_routing_run(
        active_plan=plan,
        checkpoint_plan=historical_plan,
        previous_run_id="run:" + "a" * 64,
        new_run_id=start_new_state["run_id"],
    )
    start_new_state["capability_routing_checkpoint_compatibility"] = (
        start_new_compatibility.model_dump(mode="json")
    )
    cross_run_state = copy.deepcopy(start_new_state)
    cross_run_state["capability_routing_checkpoint_compatibility"][
        "new_run_id"
    ] = "run:" + "c" * 64
    with pytest.raises(ValueError, match="new run identity"):
        prepare_decision_audit(
            cross_run_state,
            tmp_path / "cross-run-audit",
            config=config,
        )
    start_new_audit = prepare_decision_audit(
        start_new_state,
        tmp_path / "start-new-audit-artifacts",
        config=config,
    )
    assert start_new_audit["capability_routing_checkpoint_compatibility"] == (
        start_new_compatibility.model_dump(mode="json")
    )
    start_new_report = write_report_tree(
        start_new_state,
        asset.instrument_identity.symbol,
        tmp_path / "start-new-report",
    ).read_text(encoding="utf-8")
    for expected in (
        "**Compatibility action:** `start_new`",
        "financial_eligibility_policy_mismatch",
        "run:" + "a" * 64,
        start_new_state["run_id"],
    ):
        assert expected in start_new_report

    failed_shadow_state = copy.deepcopy(final_state)
    failed_shadow_state["financial_dispatch_shadow_failure"] = {
        "contract_version": "1.0",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 1,
    }
    raw_failure_text = (
        "ProviderTransportError GET "
        "https://provider.example.test/v1/indicator?token=query-secret "
        'payload={"account":"private-payload"} credential=provider-secret '
        r"raw_text=C:\Users\operator\provider-payload.json"
    )
    failed_shadow_state["provider_failure_debug"] = raw_failure_text
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
    assert "Shadow failure diagnostic contract:** `1.0`" in failed_shadow_report
    assert "Sanitized shadow failures:** 1" in failed_shadow_report
    exposed_failed_surfaces = failed_shadow_report + json.dumps(
        failed_shadow_audit,
        sort_keys=True,
    )
    for forbidden in (
        "ProviderTransportError",
        "provider.example.test",
        "token=query-secret",
        "private-payload",
        "provider-secret",
        r"C:\Users\operator\provider-payload.json",
        "raw_text",
        raw_failure_text,
    ):
        assert forbidden not in exposed_failed_surfaces

    current_failure = {
        "contract_version": "2.1",
        "diagnostic_code": "shadow_dispatch_failed",
        "failure_count": 9,
        "retained_failure_count": 7,
        "dropped_failure_count": 2,
        "failures": [
            {
                "stage": "context_result",
                "failure_kind": "typed_result",
                "typed_acquisition_reason": "temporal_eligibility_rejected",
                "temporal_rejection_reasons": ["ann_date_missing"],
                "selection_kind": "indicator",
                "context_constructed": False,
                "dispatcher_constructed": False,
            },
            {
                "stage": "context_exception",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": False,
                "dispatcher_constructed": False,
            },
            {
                "stage": "dispatcher_construction",
                "failure_kind": "exception",
                "exception_family": "financial_dispatch",
                "exception_class": "FinancialDispatchCheckpointError",
                "typed_reason": "financial_dispatch_checkpoint_malformed",
                "selection_kind": "statement",
                "context_constructed": True,
                "dispatcher_constructed": False,
            },
            {
                "stage": "request_enrichment",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "indicator",
                "context_constructed": True,
                "dispatcher_constructed": True,
            },
            {
                "stage": "request_factory",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": True,
                "dispatcher_constructed": True,
            },
            {
                "stage": "selection_dispatch",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "canonical_request_reference": (
                    "financial-request:v1:" + "a" * 64
                ),
                "selection_kind": "statement",
                "context_constructed": True,
                "dispatcher_constructed": True,
            },
            {
                "stage": "checkpoint_projection",
                "failure_kind": "exception",
                "exception_family": "unknown",
                "exception_class": "unknown",
                "selection_kind": "statement",
                "context_constructed": True,
                "dispatcher_constructed": True,
            },
        ],
    }
    current_failed_shadow_state = copy.deepcopy(final_state)
    current_failed_shadow_state["financial_dispatch_shadow_failure"] = (
        current_failure
    )
    current_failed_shadow_state["provider_failure_debug"] = raw_failure_text
    current_failed_shadow_audit = prepare_decision_audit(
        current_failed_shadow_state,
        tmp_path / "current-failed-shadow-audit",
        config=config,
    )
    repeated_failed_shadow_audit = prepare_decision_audit(
        copy.deepcopy(current_failed_shadow_state),
        tmp_path / "repeated-current-failed-shadow-audit",
        config=config,
    )

    assert current_failed_shadow_audit["financial_dispatch_shadow_failure"] == (
        current_failure
    )
    assert repeated_failed_shadow_audit["financial_dispatch_shadow_failure"] == (
        current_failure
    )
    current_failed_shadow_report = write_report_tree(
        current_failed_shadow_state,
        asset.instrument_identity.symbol,
        tmp_path / "current-failed-shadow-report",
    ).read_text(encoding="utf-8")
    for expected in (
        "Shadow failure diagnostic contract:** `2.1`",
        "Sanitized shadow failures:** 9",
        "Retained localized failures:** 7",
        "Dropped localized failures:** 2",
        "Failure 1 stage:** `context_result`",
        "Failure 1 acquisition reason:** `temporal_eligibility_rejected`",
        "Failure 1 temporal rejection reasons:** `ann_date_missing`",
        "Failure 2 stage:** `context_exception`",
        "Failure 3 stage:** `dispatcher_construction`",
        "Failure 3 typed reason:** `financial_dispatch_checkpoint_malformed`",
        "Failure 4 stage:** `request_enrichment`",
        "Failure 5 stage:** `request_factory`",
        "Failure 6 stage:** `selection_dispatch`",
        "Failure 6 canonical request:** `financial-request:v1:",
        "Failure 7 stage:** `checkpoint_projection`",
        "Failure 7 context constructed:** true",
        "Failure 7 dispatcher constructed:** true",
    ):
        assert expected in current_failed_shadow_report
    current_exposed_surfaces = current_failed_shadow_report + json.dumps(
        current_failed_shadow_audit,
        sort_keys=True,
    )
    for forbidden in (
        "ProviderTransportError",
        "provider.example.test",
        "token=query-secret",
        "private-payload",
        "provider-secret",
        r"C:\Users\operator\provider-payload.json",
        "raw_text",
        raw_failure_text,
    ):
        assert forbidden not in current_exposed_surfaces
    assert plan.financial_eligibility_policy is not None
    assert plan.financial_eligibility_policy.authority_disposition == (
        "shadow_non_authoritative"
    )
    assert plan.financial_eligibility_policy.qualified_v1_authority_enabled is False

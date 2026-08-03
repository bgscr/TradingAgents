from __future__ import annotations

import copy
import gzip
import json
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from typer.testing import CliRunner

import cli.main as cli_main
import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.interface as vendor_interface
from tests.test_financial_family_completeness import (
    _ALL_FAMILIES,
    _candidate,
    _requirements,
    _route,
)
from tests.test_financial_graph_dispatch import _analysis_outcome_state
from tests.test_financial_manifest_evidence_reporting import _attempt_event
from tests.test_financial_period_targets import (
    _ANNUAL_TARGETS,
    _REPORTING_TARGETS,
)
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingRunProjection,
    validate_mainland_capability_routing_checkpoint,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialProviderPeriodResponse,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCompanyType,
    FinancialPeriodCandidate,
    FinancialRatioFamily,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    EvidenceState,
    SourceAcquisitionAvailable,
    SourceArtifact,
    stable_acquisition_source_ref,
)
from tradingagents.evidence_artifacts import (
    SourceArtifactManifest,
    project_evidence_for_audit,
)
from tradingagents.graph.financial_tools import (
    FinancialDispatchToolNode,
    QualifiedFinancialRoutingComposition,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph

_AS_OF = date(2026, 8, 1)
_POLICY_VERSION = "mainland-financial-period-eligibility-v2"
_SPARSE_BANK_FAMILIES = (
    FinancialRatioFamily.PROFIT,
    FinancialRatioFamily.OPERATION,
    FinancialRatioFamily.BALANCE,
    FinancialRatioFamily.DUPONT,
)
_RAW_CANARY_MARKERS = (
    "ticket07-credential-canary",
    "ticket07-token-canary",
    "ticket07-url-query-canary",
    "ticket07-raw-exception-canary",
    "ticket07-payload-body-canary",
    "ticket07-absolute-path-canary",
)
_RAW_CANARY_TEXT = "\n".join(
    (
        "credential=ticket07-credential-canary",
        "token=ticket07-token-canary",
        (
            "url=https://provider.invalid/financial?"
            "marker=ticket07-url-query-canary"
        ),
        "raw_exception=Ticket07ProviderError(ticket07-raw-exception-canary)",
        'payload_body={"marker":"ticket07-payload-body-canary"}',
        "machine_path=C:\\ticket07-absolute-path-canary\\fixture.json",
    )
)
_RAW_CANARY_DIGEST = sha256(_RAW_CANARY_TEXT.encode()).hexdigest()


class _NoOpDisplay:
    def start(self) -> None:
        return None

    def refresh(self, *_args, **_kwargs) -> None:
        return None

    def publish_event(self, *_args, **_kwargs) -> None:
        return None

    def report_ready(self, *_args, **_kwargs) -> None:
        return None

    def close(self) -> None:
        return None


def _run_id(symbol: str) -> str:
    return "run:" + sha256(f"ticket-07:{symbol}".encode()).hexdigest()


def _vendor_methods(legacy_fundamentals):
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    return {
        "get_fundamentals": {"scripted": legacy_fundamentals},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }


def _identity_evidence(asset) -> EvidenceState:
    source_ref = stable_acquisition_source_ref(
        "identity-registry",
        asset.registry_source_ref,
        asset.registry_digest,
    )
    artifact = SourceArtifact(
        artifact_sha256=asset.registry_digest,
        source_ref=source_ref,
        tool_call_id="identity-registry",
        tool_name="instrument_identity_registry",
        raw_text=asset.registry_artifact,
    )
    canary_source_ref = stable_acquisition_source_ref(
        "ticket-07-raw-fixture",
        "deterministic-shadow-artifact",
        _RAW_CANARY_DIGEST,
    )
    canary_artifact = SourceArtifact(
        artifact_sha256=_RAW_CANARY_DIGEST,
        source_ref=canary_source_ref,
        tool_call_id="ticket-07-raw-fixture",
        tool_name="deterministic_shadow_artifact",
        raw_text=_RAW_CANARY_TEXT,
    )
    return EvidenceState(
        instrument_identity=asset.instrument_identity,
        source_artifacts=(artifact, canary_artifact),
        acquisition_outcomes=(
            SourceAcquisitionAvailable(
                provider="instrument-identity-registry",
                capability="instrument_identity",
                source_ref=source_ref,
                attempt=1,
                retrieved_at=asset.instrument_identity.provenance.retrieved_at,
                artifact=artifact,
            ),
            SourceAcquisitionAvailable(
                provider="deterministic-shadow-fixture",
                capability="financial_indicator_raw_artifact",
                source_ref=canary_source_ref,
                attempt=1,
                retrieved_at=asset.instrument_identity.provenance.retrieved_at,
                artifact=canary_artifact,
            ),
        ),
    )


def _qualified_response(
    *,
    routed,
    symbol: str,
    company_type: FinancialCompanyType,
) -> tuple[
    FinancialProviderPeriodResponse,
    FinancialPeriodCandidate,
    FinancialPeriodCandidate,
]:
    candidates = {
        item.candidate.candidate_identity: item.candidate
        for manifest in routed.family_manifests
        for item in manifest.provider_candidates
    }
    duplicate = _candidate(
        symbol=symbol,
        company_type=company_type,
        family=FinancialRatioFamily.GROWTH,
        period=_REPORTING_TARGETS[0],
        observed_at=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
    )
    selected = candidates[
        next(
            key
            for key, candidate in candidates.items()
            if candidate.period_identity.ratio_family is FinancialRatioFamily.GROWTH
            and candidate.period_identity.period_end == _REPORTING_TARGETS[0]
        )
    ]
    candidates[duplicate.candidate_identity] = duplicate
    ordered = tuple(candidates[key] for key in sorted(candidates))
    event = _attempt_event(ordinal=7)
    return (
        FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=ordered[0].artifact,
            retained_artifacts=tuple(
                {
                    candidate.artifact.artifact_identity: candidate.artifact
                    for candidate in ordered
                }.values()
            ),
            candidates=ordered,
            subrequest_keys=(event.request_key,),
            physical_attempt_ids=(event.attempt_event_id,),
            physical_attempt_events=(event,),
        ),
        selected,
        duplicate,
    )


@pytest.mark.parametrize(
    (
        "symbol",
        "company_type",
        "sparse_families",
        "expected_complete",
        "expected_incomplete",
    ),
    (
        (
            "601328.SS",
            FinancialCompanyType.BANK,
            _SPARSE_BANK_FAMILIES,
            (FinancialRatioFamily.GROWTH, FinancialRatioFamily.CASH_FLOW),
            _SPARSE_BANK_FAMILIES,
        ),
        (
            "600895.SS",
            FinancialCompanyType.INDUSTRIAL_NON_BANK,
            (),
            _ALL_FAMILIES,
            (),
        ),
    ),
)
def test_ticket07_corrected_shadow_joint_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symbol: str,
    company_type: FinancialCompanyType,
    sparse_families: tuple[FinancialRatioFamily, ...],
    expected_complete: tuple[FinancialRatioFamily, ...],
    expected_incomplete: tuple[FinancialRatioFamily, ...],
) -> None:
    selection_request, routed, plan = _route(
        monkeypatch,
        symbol=symbol,
        company_type=company_type,
        families=_ALL_FAMILIES,
        sparse_families=sparse_families,
        requirements=_requirements(_ALL_FAMILIES),
    )
    asset = resolve_run_asset_configuration(
        symbol,
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    response, selected_revision, equivalent_revision = _qualified_response(
        routed=routed,
        symbol=symbol,
        company_type=company_type,
    )
    provider_calls = {"legacy": 0, "shadow": 0}
    legacy_marker = f"LEGACY_AUTHORITATIVE_FINANCIAL_OUTPUT:{symbol}"
    legacy_output = (
        f"# Company Fundamentals for {symbol}\n"
        "# Data retrieved on: 2026-08-01 12:00:00\n\n"
        f"Authoritative selection: {legacy_marker}"
    )

    def legacy_fundamentals(*_args, **_kwargs) -> str:
        provider_calls["legacy"] += 1
        return legacy_output

    def qualified_source(_request):
        provider_calls["shadow"] += 1
        return response

    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config.update(
        {
            "checkpoint_enabled": False,
            "data_cache_dir": str(tmp_path / "cache"),
            "results_dir": str(tmp_path / "runs"),
            "memory_log_path": str(tmp_path / "memory.md"),
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "tool_vendors": dict.fromkeys(
                (
                    "get_fundamentals",
                    "get_balance_sheet",
                    "get_cashflow",
                    "get_income_statement",
                ),
                "scripted",
            ),
        }
    )
    vendor_methods = _vendor_methods(legacy_fundamentals)
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        indicator_sources={"tushare": qualified_source},
    )
    indicator_request_factory = lambda _request: selection_request  # noqa: E731

    def unexpected_statement_request(_request):
        raise AssertionError("indicator-only tracer requested a statement route")

    composition = QualifiedFinancialRoutingComposition(
        router=router,
        statement_request_factory=unexpected_statement_request,
        indicator_request_factory=indicator_request_factory,
    )
    deterministic_llm = MagicMock(name="ticket07_deterministic_llm")
    deterministic_client = MagicMock(name="ticket07_deterministic_client")
    deterministic_client.get_llm.return_value = deterministic_llm
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        lambda **_kwargs: deterministic_client,
    )
    production_graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=runtime_config,
        asset_configuration=asset,
        capability_routing_plan=plan,
        qualified_financial_routing=composition,
    )
    production_node = production_graph.tool_nodes["fundamentals"]
    assert isinstance(production_node, FinancialDispatchToolNode)
    assert production_node._qualified_statement_router is router
    assert (
        production_node._qualified_statement_request_factory
        is unexpected_statement_request
    )
    assert production_node._qualified_indicator_request_factory is (
        indicator_request_factory
    )
    assert {
        "tools_fundamentals",
        "Evidence Preflight",
        "Evidence Admission",
        "Decision Gate",
    } <= set(production_graph.workflow.nodes)
    production_initial = production_graph.create_initial_state(
        symbol,
        _AS_OF.isoformat(),
        evidence_state=_identity_evidence(asset),
        run_id=_run_id(symbol),
    )
    assert production_initial["capability_routing_plan"] == plan.model_dump(
        mode="json"
    )
    production_terminal = production_graph.graph.invoke(production_initial)
    assert production_terminal["analysis_outcome_contract"]["reason"] == (
        "preflight_blocked"
    )
    assert not production_terminal.get("decision_gate_v2")
    assert production_terminal.get("trading_decision") is None
    assert production_terminal.get("signal") is None
    assert production_terminal.get("position") is None
    deterministic_llm.invoke.assert_not_called()
    assert provider_calls == {"legacy": 0, "shadow": 0}

    memory_path = Path(runtime_config["memory_log_path"])
    memory_path.write_text("ticket-07-memory-sentinel\n", encoding="utf-8")
    memory_before = memory_path.read_bytes()
    financial_node = FinancialDispatchToolNode(
        config=runtime_config,
        vendor_methods=vendor_methods,
        qualified_statement_router=router,
        qualified_indicator_request_factory=indicator_request_factory,
    )
    tool_call = {
        "name": "get_fundamentals",
        "args": {"ticker": symbol, "curr_date": _AS_OF.isoformat()},
        "id": f"ticket-07-{symbol}-fundamentals",
        "type": "tool_call",
    }

    def scripted_model(_state: AgentState) -> dict[str, Any]:
        return {"messages": [AIMessage(content="", tool_calls=[tool_call])]}

    workflow = StateGraph(AgentState)
    workflow.add_node("scripted_model", scripted_model)
    workflow.add_node("tools_fundamentals", financial_node)
    workflow.add_edge(START, "scripted_model")
    workflow.add_edge("scripted_model", "tools_fundamentals")
    workflow.add_edge("tools_fundamentals", END)
    compiled = workflow.compile()
    captured: dict[str, Any] = {}

    class _CliStream:
        def stream(self, initial_state, **_kwargs):
            result = compiled.invoke(initial_state)
            captured["compiled"] = result
            evidence = EvidenceState.model_validate(result["evidence_state"])
            terminal = _analysis_outcome_state(evidence, ticker=symbol)
            terminal["trade_date"] = _AS_OF.isoformat()
            terminal["graph_signature"] = "|".join(
                (
                    "asset_configuration=" + asset.asset_configuration_signature,
                    "capability_routing=" + plan.plan_signature,
                )
            )
            yield {**result, **terminal}

    class _CliPropagator:
        @staticmethod
        def get_graph_args(*_args, **_kwargs):
            return {}

    class _CliGraph:
        def __init__(self, *_args, **kwargs) -> None:
            assert kwargs["asset_configuration"] == asset
            self.asset_configuration = asset
            self.graph = _CliStream()
            self.propagator = _CliPropagator()

        @staticmethod
        def resolve_evidence_state(*_args, **_kwargs) -> EvidenceState:
            return _identity_evidence(asset)

        def create_initial_state(
            self,
            *_args,
            evidence_state: EvidenceState,
            **_kwargs,
        ) -> dict[str, Any]:
            return Propagator().create_initial_state(
                symbol,
                _AS_OF.isoformat(),
                asset_type="stock",
                evidence_state=evidence_state,
                asset_configuration=asset,
                capability_routing_plan=plan,
                run_id=_run_id(symbol),
            )

        @staticmethod
        def _run_signature(_asset_type: str) -> str:
            return "|".join(
                (
                    "analysts=fundamentals",
                    "evidence_schema=4",
                    "asset_configuration=" + asset.asset_configuration_signature,
                    "capability_routing=" + plan.plan_signature,
                )
            )

    selections = {
        "ticker": symbol,
        "analysis_date": _AS_OF.isoformat(),
        "asset_type": "stock",
        "analysts": [SimpleNamespace(value="fundamentals")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": "scripted-quick",
        "deep_thinker": "scripted-deep",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", runtime_config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", _CliGraph)
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(vendor_interface, "VENDOR_METHODS", vendor_methods)
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *_args, **_kwargs: _NoOpDisplay(),
    )
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *_args, **_kwargs: "N")
    original_mark_completed = cli_main._mark_run_completed

    def capture_completed(final_state, artifacts) -> None:
        original_mark_completed(final_state, artifacts)
        captured["final"] = copy.deepcopy(final_state)
        captured["artifacts"] = artifacts

    monkeypatch.setattr(cli_main, "_mark_run_completed", capture_completed)

    cli_result = CliRunner().invoke(cli_main.app, [])

    assert cli_result.exit_code == 0, cli_result.output
    assert provider_calls == {"legacy": 1, "shadow": 1}
    assert memory_path.read_bytes() == memory_before
    assert "financial_dispatch_shadow_failure" not in captured["compiled"]
    tool_message = next(
        message
        for message in captured["compiled"]["messages"]
        if isinstance(message, ToolMessage)
    )
    assert tool_message.content == legacy_output
    assert captured["compiled"]["financial_dispatch_ledger"]["contract_version"] == (
        "1.0"
    )
    shadow_ledger = captured["compiled"]["financial_dispatch_shadow_ledger"]
    assert shadow_ledger["contract_version"] == "1.3"
    assert shadow_ledger["financial_eligibility_binding"] == (
        plan.financial_eligibility_binding.model_dump(mode="json")
    )

    final_state = captured["final"]
    assert final_state.get("trading_decision") is None
    assert not final_state.get("decision_gate_v2")
    assert final_state.get("signal") is None
    assert final_state.get("position") is None
    assert final_state.get("analysis_outcome_contract", {}).get("reason") == (
        "preflight_blocked"
    )
    status_path = next((tmp_path / "runs").rglob("run_status.json"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert status["lifecycle_status"] == "completed"
    assert status["current_phase"] is None
    assert status["terminal_outcome_kind"] == "analysis_outcome"

    report_path = next((tmp_path / "runs").rglob("complete_report.md"))
    audit_path = next((tmp_path / "runs").rglob("decision-audit.json"))
    report = report_path.read_text(encoding="utf-8")
    audit_text = audit_path.read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    assert audit["capability_routing_rollout"] == (
        MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(mode="json")
    )
    assert audit["financial_dispatch"]["contract_version"] == "1.0"
    shadow_audit = audit["financial_dispatch_shadow"]
    assert shadow_audit["contract_version"] == "2.0"
    assert shadow_audit["capability_routing_plan_signature"] == plan.plan_signature
    assert shadow_audit["financial_eligibility_binding"] == (
        plan.financial_eligibility_binding.model_dump(mode="json")
    )
    assert shadow_audit["acquisition_attempt_count"] == provider_calls["shadow"]
    assert len(shadow_audit["manifest_requests"]) == 1
    manifest_request = shadow_audit["manifest_requests"][0]
    family_assessments = {
        item["family"]: item for item in manifest_request["family_assessments"]
    }
    expected_family_names = {family.value for family in _ALL_FAMILIES}
    assert set(family_assessments) == expected_family_names
    assert {
        family for family, item in family_assessments.items() if item["complete"]
    } == {family.value for family in expected_complete}
    assert {
        family for family, item in family_assessments.items() if not item["complete"]
    } == {family.value for family in expected_incomplete}
    assert all(
        item["disposition"] == "degraded"
        for item in family_assessments.values()
    )
    assert all(
        item["eligibility_policy_version"] == _POLICY_VERSION
        for item in family_assessments.values()
    )
    assert manifest_request["aggregate_family_assessment"]["disposition"] == (
        "degraded"
    )
    assert "current_only_lineage" in manifest_request[
        "aggregate_family_assessment"
    ]["typed_reasons"]

    manifests = manifest_request["manifests"]
    assert manifests
    for manifest in manifests:
        targets = manifest["publication_target_construction"]
        assert targets["annual_target_period_ends"] == [
            period.isoformat() for period in _ANNUAL_TARGETS
        ]
        assert targets["reporting_target_period_ends"] == [
            period.isoformat() for period in _REPORTING_TARGETS
        ]
        q2 = next(
            item
            for item in targets["excluded_periods"]
            if item["period_end"] == "2026-06-30"
        )
        assert q2["publication_state"] == "not_yet_eligible"
        assert "2026-06-30" not in manifest["aggregate_completeness"][
            "missing_reporting_period_ends"
        ]
        assert manifest["requested_analysis_scope"] == "current_only"
        assert manifest["availability_scope"] == "current_only"
        assert manifest["availability_reason"] == (
            "missing_first_publication_for_strict_pit"
        )
        assert manifest["eligibility_policy_version"] == _POLICY_VERSION
        assert manifest["revision_selections"]
        assert manifest["conflicts"] == []
    growth_manifest = next(
        manifest
        for manifest in manifests
        if manifest["statement_or_ratio_family"]
        == FinancialRatioFamily.GROWTH.value
    )
    growth_revision = next(
        revision
        for revision in growth_manifest["revision_selections"]
        if revision["period_end"] == _REPORTING_TARGETS[0].isoformat()
    )
    assert growth_revision["selected_candidate_identity"] == (
        selected_revision.candidate_identity
    )
    assert growth_revision["selected_artifact_identity"] == (
        selected_revision.artifact.artifact_identity
    )
    assert growth_revision["ordering_basis"] == (
        "ann_date_observed_at_retrieved_at_local_revision_identity"
    )
    assert growth_revision["chronology_claim"] == "operational_only"
    revision_dispositions = {
        item["candidate_identity"]: (
            item["artifact_identity"],
            item["disposition"],
        )
        for item in growth_revision["candidate_dispositions"]
    }
    assert revision_dispositions == {
        selected_revision.candidate_identity: (
            selected_revision.artifact.artifact_identity,
            "selected",
        ),
        equivalent_revision.candidate_identity: (
            equivalent_revision.artifact.artifact_identity,
            "equivalent_unselected",
        ),
    }
    expected_revision_artifacts = {
        selected_revision.artifact.artifact_identity,
        equivalent_revision.artifact.artifact_identity,
    }
    assert {artifact for artifact, _disposition in revision_dispositions.values()} == (
        expected_revision_artifacts
    )
    assert expected_revision_artifacts <= set(growth_manifest["artifact_identities"])
    assert growth_manifest["aggregate_completeness"][
        "covered_reporting_period_ends"
    ].count(_REPORTING_TARGETS[0].isoformat()) == 1
    assert audit["evidence_state"]["source_facts"] == []
    assert "Qualified routing shadow comparison" in report
    assert "Legacy-selected financial output remains authoritative" in report
    assert "shadow_non_authoritative" in report
    assert _POLICY_VERSION in report
    assert "2026-06-30 / `not_yet_eligible`" in report
    assert "Revision selection" in report
    assert "equivalent_unselected" in report
    assert "Provider artifacts / selected lineage / conflicts" in report
    for family in _ALL_FAMILIES:
        assert f"##### `{family.value}` family" in report

    artifact_refs = {
        item["artifact_ref"]
        for item in audit["evidence_state"]["artifact_manifest"]["artifacts"]
    }
    assert f"artifact=sha256:{_RAW_CANARY_DIGEST}" in artifact_refs
    raw_artifact_path = (
        audit_path.parent
        / "evidence_artifacts"
        / "sha256"
        / _RAW_CANARY_DIGEST[:2]
        / f"{_RAW_CANARY_DIGEST}.utf8.gz"
    )
    assert gzip.decompress(raw_artifact_path.read_bytes()).decode("utf-8") == (
        _RAW_CANARY_TEXT
    )

    checkpoint_projection = {
        "capability_routing_plan": captured["compiled"]["capability_routing_plan"],
        "capability_routing_rollout": captured["compiled"][
            "capability_routing_rollout"
        ],
        "financial_eligibility_binding": captured["compiled"][
            "financial_eligibility_binding"
        ],
        "financial_dispatch_ledger": captured["compiled"][
            "financial_dispatch_ledger"
        ],
        "financial_dispatch_shadow_ledger": shadow_ledger,
    }
    evidence_projection = project_evidence_for_audit(
        EvidenceState.model_validate(captured["compiled"]["evidence_state"]),
        SourceArtifactManifest.model_validate_json(
            json.dumps(audit["evidence_state"]["artifact_manifest"])
        ),
    ).model_dump(mode="json")
    assert evidence_projection == audit["evidence_state"]

    public_surfaces = "\n".join(
        (
            report,
            audit_text,
            status_path.read_text(encoding="utf-8"),
            json.dumps(tool_message.artifact, sort_keys=True, default=str),
            json.dumps(checkpoint_projection, sort_keys=True, default=str),
            json.dumps(evidence_projection, sort_keys=True, default=str),
        )
    )
    for forbidden in (
        *_RAW_CANARY_MARKERS,
        "deterministic-fixture-only",
        "TUSHARE_TOKEN",
        "raw_payload",
        "raw_error",
        "https://",
        "http://",
        str(tmp_path),
    ):
        assert forbidden not in public_surfaces

    calls_before_resume = dict(provider_calls)
    resume_state = copy.deepcopy(captured["compiled"])
    resume_state["messages"] = [AIMessage(content="", tool_calls=[tool_call])]
    resumed = financial_node(resume_state)
    assert provider_calls == calls_before_resume
    assert resumed["financial_dispatch_shadow_ledger"][
        "qualified_indicator_selections"
    ][0]["reuse_count"] == 1
    compatibility = validate_mainland_capability_routing_checkpoint(
        active_plan=plan,
        checkpoint_plan=resume_state["capability_routing_plan"],
    )
    assert compatibility.provider_io_performed is False
    assert compatibility.checkpoint_rewrite_permitted is False
    assert compatibility.historical_state_preserved is True

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.test_financial_capability_routing_selection import (
    _ANNUAL_PERIODS,
    _REPORTING_PERIODS,
    _candidate,
    _qualified_plan,
    _ratio_candidate,
)
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialProviderPeriodResponse,
    FinancialStatementRoutingRequest,
    MainlandFinancialCapabilityRouter,
    render_indicator_routing_result,
    render_statement_routing_result,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialListingProvenance,
    FinancialPeriodRejectionReason,
    FinancialProviderArtifactIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialProvider,
    FinancialProviderVariant,
    FinancialToolDispatcher,
    FinancialToolMessageManifestEnvelope,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
    project_financial_selection_evidence,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestAttemptEvent,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.graph.financial_tools import FinancialDispatchToolNode
from tradingagents.reporting import _render_financial_dispatch_operations


def _attempt_event(
    *,
    ordinal: int = 1,
    outcome: ProviderSubrequestOutcomeKind = ProviderSubrequestOutcomeKind.AVAILABLE,
) -> ProviderSubrequestAttemptEvent:
    return ProviderSubrequestAttemptEvent(
        attempt_event_id=(
            "provider-physical-attempt=sha256:"
            + sha256(f"attempt-{ordinal}".encode()).hexdigest()
        ),
        sequence_id=(
            "provider-request-sequence=sha256:"
            + sha256(f"financial-sequence-{ordinal}".encode()).hexdigest()
        ),
        request_key=(
            "provider-subrequest:v1:"
            + sha256(f"financial-request-{ordinal}".encode()).hexdigest()
        ),
        upstream_service_id="tushare-pro:personal-research-primary",
        capacity_scope="balancesheet",
        attempt_index=1,
        attempted_at=datetime(2026, 7, 30, 12, ordinal, tzinfo=timezone.utc),
        pacing_event="permit_acquired",
        pacing_wait_seconds=0,
        outcome=outcome,
        retryable=False,
        cooldown_changed=False,
        cooldown_until=None,
        final_physical_attempt_count=1,
    )


def test_v2_manifest_retains_attempts_and_binds_final_rendered_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
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
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=candidates[0].artifact,
        retained_artifacts=tuple(candidate.artifact for candidate in candidates),
        candidates=candidates,
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    request = FinancialStatementRoutingRequest(
        instrument_identity=asset.instrument_identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=_ANNUAL_PERIODS,
        eligible_reporting_period_ends=_REPORTING_PERIODS,
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={"tushare": lambda _request: response},
    ).route_statement(request)

    rendered = render_statement_routing_result(result)
    manifest = result.manifest
    assert manifest.contract_version == "financial-manifest-v2"
    assert manifest.physical_attempt_events == (event,)
    assert manifest.disposition.value == "strict_pit_eligible"
    assert manifest.final_rendered_artifact is not None
    assert manifest.final_rendered_artifact.content_sha256 == sha256(
        rendered.encode("utf-8")
    ).hexdigest()
    assert manifest.final_rendered_artifact.byte_length == len(
        rendered.encode("utf-8")
    )


def test_v2_checkpoint_round_trip_restores_without_io_and_rejects_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    period = date(2026, 6, 30)
    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=asset.instrument_identity,
    )
    event = _attempt_event()
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=candidate.artifact,
        retained_artifacts=(candidate.artifact,),
        candidates=(candidate,),
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    selection_request = FinancialStatementRoutingRequest(
        instrument_identity=asset.instrument_identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=(),
        eligible_reporting_period_ends=(period,),
    )
    tool_request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=date(2026, 7, 30),
        tool_call_id="ticket-11-checkpoint",
    )
    provider_chain = (
        FinancialProvider(
            name="legacy-placeholder",
            variants=(
                FinancialProviderVariant(
                    variant_id="default",
                    invoke=lambda _request: "must not be called",
                ),
            ),
        ),
    )
    calls = 0

    def source(_request):
        nonlocal calls
        calls += 1
        return response

    plan = _qualified_plan(monkeypatch)
    original = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": source},
        ),
        run_scope_id="ticket-11-run",
    )
    first = original.dispatch_statement_selection(
        tool_request,
        selection_request,
    )
    checkpoint = original.checkpoint_ledger()

    assert checkpoint["contract_version"] == "1.2"
    assert checkpoint["run_scope_id"] == "ticket-11-run"
    assert checkpoint["capability_routing_plan_signature"] == plan.plan_signature
    assert (
        checkpoint["qualified_statement_selections"][0]["terminal"]["routing_result"][
            "manifest"
        ]["contract_version"]
        == "financial-manifest-v2"
    )

    restored = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                "tushare": lambda _request: pytest.fail(
                    "checkpoint restore repeated provider I/O"
                )
            },
        ),
        run_scope_id="ticket-11-run",
        checkpoint_ledger=checkpoint,
    )
    resumed = restored.dispatch_statement_selection(
        tool_request.model_copy(update={"tool_call_id": "ticket-11-resumed"}),
        selection_request,
    )
    assert calls == 1
    assert resumed.routing_result == first.routing_result

    tampered = copy.deepcopy(checkpoint)
    tampered["qualified_statement_selections"][0]["terminal"]["routing_result"][
        "manifest"
    ]["manifest_identity"] = "financial-manifest:v2:" + "f" * 64
    with pytest.raises(FinancialDispatchCheckpointError) as raised:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_balance_sheet": provider_chain},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
            ),
            run_scope_id="ticket-11-run",
            checkpoint_ledger=tampered,
        )
    assert raised.value.reason is (
        FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID
    )

    for identity_path in (
        (
            "qualified_statement_selections",
            0,
            "terminal",
            "routing_result",
            "manifest",
            "artifacts",
            0,
            "artifact",
            "artifact_identity",
        ),
        (
            "qualified_statement_selections",
            0,
            "terminal",
            "routing_result",
            "manifest",
            "selections",
            0,
            "candidate_identity",
        ),
    ):
        identity_tampered = copy.deepcopy(checkpoint)
        target = identity_tampered
        for component in identity_path[:-1]:
            target = target[component]
        target[identity_path[-1]] = (
            "financial-provider-artifact:v1:" + "e" * 64
            if identity_path[-1] == "artifact_identity"
            else "financial-period-candidate:v1:" + "d" * 64
        )
        with pytest.raises(FinancialDispatchCheckpointError) as raised:
            FinancialToolDispatcher(
                instrument_identity=asset.instrument_identity,
                run_asset_configuration=asset,
                provider_chains={"get_balance_sheet": provider_chain},
                qualified_statement_router=MainlandFinancialCapabilityRouter(
                    routing_plan=plan,
                    statement_sources={},
                ),
                run_scope_id="ticket-11-run",
                checkpoint_ledger=identity_tampered,
            )
        assert raised.value.reason is (
            FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID
        )

    with pytest.raises(FinancialDispatchCheckpointError) as raised:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_balance_sheet": provider_chain},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
            ),
            run_scope_id="different-run",
            checkpoint_ledger=checkpoint,
        )
    assert raised.value.reason is (
        FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
    )

    contradictory_plan = _qualified_plan(
        monkeypatch,
        account_scope_label="contradictory-account-scope",
    )
    with pytest.raises(FinancialDispatchCheckpointError) as raised:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_balance_sheet": provider_chain},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=contradictory_plan,
                statement_sources={},
            ),
            run_scope_id="ticket-11-run",
            checkpoint_ledger=checkpoint,
        )
    assert raised.value.reason is (
        FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
    )

    contradictory_request = copy.deepcopy(checkpoint)
    contradictory_request["qualified_statement_selections"][0]["terminal"][
        "selection_request"
    ]["currency"] = "USD"
    with pytest.raises(FinancialDispatchCheckpointError) as raised:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_balance_sheet": provider_chain},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
            ),
            run_scope_id="ticket-11-run",
            checkpoint_ledger=contradictory_request,
        )
    assert raised.value.reason is (
        FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID
    )

    contradictory_route = copy.deepcopy(checkpoint)
    contradictory_route["qualified_statement_selections"][0]["terminal"][
        "routing_result"
    ]["route"] = ["yfinance"]
    with pytest.raises(FinancialDispatchCheckpointError) as raised:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_balance_sheet": provider_chain},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
            ),
            run_scope_id="ticket-11-run",
            checkpoint_ledger=contradictory_route,
        )
    assert raised.value.reason is (
        FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
    )


def test_run_scoped_qualified_dispatch_finalizes_zero_attempts_and_rejects_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    period = date(2026, 6, 30)
    plan = _qualified_plan(monkeypatch)
    provider_chain = (
        FinancialProvider(
            name="legacy-placeholder",
            variants=(
                FinancialProviderVariant(
                    variant_id="default",
                    invoke=lambda _request: "must not be called",
                ),
            ),
        ),
    )
    selection_request = FinancialStatementRoutingRequest(
        instrument_identity=asset.instrument_identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=(),
        eligible_reporting_period_ends=(period,),
    )
    tool_request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=date(2026, 7, 30),
        tool_call_id="ticket-11-zero-attempts",
    )
    zero_attempt_dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        run_scope_id="ticket-11-zero-attempt-run",
    )

    zero_attempt_result = zero_attempt_dispatcher.dispatch_statement_selection(
        tool_request,
        selection_request,
    )

    assert zero_attempt_result.routing_result.manifest.contract_version == (
        "financial-manifest-v2"
    )
    assert zero_attempt_result.routing_result.manifest.physical_attempt_events == ()
    assert zero_attempt_result.routing_result.manifest.disposition.value == "insufficient"
    assert zero_attempt_dispatcher.checkpoint_ledger()["contract_version"] == "1.2"

    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=asset.instrument_identity,
    )
    incomplete_attempt_response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=candidate.artifact,
        retained_artifacts=(candidate.artifact,),
        candidates=(candidate,),
        subrequest_keys=("provider-subrequest:v1:" + "a" * 64,),
        physical_attempt_ids=("provider-physical-attempt=sha256:" + "b" * 64,),
    )
    incomplete_dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                "tushare": lambda _request: incomplete_attempt_response,
            },
        ),
        run_scope_id="ticket-11-incomplete-attempt-run",
    )
    with pytest.raises(FinancialDispatchCheckpointError) as raised:
        incomplete_dispatcher.dispatch_statement_selection(
            tool_request.model_copy(
                update={"tool_call_id": "ticket-11-incomplete-attempt"}
            ),
            selection_request,
        )
    assert raised.value.reason is FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID


def test_qualified_mode_rejects_legacy_checkpoint_without_plan_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    provider_chain = (
        FinancialProvider(
            name="legacy-placeholder",
            variants=(
                FinancialProviderVariant(
                    variant_id="default",
                    invoke=lambda _request: "must not be called",
                ),
            ),
        ),
    )
    legacy = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
    ).checkpoint_ledger()
    assert legacy["contract_version"] == "1.0"

    with pytest.raises(FinancialDispatchCheckpointError) as raised:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_balance_sheet": provider_chain},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=_qualified_plan(monkeypatch),
                statement_sources={},
            ),
            run_scope_id="ticket-11-qualified-resume",
            checkpoint_ledger=legacy,
        )
    assert raised.value.reason is (
        FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
    )


def test_indicator_manifest_retains_fallback_lineage_and_yahoo_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    selected = _ratio_candidate(
        provider_id="akshare",
        ratio_family=FinancialRatioFamily.PROFIT,
        period_end=_REPORTING_PERIODS[-1],
        f_ann_date=None,
        instrument_identity=asset.instrument_identity,
    )
    yahoo_artifact = _ratio_candidate(
        provider_id="yfinance",
        ratio_family=FinancialRatioFamily.PROFIT,
        period_end=_REPORTING_PERIODS[0],
        f_ann_date=None,
        instrument_identity=asset.instrument_identity,
    ).artifact
    failed_event = _attempt_event(
        ordinal=1,
        outcome=ProviderSubrequestOutcomeKind.PROVIDER_ERROR,
    )
    akshare_event = _attempt_event(ordinal=2)
    yahoo_event = _attempt_event(ordinal=3)

    def unavailable(_request):
        return FinancialProviderPeriodResponse.unavailable(
            provider_id="tushare",
            reason="provider_error",
            subrequest_keys=(failed_event.request_key,),
            physical_attempt_ids=(failed_event.attempt_event_id,),
            physical_attempt_events=(failed_event,),
        )

    def akshare(_request):
        return FinancialProviderPeriodResponse.available(
            provider_id="akshare",
            response_artifact=selected.artifact,
            retained_artifacts=(selected.artifact,),
            candidates=(selected,),
            subrequest_keys=(akshare_event.request_key,),
            physical_attempt_ids=(akshare_event.attempt_event_id,),
            physical_attempt_events=(akshare_event,),
        )

    def yahoo(_request):
        return FinancialProviderPeriodResponse.available(
            provider_id="yfinance",
            response_artifact=yahoo_artifact,
            retained_artifacts=(yahoo_artifact,),
            candidates=(),
            subrequest_keys=(yahoo_event.request_key,),
            physical_attempt_ids=(yahoo_event.attempt_event_id,),
            physical_attempt_events=(yahoo_event,),
            degraded_current_profile=True,
        )

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            "tushare": unavailable,
            "akshare": akshare,
            "yfinance": yahoo,
        },
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=asset.instrument_identity,
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=_REPORTING_PERIODS,
            remaining_evidence_decision_ready=True,
        )
    )

    manifest = result.family_manifests[0]
    rendered = render_indicator_routing_result(result)
    assert manifest.contract_version == "financial-manifest-v2"
    assert manifest.disposition.value == "degraded"
    assert manifest.physical_attempt_events == tuple(
        sorted(
            (failed_event, akshare_event, yahoo_event),
            key=lambda item: item.attempt_event_id,
        )
    )
    assert {
        item.artifact.artifact_identity for item in manifest.artifacts
    } == {
        selected.artifact.artifact_identity,
        yahoo_artifact.artifact_identity,
    }
    assert manifest.final_rendered_artifact is not None
    assert manifest.final_rendered_artifact.content_sha256 == sha256(
        rendered.encode("utf-8")
    ).hexdigest()

    insufficient = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            "tushare": unavailable,
            "akshare": akshare,
            "yfinance": yahoo,
        },
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=asset.instrument_identity,
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=_REPORTING_PERIODS,
            remaining_evidence_decision_ready=False,
        )
    )
    assert insufficient.family_manifests[0].disposition.value == "insufficient"


def test_v2_audit_and_report_expose_safe_deterministic_completeness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    period = date(2026, 6, 30)
    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=asset.instrument_identity,
    )
    event = _attempt_event()
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=candidate.artifact,
        retained_artifacts=(candidate.artifact,),
        candidates=(candidate,),
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    plan = _qualified_plan(monkeypatch)
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_balance_sheet": (
                FinancialProvider(
                    name="legacy-placeholder",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="default",
                            invoke=lambda _request: "must not be called",
                        ),
                    ),
                ),
            )
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": lambda _request: response},
        ),
        run_scope_id="ticket-11-audit",
    )
    dispatch = dispatcher.dispatch_statement_selection(
        FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 30),
            tool_call_id="ticket-11-audit-call",
        ),
        FinancialStatementRoutingRequest(
            instrument_identity=asset.instrument_identity,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(period,),
            listing_date=date(2026, 1, 15),
            listing_provenance=FinancialListingProvenance(
                provider_id="baostock",
                source_ref="issuer_lifecycle",
                observed_at=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc),
            ),
        ),
    )

    audit = project_financial_dispatch_ledger(
        dispatcher.checkpoint_ledger(),
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-11-audit",
    )

    assert audit is not None
    assert audit.contract_version == "2.0"
    assert audit.capability_routing_plan_signature == plan.plan_signature
    assert audit.financial_manifest_version == "financial-manifest-v2"
    assert audit.request_count == 1
    assert audit.logical_provider_count == 1
    assert audit.logical_candidate_count == 1
    assert audit.acquisition_attempt_count == 1
    assert len(audit.manifest_requests) == 1
    assert len(audit.completeness_rows) == 1
    row = audit.completeness_rows[0]
    assert row.capability.value == "financial_statement"
    assert row.statement_or_ratio_family == "balance_sheet"
    assert row.period == period
    assert row.provider == "tushare"
    assert row.company_type.value == "industrial/non-bank"
    assert row.metadata_coverage == 1
    assert row.ann_date == candidate.filing_metadata.ann_date
    assert row.f_ann_date == candidate.filing_metadata.f_ann_date
    assert row.report_type == candidate.filing_metadata.report_type
    assert row.provider_comp_type == candidate.filing_metadata.comp_type
    assert row.update_flag == candidate.filing_metadata.update_flag
    assert row.revision_observed_at == candidate.filing_metadata.observed_at
    assert (
        row.local_provider_revision_identity
        == candidate.filing_metadata.local_provider_revision_identity
    )
    assert row.critical_coverage == 1
    assert row.core_coverage == 1
    assert row.core_threshold == Decimal("0.9")
    assert row.disposition == "selected"
    assert row.pit_eligible is True
    assert row.artifact_identity == candidate.artifact.artifact_identity
    assert row.provider_attempt_count == 1
    assert row.since_listing_exception is True
    assert row.listing_date == date(2026, 1, 15)
    assert row.listing_provider == "baostock"
    assert row.listing_source_ref == "issuer_lifecycle"

    unsafe_row = row.model_dump(mode="json")
    unsafe_row["report_type"] = (
        "https://provider.invalid/path?symbol=600895.SS"
    )
    with pytest.raises(ValueError, match="URL query"):
        type(row).model_validate(unsafe_row)

    tool_envelope = dispatch.to_tool_message().artifact
    checkpoint = dispatcher.checkpoint_ledger()
    for unsafe_projection_value in (
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "RuntimeError: provider failure detail",
        "https://provider.invalid/path?credential=leak",
    ):
        unsafe_audit = audit.model_dump(mode="json")
        unsafe_audit["completeness_rows"][0]["report_type"] = (
            unsafe_projection_value
        )
        with pytest.raises(ValueError):
            type(audit).model_validate(unsafe_audit)

        unsafe_tool_envelope = copy.deepcopy(tool_envelope)
        unsafe_tool_envelope["completeness_rows"][0]["report_type"] = (
            unsafe_projection_value
        )
        with pytest.raises(ValueError):
            FinancialToolMessageManifestEnvelope.model_validate(
                unsafe_tool_envelope
            )

        unsafe_checkpoint = copy.deepcopy(checkpoint)
        unsafe_checkpoint["qualified_statement_selections"][0]["terminal"][
            "routing_result"
        ]["manifest"]["provider_candidates"][0]["candidate"][
            "filing_metadata"
        ]["report_type"] = unsafe_projection_value
        with pytest.raises(FinancialDispatchCheckpointError) as raised:
            FinancialToolDispatcher(
                instrument_identity=asset.instrument_identity,
                run_asset_configuration=asset,
                provider_chains={
                    "get_balance_sheet": (
                        FinancialProvider(
                            name="legacy-placeholder",
                            variants=(
                                FinancialProviderVariant(
                                    variant_id="default",
                                    invoke=lambda _request: "must not be called",
                                ),
                            ),
                        ),
                    )
                },
                qualified_statement_router=MainlandFinancialCapabilityRouter(
                    routing_plan=plan,
                    statement_sources={},
                ),
                run_scope_id="ticket-11-audit",
                checkpoint_ledger=unsafe_checkpoint,
            )
        assert raised.value.reason is (
            FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID
        )

    report = "\n".join(_render_financial_dispatch_operations(audit))
    assert "Financial manifest version:** financial-manifest-v2" in report
    assert "Physical request count:** 1" in report
    assert "| Capability | Family | Period | Provider |" in report
    assert candidate.artifact.artifact_identity in report
    assert event.attempt_event_id in report
    assert "2026-07-02" in report
    assert "Since-listing exception" in report
    assert "2026-01-15" in report
    assert "baostock" in report
    assert "issuer_lifecycle" in report
    assert "provider failure detail" not in report
    assert "credential=leak" not in report


def test_v2_tool_envelope_and_evidence_admit_only_selected_strict_pit_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    strict_period = date(2026, 6, 30)
    current_period = date(2026, 3, 31)
    strict_candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=strict_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=asset.instrument_identity,
    )
    current_candidate = _candidate(
        provider_id="akshare_sina",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=current_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        f_ann_date=None,
        instrument_identity=asset.instrument_identity,
    )
    strict_event = _attempt_event(ordinal=1)
    current_event = _attempt_event(ordinal=2)
    responses = {
        "tushare": FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=strict_candidate.artifact,
            retained_artifacts=(strict_candidate.artifact,),
            candidates=(strict_candidate,),
            subrequest_keys=(strict_event.request_key,),
            physical_attempt_ids=(strict_event.attempt_event_id,),
            physical_attempt_events=(strict_event,),
        ),
        "akshare_sina": FinancialProviderPeriodResponse.available(
            provider_id="akshare_sina",
            response_artifact=current_candidate.artifact,
            retained_artifacts=(current_candidate.artifact,),
            candidates=(current_candidate,),
            subrequest_keys=(current_event.request_key,),
            physical_attempt_ids=(current_event.attempt_event_id,),
            physical_attempt_events=(current_event,),
        ),
    }
    plan = _qualified_plan(monkeypatch)
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_balance_sheet": (
                FinancialProvider(
                    name="legacy-placeholder",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="default",
                            invoke=lambda _request: "must not be called",
                        ),
                    ),
                ),
            )
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                provider: (
                    lambda _request, response=response: response
                )
                for provider, response in responses.items()
            },
        ),
        run_scope_id="ticket-11-evidence",
    )
    dispatch = dispatcher.dispatch_statement_selection(
        FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 30),
            tool_call_id="ticket-11-evidence-call",
        ),
        FinancialStatementRoutingRequest(
            instrument_identity=asset.instrument_identity,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(strict_period, current_period),
        ),
    )

    message = dispatch.to_tool_message()
    envelope = message.artifact
    assert envelope["contract_version"] == "financial-tool-message-envelope-v2"
    assert envelope["capability_routing_plan_signature"] == plan.plan_signature
    assert envelope["financial_manifest_version"] == "financial-manifest-v2"
    assert envelope["physical_request_count"] == 2
    assert envelope["logical_candidate_count"] == 2
    assert envelope["selected_artifact"]["raw_text"] == message.content
    assert envelope["selected_artifact"]["artifact_sha256"] == sha256(
        message.content.encode("utf-8")
    ).hexdigest()
    assert set(envelope["manifests"][0]["artifact_identities"]) == {
        strict_candidate.artifact.artifact_identity,
        current_candidate.artifact.artifact_identity,
    }
    current_row = next(
        row
        for row in envelope["completeness_rows"]
        if row["period"] == current_period.isoformat()
    )
    assert Decimal(str(current_row["metadata_coverage"])) == Decimal("0.8")
    assert current_row["f_ann_date"] is None
    envelope_text = str(envelope).casefold()
    for forbidden in (
        "retry_after",
        "pacing_wait",
        "cooldown",
        "upstream_service",
        "request_key",
    ):
        assert forbidden not in envelope_text

    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=asset.instrument_identity),
        dispatch,
    )
    assert evidence.physical_attempt_count == 2
    assert {
        event.attempt_event_id for event in evidence.physical_attempt_events
    } == {
        strict_event.attempt_event_id,
        current_event.attempt_event_id,
    }
    assert len(evidence.source_artifacts) == 1
    assert len(evidence.source_facts) == len(strict_candidate.fields)
    assert {
        fact.effective_date for fact in evidence.source_facts
    } == {strict_period.isoformat()}
    assert all(fact.fact_kind == "canonical" for fact in evidence.source_facts)
    assert all(
        fact.canonical_field
        in {field.normalized_field for field in strict_candidate.fields}
        for fact in evidence.source_facts
    )
    assert current_period.isoformat() not in {
        fact.effective_date for fact in evidence.source_facts
    }


def test_fully_rejected_artifact_remains_operational_and_creates_no_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    period = date(2026, 6, 30)
    rejected_candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        ann_date=None,
        f_ann_date=None,
        instrument_identity=asset.instrument_identity,
    )
    event = _attempt_event(ordinal=4)
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=rejected_candidate.artifact,
        retained_artifacts=(rejected_candidate.artifact,),
        candidates=(rejected_candidate,),
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    plan = _qualified_plan(monkeypatch)
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_balance_sheet": (
                FinancialProvider(
                    name="legacy-placeholder",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="default",
                            invoke=lambda _request: "must not be called",
                        ),
                    ),
                ),
            )
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": lambda _request: response},
        ),
        run_scope_id="ticket-11-rejected",
    )
    dispatch = dispatcher.dispatch_statement_selection(
        FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 30),
            tool_call_id="ticket-11-rejected-call",
        ),
        FinancialStatementRoutingRequest(
            instrument_identity=asset.instrument_identity,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(period,),
        ),
    )

    manifest = dispatch.routing_result.manifest
    assert manifest.disposition.value == "insufficient"
    assert {
        item.artifact.artifact_identity for item in manifest.artifacts
    } == {rejected_candidate.artifact.artifact_identity}
    assert not manifest.selections
    assert len(manifest.rejected_periods) == 1
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA in (
        manifest.rejected_periods[0].reasons
    )
    checkpoint_text = str(dispatcher.checkpoint_ledger())
    assert rejected_candidate.artifact.artifact_identity in checkpoint_text
    assert rejected_candidate.candidate_identity in checkpoint_text

    audit = project_financial_dispatch_ledger(
        dispatcher.checkpoint_ledger(),
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-11-rejected",
    )
    assert audit is not None
    rejected_row = next(
        row for row in audit.completeness_rows if row.provider == "tushare"
    )
    assert rejected_row.disposition == "rejected"
    assert "incompatible_metadata" in rejected_row.typed_reason
    assert (
        rejected_row.artifact_identity
        == rejected_candidate.artifact.artifact_identity
    )

    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=asset.instrument_identity),
        dispatch,
    )
    assert evidence.physical_attempt_count == 1
    assert not evidence.source_artifacts
    assert not evidence.source_facts


def test_critical_conflict_is_period_local_in_audit_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    conflict_period = date(2026, 6, 30)
    usable_period = date(2026, 3, 31)
    missing_period = date(2025, 12, 31)
    first_conflict = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=conflict_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        value="1",
        instrument_identity=asset.instrument_identity,
    )
    independent = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=usable_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        value="3",
        instrument_identity=asset.instrument_identity,
    )
    conflicting = _candidate(
        provider_id="akshare_sina",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=conflict_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        value="2",
        instrument_identity=asset.instrument_identity,
    )
    tushare_event = _attempt_event(ordinal=5)
    akshare_event = _attempt_event(ordinal=6)
    responses = {
        "tushare": FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=first_conflict.artifact,
            retained_artifacts=(
                first_conflict.artifact,
                independent.artifact,
            ),
            candidates=(first_conflict, independent),
            subrequest_keys=(tushare_event.request_key,),
            physical_attempt_ids=(tushare_event.attempt_event_id,),
            physical_attempt_events=(tushare_event,),
        ),
        "akshare_sina": FinancialProviderPeriodResponse.available(
            provider_id="akshare_sina",
            response_artifact=conflicting.artifact,
            retained_artifacts=(conflicting.artifact,),
            candidates=(conflicting,),
            subrequest_keys=(akshare_event.request_key,),
            physical_attempt_ids=(akshare_event.attempt_event_id,),
            physical_attempt_events=(akshare_event,),
        ),
    }
    plan = _qualified_plan(monkeypatch)
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_balance_sheet": (
                FinancialProvider(
                    name="legacy-placeholder",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="default",
                            invoke=lambda _request: "must not be called",
                        ),
                    ),
                ),
            )
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                provider: (
                    lambda _request, response=response: response
                )
                for provider, response in responses.items()
            },
        ),
        run_scope_id="ticket-11-conflict",
    )
    dispatch = dispatcher.dispatch_statement_selection(
        FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 30),
            tool_call_id="ticket-11-conflict-call",
        ),
        FinancialStatementRoutingRequest(
            instrument_identity=asset.instrument_identity,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(
                usable_period,
                conflict_period,
                missing_period,
            ),
        ),
    )

    assert dispatch.routing_result.manifest.disposition.value == "conflicted"
    assert dispatch.routing_result.conflicted_reporting_period_ends == (
        conflict_period,
    )
    assert {
        candidate.period_identity.period_end
        for candidate in dispatch.routing_result.selected_candidates
    } == {usable_period}

    audit = project_financial_dispatch_ledger(
        dispatcher.checkpoint_ledger(),
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-11-conflict",
    )
    assert audit is not None
    conflict_rows = tuple(
        row
        for row in audit.completeness_rows
        if row.period == conflict_period and row.provider != "none"
    )
    assert len(conflict_rows) == 2
    assert {row.disposition for row in conflict_rows} == {"conflicted"}
    independent_row = next(
        row
        for row in audit.completeness_rows
        if row.period == usable_period
    )
    assert independent_row.disposition == "selected"
    assert independent_row.pit_eligible is True

    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=asset.instrument_identity),
        dispatch,
    )
    assert {
        fact.effective_date for fact in evidence.source_facts
    } == {usable_period.isoformat()}
    assert len(evidence.source_facts) == len(independent.fields)


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
        "https://provider.invalid/path?credential=leak",
        "RuntimeError: provider failure detail",
        "TimeoutError: provider timed out",
    ),
)
def test_token_like_and_raw_operational_values_are_rejected_from_contracts(
    unsafe_value: str,
) -> None:
    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=date(2026, 6, 30),
        frequency=FinancialReportingFrequency.QUARTERLY,
    )
    with pytest.raises(ValueError, match="unsafe operational data"):
        FinancialProviderArtifactIdentity.create(
            dataset=candidate.artifact.dataset,
            canonical_request={"symbol": "600895.SS"},
            provider_metadata={"fixture": "ticket-11"},
            retrieved_at=candidate.artifact.retrieved_at,
            observed_at=candidate.artifact.observed_at,
            payload={"value": unsafe_value},
        )


def test_complete_statement_families_retain_uncited_lineage_through_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    statement_tools = (
        (FinancialStatementType.BALANCE_SHEET, "get_balance_sheet"),
        (FinancialStatementType.INCOME_STATEMENT, "get_income_statement"),
        (FinancialStatementType.CASH_FLOW, "get_cashflow"),
    )
    responses: dict[
        FinancialStatementType,
        FinancialProviderPeriodResponse,
    ] = {}
    expected_artifact_ids: set[str] = set()
    expected_candidate_ids: set[str] = set()
    for ordinal, (statement_type, _tool_name) in enumerate(
        statement_tools,
        start=10,
    ):
        candidates = tuple(
            _candidate(
                provider_id="tushare",
                statement_type=statement_type,
                period_end=period,
                frequency=FinancialReportingFrequency.ANNUAL,
                instrument_identity=asset.instrument_identity,
            )
            for period in _ANNUAL_PERIODS
        ) + tuple(
            _candidate(
                provider_id="tushare",
                statement_type=statement_type,
                period_end=period,
                frequency=FinancialReportingFrequency.QUARTERLY,
                instrument_identity=asset.instrument_identity,
            )
            for period in _REPORTING_PERIODS
        )
        event = _attempt_event(ordinal=ordinal)
        responses[statement_type] = FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=candidates[0].artifact,
            retained_artifacts=tuple(
                candidate.artifact for candidate in candidates
            ),
            candidates=candidates,
            subrequest_keys=(event.request_key,),
            physical_attempt_ids=(event.attempt_event_id,),
            physical_attempt_events=(event,),
        )
        expected_artifact_ids.update(
            candidate.artifact.artifact_identity for candidate in candidates
        )
        expected_candidate_ids.update(
            candidate.candidate_identity for candidate in candidates
        )

    provider_chains = {
        tool_name: (
            FinancialProvider(
                name="legacy-placeholder",
                variants=(
                    FinancialProviderVariant(
                        variant_id="default",
                        invoke=lambda _request: "must not be called",
                    ),
                ),
            ),
        )
        for _statement_type, tool_name in statement_tools
    }
    plan = _qualified_plan(monkeypatch)
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains=provider_chains,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                "tushare": (
                    lambda request: responses[request.statement_type]
                )
            },
        ),
        run_scope_id="ticket-11-complete-statements",
    )

    for statement_type, tool_name in statement_tools:
        result = dispatcher.dispatch_statement_selection(
            FinancialToolRequest(
                tool_name=tool_name,
                statement_type=statement_type,
                frequency=FinancialReportingFrequency.QUARTERLY,
                as_of_date=date(2026, 7, 30),
                tool_call_id=f"ticket-11-{statement_type.value}",
            ),
            FinancialStatementRoutingRequest(
                instrument_identity=asset.instrument_identity,
                statement_type=statement_type,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=_ANNUAL_PERIODS,
                eligible_reporting_period_ends=_REPORTING_PERIODS,
            ),
        )
        assert result.routing_result.manifest.disposition.value == (
            "strict_pit_eligible"
        )
        assert len(result.routing_result.manifest.selections) == 13

    checkpoint = dispatcher.checkpoint_ledger()
    checkpoint_text = str(checkpoint)
    assert checkpoint["contract_version"] == "1.2"
    assert len(checkpoint["qualified_statement_selections"]) == 3
    assert all(
        artifact_id in checkpoint_text for artifact_id in expected_artifact_ids
    )
    assert all(
        candidate_id in checkpoint_text for candidate_id in expected_candidate_ids
    )

    audit = project_financial_dispatch_ledger(
        checkpoint,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-11-complete-statements",
    )
    assert audit is not None
    assert audit.request_count == 3
    assert audit.logical_provider_count == 3
    assert audit.logical_candidate_count == 39
    assert audit.acquisition_attempt_count == 3
    assert len(audit.manifest_requests) == 3
    assert len(audit.completeness_rows) == 39
    assert {row.disposition for row in audit.completeness_rows} == {"selected"}
    audit_artifact_ids = {
        artifact_id
        for request in audit.manifest_requests
        for manifest in request.manifests
        for artifact_id in manifest.artifact_identities
    }
    assert audit_artifact_ids == expected_artifact_ids

    report = "\n".join(_render_financial_dispatch_operations(audit))
    assert "Physical request count:** 3" in report
    assert "Logical candidate count:** 39" in report
    assert all(
        statement_type.value in report
        for statement_type, _tool_name in statement_tools
    )
    assert all(artifact_id in report for artifact_id in expected_artifact_ids)


def test_equivalent_overlap_is_unselected_without_duplicate_source_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    selected_period = date(2026, 6, 30)
    fallback_period = date(2026, 3, 31)
    selected = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=selected_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        value="1",
        instrument_identity=asset.instrument_identity,
    )
    equivalent = _candidate(
        provider_id="akshare_sina",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=selected_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        value="1",
        instrument_identity=asset.instrument_identity,
    )
    fallback = _candidate(
        provider_id="akshare_sina",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=fallback_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        value="2",
        instrument_identity=asset.instrument_identity,
    )
    first_event = _attempt_event(ordinal=20)
    fallback_event = _attempt_event(ordinal=21)
    responses = {
        "tushare": FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=selected.artifact,
            retained_artifacts=(selected.artifact,),
            candidates=(selected,),
            subrequest_keys=(first_event.request_key,),
            physical_attempt_ids=(first_event.attempt_event_id,),
            physical_attempt_events=(first_event,),
        ),
        "akshare_sina": FinancialProviderPeriodResponse.available(
            provider_id="akshare_sina",
            response_artifact=fallback.artifact,
            retained_artifacts=(equivalent.artifact, fallback.artifact),
            candidates=(equivalent, fallback),
            subrequest_keys=(fallback_event.request_key,),
            physical_attempt_ids=(fallback_event.attempt_event_id,),
            physical_attempt_events=(fallback_event,),
        ),
    }
    plan = _qualified_plan(monkeypatch)
    provider_chain = (
        FinancialProvider(
            name="legacy-placeholder",
            variants=(
                FinancialProviderVariant(
                    variant_id="default",
                    invoke=lambda _request: "must not be called",
                ),
            ),
        ),
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                provider: (
                    lambda _request, response=response: response
                )
                for provider, response in responses.items()
            },
        ),
        run_scope_id="ticket-11-equivalent-overlap",
    )
    dispatch = dispatcher.dispatch_statement_selection(
        FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 30),
            tool_call_id="ticket-11-equivalent-overlap-call",
        ),
        FinancialStatementRoutingRequest(
            instrument_identity=asset.instrument_identity,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(
                selected_period,
                fallback_period,
            ),
        ),
    )

    manifest = dispatch.routing_result.manifest
    assert len(manifest.overlaps) == 1
    assert manifest.overlaps[0].disposition == "equivalent_unselected"
    assert manifest.overlaps[0].overlapping_candidate_identity == (
        equivalent.candidate_identity
    )
    assert {
        selection.candidate_identity for selection in manifest.selections
    } == {selected.candidate_identity, fallback.candidate_identity}

    checkpoint = dispatcher.checkpoint_ledger()
    checkpoint_text = str(checkpoint)
    assert equivalent.artifact.artifact_identity in checkpoint_text
    assert equivalent.candidate_identity in checkpoint_text
    audit = project_financial_dispatch_ledger(
        checkpoint,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-11-equivalent-overlap",
    )
    assert audit is not None
    overlap_rows = tuple(
        row
        for row in audit.completeness_rows
        if row.period == selected_period
    )
    assert {
        (row.provider, row.disposition)
        for row in overlap_rows
    } == {
        ("tushare", "selected"),
        ("akshare_sina", "unselected"),
    }

    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=asset.instrument_identity),
        dispatch,
    )
    expected_fact_count = len(selected.fields) + len(fallback.fields)
    assert len(evidence.source_facts) == expected_fact_count
    assert {
        fact.effective_date for fact in evidence.source_facts
    } == {selected_period.isoformat(), fallback_period.isoformat()}


def test_physical_attempt_totals_derive_from_all_coordinator_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=date(2026, 6, 30),
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=asset.instrument_identity,
    )
    attempt_events = (
        _attempt_event(ordinal=30),
        _attempt_event(ordinal=31),
    )
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=candidate.artifact,
        retained_artifacts=(candidate.artifact,),
        candidates=(candidate,),
        subrequest_keys=tuple(event.request_key for event in attempt_events),
        physical_attempt_ids=tuple(
            event.attempt_event_id for event in attempt_events
        ),
        physical_attempt_events=attempt_events,
    )
    plan = _qualified_plan(monkeypatch)
    provider_chain = (
        FinancialProvider(
            name="legacy-placeholder",
            variants=(
                FinancialProviderVariant(
                    variant_id="default",
                    invoke=lambda _request: "must not be called",
                ),
            ),
        ),
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": lambda _request: response},
        ),
        run_scope_id="ticket-11-attempt-count",
    )
    dispatch = dispatcher.dispatch_statement_selection(
        FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 30),
            tool_call_id="ticket-11-attempt-count-call",
        ),
        FinancialStatementRoutingRequest(
            instrument_identity=asset.instrument_identity,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(date(2026, 6, 30),),
        ),
    )

    audit = project_financial_dispatch_ledger(
        dispatcher.checkpoint_ledger(),
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-11-attempt-count",
    )
    assert audit is not None
    assert audit.logical_provider_count == 1
    assert audit.logical_candidate_count == 1
    assert audit.acquisition_attempt_count == 2
    assert audit.manifest_requests[0].physical_request_count == 2
    assert audit.completeness_rows[0].provider_attempt_count == 2
    assert dispatch.to_tool_message().artifact["physical_request_count"] == 2
    assert "Physical request count:** 2" in "\n".join(
        _render_financial_dispatch_operations(audit)
    )


def test_checkpoint_replay_admits_only_observed_strict_pit_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    strict_period = date(2026, 6, 30)
    current_period = date(2026, 3, 31)
    strict_candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=strict_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=asset.instrument_identity,
    )
    current_candidate = _candidate(
        provider_id="akshare_sina",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=current_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        f_ann_date=None,
        instrument_identity=asset.instrument_identity,
    )
    strict_event = _attempt_event(ordinal=40)
    current_event = _attempt_event(ordinal=41)
    responses = {
        "tushare": FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=strict_candidate.artifact,
            retained_artifacts=(strict_candidate.artifact,),
            candidates=(strict_candidate,),
            subrequest_keys=(strict_event.request_key,),
            physical_attempt_ids=(strict_event.attempt_event_id,),
            physical_attempt_events=(strict_event,),
        ),
        "akshare_sina": FinancialProviderPeriodResponse.available(
            provider_id="akshare_sina",
            response_artifact=current_candidate.artifact,
            retained_artifacts=(current_candidate.artifact,),
            candidates=(current_candidate,),
            subrequest_keys=(current_event.request_key,),
            physical_attempt_ids=(current_event.attempt_event_id,),
            physical_attempt_events=(current_event,),
        ),
    }
    plan = _qualified_plan(monkeypatch)
    provider_chain = (
        FinancialProvider(
            name="legacy-placeholder",
            variants=(
                FinancialProviderVariant(
                    variant_id="default",
                    invoke=lambda _request: "must not be called",
                ),
            ),
        ),
    )
    selection_request = FinancialStatementRoutingRequest(
        instrument_identity=asset.instrument_identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=(),
        eligible_reporting_period_ends=(strict_period, current_period),
    )
    tool_request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=date(2026, 7, 30),
        tool_call_id="ticket-11-replay-first",
    )
    original = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                provider: (
                    lambda _request, response=response: response
                )
                for provider, response in responses.items()
            },
        ),
        run_scope_id="ticket-11-replay",
    )
    original.dispatch_statement_selection(tool_request, selection_request)
    checkpoint = original.checkpoint_ledger()

    restored = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                "tushare": lambda _request: pytest.fail(
                    "strict replay repeated provider I/O"
                ),
                "akshare_sina": lambda _request: pytest.fail(
                    "strict replay repeated provider I/O"
                ),
            },
        ),
        run_scope_id="ticket-11-replay",
        checkpoint_ledger=checkpoint,
    )
    replay = restored.dispatch_statement_selection(
        tool_request.model_copy(
            update={"tool_call_id": "ticket-11-replay-restored"}
        ),
        selection_request,
    )
    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=asset.instrument_identity),
        replay,
    )

    assert {
        fact.effective_date for fact in evidence.source_facts
    } == {strict_period.isoformat()}
    assert len(evidence.source_facts) == len(strict_candidate.fields)
    audit = project_financial_dispatch_ledger(
        checkpoint,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-11-replay",
    )
    assert audit is not None
    current_row = next(
        row
        for row in audit.completeness_rows
        if row.period == current_period
    )
    assert current_row.disposition == "current_only"
    assert current_row.pit_eligible is False


def test_graph_persists_v2_manifest_and_evidence_without_repeating_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    period = date(2026, 6, 30)
    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=asset.instrument_identity,
    )
    event = _attempt_event(ordinal=7)
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=candidate.artifact,
        retained_artifacts=(candidate.artifact,),
        candidates=(candidate,),
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    calls = 0

    def source(_request):
        nonlocal calls
        calls += 1
        return response

    plan = _qualified_plan(monkeypatch)
    selection_request = FinancialStatementRoutingRequest(
        instrument_identity=asset.instrument_identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=(),
        eligible_reporting_period_ends=(period,),
    )
    unused = lambda *_args, **_kwargs: "must not be called"  # noqa: E731
    vendor_methods = {
        tool_name: {"legacy-placeholder": unused}
        for tool_name in (
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        )
    }
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(
        vendor_methods,
        "legacy-placeholder",
    )
    runtime_config["mainland_capability_routing_mode"] = "qualified_v1"
    node = FinancialDispatchToolNode(
        config=runtime_config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": source},
        ),
        qualified_statement_request_factory=lambda _request: selection_request,
    )
    call = {
        "name": "get_balance_sheet",
        "args": {
            "ticker": asset.instrument_identity.symbol,
            "freq": "quarterly",
            "curr_date": "2026-07-30",
        },
        "id": "ticket-11-graph-call",
        "type": "tool_call",
    }
    state = {
        "messages": [AIMessage(content="", tool_calls=[call])],
        "evidence_state": EvidenceState(
            instrument_identity=asset.instrument_identity
        ).model_dump(mode="json"),
        "asset_configuration": asset.model_dump(mode="json"),
        "trade_date": "2026-07-30",
        "run_id": "ticket-11-graph",
    }

    first = node(state)
    restored = node(
        {
            **state,
            "evidence_state": first["evidence_state"],
            "financial_dispatch_ledger": first[
                "financial_dispatch_ledger"
            ],
        }
    )

    assert calls == 1
    message = next(
        item for item in first["messages"] if isinstance(item, ToolMessage)
    )
    assert message.artifact["contract_version"] == (
        "financial-tool-message-envelope-v2"
    )
    assert first["financial_dispatch_ledger"]["contract_version"] == "1.2"
    assert first["financial_dispatch_ledger"]["run_scope_id"] == (
        "ticket-11-graph"
    )
    evidence = EvidenceState.model_validate(first["evidence_state"])
    assert evidence.physical_attempt_count == 1
    assert len(evidence.source_facts) == len(candidate.fields)
    assert (
        restored["financial_dispatch_ledger"][
            "qualified_statement_selections"
        ][0]["terminal"]["routing_result"]
        == first["financial_dispatch_ledger"][
            "qualified_statement_selections"
        ][0]["terminal"]["routing_result"]
    )

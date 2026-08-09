from __future__ import annotations

import copy
import importlib.util
import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from types import MappingProxyType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingRunProjection,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.acquisition import (
    AcquisitionUnavailableReason,
    RetryPolicy,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialProviderGapRequest,
    FinancialProviderPeriodResponse,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2,
    FinancialAvailabilityScope,
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialListingProvenance,
    FinancialPeriodCandidate,
    FinancialPeriodIdentity,
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
    FinancialReportingFrequency,
    FinancialStatementType,
    financial_statement_field_declaration,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeEvidenceBinding,
    FinancialEligibilityRequestBinding,
    build_financial_eligibility_acquisition_context,
    derive_mainland_financial_acquisition_period_supersets,
    financial_eligibility_acquisition_context_identity,
)
from tradingagents.dataflows.financial_request_factory import (
    build_financial_statement_routing_request,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestKey,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.graph.financial_tools import (
    FinancialDispatchToolNode,
    QualifiedFinancialRoutingComposition,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.market_history.config import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
)
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    RequestPriority,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.store import MarketHistoryStore
from tradingagents.qualified_routing_composition import (
    PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    ProductionQualifiedRoutingRunBinding,
    build_production_qualified_routing_composition_blueprint,
    build_registered_production_qualified_routing_composition,
    production_qualified_routing_runtime_factories,
)

_NOW = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
_CHECKPOINT_THREAD_ID = "0123456789abcdef"


def _checkpoint_run_id(checkpoint_thread_id: str) -> str:
    return "run:" + sha256(f"checkpoint:{checkpoint_thread_id}".encode()).hexdigest()


def _blueprint(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
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
            "market_history_payload_root": str(tmp_path / "market-history" / "payloads"),
            "market_history_backup_root": str(tmp_path / "market-history" / "backups"),
            "results_dir": str(tmp_path / "results"),
        }
    )
    asset = resolve_run_asset_configuration("601328.SS", config=config)
    preflight = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "deterministic-fixture-only"},
    )
    assert preflight.plan is not None
    plan = preflight.plan
    config["asset_configuration_signature"] = asset.asset_configuration_signature
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    blueprint = build_production_qualified_routing_composition_blueprint(
        plan=plan,
        asset_configuration=asset,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        config=config,
        validation_workload=True,
        disposition=MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE,
        environment={"TUSHARE_TOKEN": "deterministic-fixture-only"},
        dependency_availability={
            "akshare": True,
            "baostock": True,
            "tushare": True,
            "yfinance": True,
        },
        provider_source_coverage={
            "akshare": True,
            "akshare_sina": True,
            "baostock": True,
            "baostock_qualified_families": True,
            "tushare": True,
            "yfinance": True,
            "yfinance_derived": True,
        },
        factory_contract_version=(PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION),
    )
    return blueprint, asset, plan


def _history_config(blueprint) -> MarketHistoryConfig:
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=blueprint.database_path,
        payload_root=blueprint.provider_artifact_root_path,
        backup_root=blueprint.backup_root_path,
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _bulk_statement_candidates(asset):
    periods = (
        (date(2025, 12, 31), FinancialReportingFrequency.ANNUAL, date(2026, 4, 1)),
        (date(2026, 6, 30), FinancialReportingFrequency.QUARTERLY, date(2026, 7, 20)),
    )
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id="tushare",
        endpoint_id="balancesheet",
        dataset_id="single_stock_history",
        schema_identity="tushare-balancesheet-bulk-fixture-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": asset.instrument_identity.symbol,
            "range_start": periods[0][0].isoformat(),
            "range_end": periods[-1][0].isoformat(),
        },
        provider_metadata={"report_type": "1", "comp_type": "1"},
        retrieved_at=_NOW,
        observed_at=_NOW,
        payload={"period_ends": [period[0].isoformat() for period in periods]},
    )
    declaration = financial_statement_field_declaration(
        FinancialCompanyType.INDUSTRIAL_NON_BANK,
        FinancialStatementType.BALANCE_SHEET,
        declaration_version=FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2,
    )
    fields = tuple(
        FinancialFieldValue(
            provider_field=field_name,
            original_value="1",
            original_unit=declaration.normalized_unit,
            normalized_field=field_name,
            normalized_value="1",
            normalized_unit=declaration.normalized_unit,
        )
        for field_name in declaration.core_fields
    )
    resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        provider_declaration_qualified=True,
        classifier_metadata={},
        present_fields=declaration.core_fields,
    )
    candidates = tuple(
        FinancialPeriodCandidate.create(
            artifact=artifact,
            period_identity=FinancialPeriodIdentity.create(
                instrument_identity=asset.instrument_identity,
                capability=FinancialCapability.STATEMENT,
                statement_type=FinancialStatementType.BALANCE_SHEET,
                ratio_family=None,
                period_end=period_end,
                frequency=frequency,
                company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                provider_revision_binding=artifact.artifact_identity,
            ),
            filing_metadata=FinancialFilingMetadata(
                ann_date=first_publication_date - timedelta(days=1),
                f_ann_date=first_publication_date,
                report_type="1",
                comp_type="1",
                update_flag="0",
                retrieved_at=_NOW,
                observed_at=_NOW,
                local_provider_revision_identity=artifact.artifact_identity,
            ),
            company_type_resolution=resolution,
            fields=fields,
        )
        for period_end, frequency, first_publication_date in periods
    )
    return artifact, candidates


@dataclass
class _Client:
    name: str
    events: list[str]

    def interrupt(self) -> None:
        self.events.append(f"interrupt:{self.name}")

    def close(self) -> None:
        self.events.append(f"close:{self.name}")


def test_factory_restores_terminal_cache_and_dispatcher_before_provider_clients(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint, asset, plan = _blueprint(tmp_path, monkeypatch)
    run_id = _checkpoint_run_id(_CHECKPOINT_THREAD_ID)
    legacy_provider_calls = 0
    qualified_source_calls = 0

    def legacy_balance_sheet(
        symbol: str,
        frequency: str,
        current_date: str,
    ) -> str:
        nonlocal legacy_provider_calls
        legacy_provider_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            f"# Data retrieved on: {current_date} 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": legacy_balance_sheet},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    qualified_config = copy.deepcopy(DEFAULT_CONFIG)
    qualified_config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    legacy_config = copy.deepcopy(qualified_config)
    legacy_config["mainland_capability_routing_mode"] = "legacy"
    legacy_config.pop("mainland_capability_routing_plan_signature")
    company_type_resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        provider_declaration_qualified=True,
        classifier_metadata={"industry": "industrial"},
        present_fields=("accounts_receivable", "inventory"),
    )
    company_type_evidence = FinancialCompanyTypeEvidenceBinding.create(
        provider_id="tushare",
        artifact_identity=(
            "provider-artifact:v1:"
            + sha256(b"factory-restore-company-type").hexdigest()
        ),
        source_ref="acq.v1:financial.company-type:factory-restore",
        observed_at=_NOW,
        retrieved_at=_NOW,
        provider_declared_type=company_type_resolution.provider_declared_type,
        provider_declaration_qualified=(
            company_type_resolution.provider_declaration_qualified
        ),
        normalized_resolver_inputs=company_type_resolution.classifier_inputs,
    )
    listing_date = date(2007, 5, 15)
    annual_periods, reporting_periods = (
        derive_mainland_financial_acquisition_period_supersets(
            run_as_of_date=date(2026, 7, 30),
            listing_date=listing_date,
            policy=plan.financial_eligibility_policy,
        )
    )
    context_result = build_financial_eligibility_acquisition_context(
        canonical_run_id=run_id,
        run_id=run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        authoritative_as_of_date=date(2026, 7, 30),
        as_of_date=date(2026, 7, 30),
        authoritative_availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan=plan,
        routing_plan_signature=plan.plan_signature,
        company_type_resolution=company_type_resolution,
        company_type_evidence=company_type_evidence,
        listing_date=listing_date,
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref=(
                "provider-artifact:v1:"
                + sha256(b"factory-restore-listing").hexdigest()
            ),
            observed_at=_NOW,
        ),
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        acquisition_annual_period_ends=annual_periods,
        acquisition_reporting_period_ends=reporting_periods,
    )
    assert context_result.context is not None
    context = context_result.context
    upstream_id, upstream_name = upstream_service_identity_for_provider(
        "tushare",
        account_scope=plan.account_scope_label,
    )
    key = ProviderSubrequestKey.create(
        provider_id="tushare",
        upstream_service_id=upstream_id,
        account_scope=plan.account_scope_label,
        capacity_scope="balancesheet",
        instrument_identity=asset.instrument_identity,
        requested_range_start="2026-06-30",
        requested_range_end="2026-07-30",
        requested_fields=("all_fields",),
        as_of_date=date(2026, 7, 30),
        qualification_profile=plan.qualification_profile,
        normalizer_version=plan.normalizer_version,
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    artifact_store = ProviderSubrequestArtifactStore(
        blueprint.provider_artifact_root_path / "qualified-provider-subrequests"
    )
    with MarketHistoryStore.open(_history_config(blueprint)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            upstream_id,
            upstream_name,
            account_scope=plan.account_scope_label,
        )
        original_cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id=run_id,
        )
        original_cache.bind_financial_eligibility_context(
            context_contract_version=context.contract_version,
            context_identity=context.context_identity,
        )
        original_result = original_cache.execute(
            key,
            owner_id=run_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="tushare_statement_balancesheet",
            media_type="application/json",
            physical_request=lambda: b'{"fixture":"terminal"}',
        )
        unavailable_key = ProviderSubrequestKey.create(
            provider_id="tushare",
            upstream_service_id=upstream_id,
            account_scope=plan.account_scope_label,
            capacity_scope="income",
            instrument_identity=asset.instrument_identity,
            requested_range_start="2026-06-30",
            requested_range_end="2026-07-30",
            requested_fields=("all_fields",),
            as_of_date=date(2026, 7, 30),
            qualification_profile=plan.qualification_profile,
            normalizer_version=plan.normalizer_version,
            data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
        )
        unavailable_result = original_cache.execute(
            unavailable_key,
            owner_id=run_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: _NOW + timedelta(seconds=1),
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="tushare_statement_income",
            media_type="application/json",
            physical_request=lambda: (_ for _ in ()).throw(
                PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
                    retryable=False,
                    error_code="empty_fixture",
                )
            ),
        )
        bulk_financial_artifact, bulk_candidates = _bulk_statement_candidates(asset)

        def statement_source(
            request: FinancialProviderGapRequest,
        ) -> FinancialProviderPeriodResponse:
            nonlocal qualified_source_calls
            qualified_source_calls += 1
            if request.provider_id == "tushare":
                return FinancialProviderPeriodResponse.available(
                    provider_id="tushare",
                    response_artifact=bulk_financial_artifact,
                    retained_artifacts=(bulk_financial_artifact,),
                    candidates=bulk_candidates,
                    subrequest_keys=(key.subrequest_key,),
                    physical_attempt_ids=tuple(
                        event.attempt_event_id for event in original_result.attempt_events
                    ),
                    physical_attempt_events=original_result.attempt_events,
                )
            return FinancialProviderPeriodResponse.unavailable(
                provider_id=request.provider_id,
                reason=AcquisitionUnavailableReason.NO_DATA,
            )

        original_router = MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources=dict.fromkeys(
                plan.route_for(
                    next(
                        route.capability
                        for route in plan.routes
                        if route.capability.value == "balance_sheet"
                    )
                ),
                statement_source,
            ),
        )
        original_request = FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=date(2026, 7, 30),
            tool_call_id="original-tool-call",
            financial_eligibility_context_binding=(
                FinancialEligibilityRequestBinding.from_context(context)
            ),
        )
        selection_request = build_financial_statement_routing_request(
            original_request,
            context,
        )
        original_dispatcher = FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            config=qualified_config,
            vendor_methods=vendor_methods,
            retry_policy=RetryPolicy(),
            provider_subrequest_cache=original_cache,
            qualified_statement_router=original_router,
            run_scope_id=run_id,
            financial_eligibility_context=context,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
        )
        original_selection = original_dispatcher.dispatch_statement_selection(
            original_request,
            selection_request,
        )
        assert {
            item.candidate.period_identity.period_end
            for item in original_selection.routing_result.manifest.provider_candidates
        } == {date(2025, 12, 31), date(2026, 6, 30)}
        assert len(original_selection.routing_result.manifest.selections) == 2
        assert len(
            {
                item.artifact_identity
                for item in original_selection.routing_result.manifest.selections
            }
        ) == 1
        shadow_ledger = original_dispatcher.checkpoint_ledger()
        authoritative_dispatcher = FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            config=legacy_config,
            vendor_methods=vendor_methods,
            retry_policy=RetryPolicy(),
        )
        authoritative_dispatcher.dispatch(original_request)
        authoritative_ledger = authoritative_dispatcher.checkpoint_ledger()
        original_cache.close()
        coordinator.close()

    binding = plan.financial_eligibility_binding
    assert binding is not None
    historical_authoritative_ledger = copy.deepcopy(authoritative_ledger)
    historical_authoritative_ledger["provider_subrequest_cache"] = copy.deepcopy(
        shadow_ledger["provider_subrequest_cache"]
    )
    checkpoint_state = {
        "run_id": run_id,
        "trade_date": "2026-07-30",
        "asset_configuration": asset.model_dump(mode="json"),
        "capability_routing_plan": plan.model_dump(mode="json"),
        "capability_routing_rollout": (
            MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(mode="json")
        ),
        "financial_eligibility_binding": binding.model_dump(mode="json"),
        "evidence_state": EvidenceState(
            instrument_identity=asset.instrument_identity,
            financial_eligibility_binding=binding,
        ).model_dump(mode="json"),
        "financial_dispatch_ledger": historical_authoritative_ledger,
        "financial_dispatch_shadow_ledger": shadow_ledger,
    }
    checkpoint_bytes = json.dumps(
        checkpoint_state,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    artifact_root = blueprint.provider_artifact_root_path / "qualified-provider-subrequests"

    def artifact_bytes() -> dict[str, bytes]:
        return {
            path.relative_to(artifact_root).as_posix(): path.read_bytes()
            for path in artifact_root.rglob("*")
            if path.is_file()
        }

    original_artifact_bytes = artifact_bytes()
    events: list[str] = []
    coordinator_sequences = 0
    base_factories = production_qualified_routing_runtime_factories()

    def track_close(resource_name: str, resource):
        close = resource.close

        def tracked_close():
            events.append(f"close:{resource_name}")
            close()

        resource.close = tracked_close
        return resource

    def open_coordinator_store(restored_blueprint):
        return track_close(
            "coordinator_store",
            base_factories.open_coordinator_store(restored_blueprint),
        )

    def build_coordinator(store, stop_signal):
        nonlocal coordinator_sequences
        restored_coordinator = base_factories.build_coordinator(store, stop_signal)
        execute_retry_sequence = restored_coordinator.execute_retry_sequence

        def counted_execute_retry_sequence(**kwargs):
            nonlocal coordinator_sequences
            coordinator_sequences += 1
            return execute_retry_sequence(**kwargs)

        restored_coordinator.execute_retry_sequence = counted_execute_retry_sequence
        return track_close("coordinator", restored_coordinator)

    def build_subrequest_cache(coordinator, artifact_store, restored_run_id, stop_signal):
        return track_close(
            "subrequest_cache",
            base_factories.build_subrequest_cache(
                coordinator,
                artifact_store,
                restored_run_id,
                stop_signal,
            ),
        )

    def client(name: str):
        def build(_blueprint, _stop_signal):
            events.append(f"client:{name}")
            return _Client(name, events)

        return build

    runtime_factories = replace(
        base_factories,
        open_coordinator_store=open_coordinator_store,
        build_coordinator=build_coordinator,
        build_subrequest_cache=build_subrequest_cache,
        build_tushare_client=client("tushare"),
        build_akshare_facade=client("akshare"),
        build_yahoo_transport=client("yfinance"),
        build_baostock_transport=client("baostock"),
        phase_hook=lambda phase, _value: events.append(f"phase:{phase}"),
    )

    def restore_financial_dispatcher(
        state,
        *,
        provider_subrequest_cache,
        routing_plan,
    ):
        events.append("restore:dispatcher")
        assert provider_subrequest_cache.checkpoint() == state[
            "financial_dispatch_shadow_ledger"
        ]["provider_subrequest_cache"]
        validation_router = MainlandFinancialCapabilityRouter(
            routing_plan=routing_plan,
            statement_sources={},
        )
        return FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            config=qualified_config,
            vendor_methods=vendor_methods,
            retry_policy=RetryPolicy(),
            checkpoint_ledger=state["financial_dispatch_shadow_ledger"],
            provider_subrequest_cache=provider_subrequest_cache,
            qualified_statement_router=validation_router,
            run_scope_id=run_id,
            financial_eligibility_context=context,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
        )

    class RestoredContextProbe:
        def __init__(self, restored_context) -> None:
            self.context = restored_context
            self.routing = None

        def install_restored_financial_routing(self, routing):
            self.routing = routing
            return routing

    def restore_financial_eligibility_context(restored_context):
        return RestoredContextProbe(restored_context)

    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=runtime_factories,
    )

    def financial_node(
        routing=None,
        context_owner=None,
    ) -> FinancialDispatchToolNode:
        qualified = routing if isinstance(routing, QualifiedFinancialRoutingComposition) else None
        owner_routing = (
            context_owner.financial_routing
            if context_owner is not None
            else None
        )
        effective = qualified or owner_routing
        return FinancialDispatchToolNode(
            config=qualified_config,
            vendor_methods=vendor_methods,
            retry_policy=RetryPolicy(),
            qualified_statement_router=(effective.router if effective else None),
            qualified_statement_request_factory=(
                qualified.statement_request_factory if qualified else None
            ),
            qualified_indicator_request_factory=(
                qualified.indicator_request_factory if qualified else None
            ),
            qualified_provider_subrequest_cache=(
                effective.provider_subrequest_cache if effective else None
            ),
            restored_shadow_dispatcher=(
                effective.restored_shadow_dispatcher if effective else None
            ),
            qualified_context_owner=context_owner,
        )

    class Workflow:
        @staticmethod
        def compile(*, checkpointer=None):
            del checkpointer
            return SimpleNamespace()

    class Setup:
        tool_nodes = None

        @staticmethod
        def setup_graph(_selected_analysts):
            return Workflow()

    host = SimpleNamespace(
        _managed_qualified_routing_composition=composition,
        asset_configuration=asset,
        capability_routing_plan=plan,
        qualified_financial_routing=composition,
        tool_nodes={"fundamentals": financial_node()},
        graph_setup=Setup(),
        selected_analysts=("fundamentals",),
        _active_checkpointer=None,
    )
    host._create_tool_nodes = lambda: {
        "fundamentals": financial_node(
            host.qualified_financial_routing,
            getattr(host, "_financial_eligibility_context_owner", None),
        )
    }
    runtime = TradingAgentsGraph.bind_qualified_routing_runtime(
        host,
        run_id=run_id,
        checkpoint_compatibility_known=True,
        resume_from_checkpoint=True,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
        checkpoint_state=MappingProxyType(checkpoint_state),
    )
    assert runtime is not None

    assert runtime.financial_routing is None
    assert runtime.dependencies is None
    restored_owner = host._financial_eligibility_context_owner
    assert restored_owner.context == context
    restored_routing = restored_owner.financial_routing
    restored_dispatcher = restored_routing.restored_shadow_dispatcher
    assert restored_dispatcher is not None
    assert restored_dispatcher.checkpoint_ledger() == shadow_ledger
    legacy_calls_before_resume = legacy_provider_calls
    qualified_calls_before_resume = qualified_source_calls
    graph_state = copy.deepcopy(checkpoint_state)
    graph_state.update(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_balance_sheet",
                            "args": {
                                "ticker": "601328.SS",
                                "freq": "quarterly",
                                "curr_date": "2026-07-30",
                            },
                            "id": "graph-resumed-tool-call",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "trade_date": "2026-07-30",
        }
    )
    graph_result = host.tool_nodes["fundamentals"](graph_state)
    assert legacy_provider_calls == legacy_calls_before_resume == 1
    assert qualified_source_calls == qualified_calls_before_resume
    assert graph_result["messages"][0].tool_call_id == "graph-resumed-tool-call"
    assert (
        graph_result["financial_dispatch_ledger"]["entries"][0]["terminal"]
        == authoritative_ledger["entries"][0]["terminal"]
    )
    assert graph_result["financial_dispatch_ledger"]["entries"][0]["reuse_count"] == 1
    assert (
        graph_result["financial_dispatch_shadow_ledger"]["qualified_statement_selections"][0][
            "terminal"
        ]
        == shadow_ledger["qualified_statement_selections"][0]["terminal"]
    )
    assert (
        graph_result["financial_dispatch_shadow_ledger"]["qualified_statement_selections"][0][
            "reuse_count"
        ]
        == 1
    )
    repeated = restored_dispatcher.dispatch_statement_selection(
        original_request.model_copy(update={"tool_call_id": "resumed-tool-call"}),
        selection_request,
    )
    restored_cache = restored_routing.provider_subrequest_cache
    assert restored_cache is not None
    restored_cache_result = restored_cache.execute(
        key,
        owner_id=run_id,
        priority=RequestPriority.INTERACTIVE_MAINLAND,
        now=lambda: _NOW + timedelta(minutes=1),
        sleep=lambda _seconds: None,
        lease_duration=timedelta(seconds=30),
        operation="tushare_statement_balancesheet",
        media_type="application/json",
        physical_request=lambda: pytest.fail("resume must perform zero provider I/O"),
    )
    restored_unavailable = restored_cache.execute(
        unavailable_key,
        owner_id=run_id,
        priority=RequestPriority.INTERACTIVE_MAINLAND,
        now=lambda: _NOW + timedelta(minutes=1),
        sleep=lambda _seconds: None,
        lease_duration=timedelta(seconds=30),
        operation="tushare_statement_income",
        media_type="application/json",
        physical_request=lambda: pytest.fail("resume must perform zero provider I/O"),
    )

    def concurrent_repeat(index: int):
        return restored_dispatcher.dispatch_statement_selection(
            original_request.model_copy(update={"tool_call_id": f"concurrent-resume-{index}"}),
            selection_request,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        concurrent_results = tuple(executor.map(concurrent_repeat, range(8)))

    assert repeated.tool_call_id == "resumed-tool-call"
    assert repeated.disposition == "duplicate_suppressed"
    assert repeated.routing_result == original_selection.routing_result
    assert len(repeated.routing_result.manifest.selections) == 2
    assert {
        selection.artifact_identity
        for selection in repeated.routing_result.manifest.selections
    } == {bulk_financial_artifact.artifact_identity}
    assert restored_cache_result == original_result
    assert restored_unavailable == unavailable_result
    assert all(
        result.disposition == "duplicate_suppressed"
        and result.routing_result == original_selection.routing_result
        and result.tool_call_id == f"concurrent-resume-{index}"
        for index, result in enumerate(concurrent_results)
    )
    resumed_shadow_ledger = restored_dispatcher.checkpoint_ledger()
    assert (
        resumed_shadow_ledger["qualified_statement_selections"][0]["terminal"]
        == shadow_ledger["qualified_statement_selections"][0]["terminal"]
    )
    assert resumed_shadow_ledger["qualified_statement_selections"][0]["reuse_count"] == 10
    assert (
        restored_cache.checkpoint()
        == shadow_ledger["provider_subrequest_cache"]
    )
    assert qualified_source_calls == qualified_calls_before_resume
    assert coordinator_sequences == 0
    assert events.index("phase:eligibility_context_restore") < events.index(
        "phase:subrequest_cache"
    )
    assert events.index("phase:subrequest_cache_restore") < events.index(
        "phase:checkpoint_restore"
    )
    assert events.index("phase:checkpoint_restore") < events.index("client:tushare")
    assert events.index("phase:checkpoint_restore") < events.index("phase:provider_adapters")
    assert events.index("phase:checkpoint_restore") < events.index(
        "phase:eligibility_runtime_exposed"
    )
    assert artifact_bytes() == original_artifact_bytes
    assert (
        json.dumps(
            checkpoint_state,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        == checkpoint_bytes
    )
    with pytest.raises(FinancialDispatchCheckpointError) as rebind_error:
        restored_dispatcher.bind_qualified_statement_router(
            restored_routing.router
        )
    assert rebind_error.value.reason is (
        FinancialDispatchCheckpointFailureReason.UNSAFE_BOUNDARY
    )
    composition.close()
    closed_events = tuple(events)
    composition.close()
    assert tuple(events) == closed_events
    for client_name in ("tushare", "akshare", "yfinance", "baostock"):
        assert events.count(f"close:{client_name}") == 1

    pre_context_state = copy.deepcopy(checkpoint_state)
    pre_context_shadow = pre_context_state["financial_dispatch_shadow_ledger"]
    pre_context_shadow["contract_version"] = "1.3"
    pre_context_shadow.pop("checkpoint_thread_id")
    pre_context_shadow.pop("financial_eligibility_context")
    pre_context_bytes = json.dumps(
        pre_context_state,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    events.clear()
    pre_context_composition = (
        build_registered_production_qualified_routing_composition(
            blueprint=blueprint,
            runtime_factories=runtime_factories,
        )
    )
    with pytest.raises(FinancialDispatchCheckpointError) as pre_context_error:
        pre_context_composition.bind_run(
            ProductionQualifiedRoutingRunBinding(
                run_id=run_id,
                asset_configuration=asset,
                routing_plan=plan,
                checkpoint_compatibility_known=True,
                resume_from_checkpoint=True,
                checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
                checkpoint_state=MappingProxyType(pre_context_state),
                restore_financial_dispatcher=restore_financial_dispatcher,
                restore_financial_eligibility_context=(
                    restore_financial_eligibility_context
                ),
            )
        )
    assert pre_context_error.value.reason is (
        FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_MISSING
    )
    assert events == []
    assert (
        json.dumps(
            pre_context_state,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        == pre_context_bytes
    )
    assert artifact_bytes() == original_artifact_bytes
    pre_context_composition.close()

    def available_entry(state):
        return next(
            entry
            for entry in state["financial_dispatch_shadow_ledger"]["provider_subrequest_cache"][
                "entries"
            ]
            if entry["artifact"] is not None
        )

    def unavailable_entry(state):
        return next(
            entry
            for entry in state["financial_dispatch_shadow_ledger"]["provider_subrequest_cache"][
                "entries"
            ]
            if entry["artifact"] is None
        )

    def context_projection(state):
        return state["financial_dispatch_shadow_ledger"][
            "financial_eligibility_context"
        ]

    def reidentify_context(state) -> None:
        projection = context_projection(state)
        identity_material = copy.deepcopy(projection)
        identity_material.pop("context_identity")
        projection["context_identity"] = financial_eligibility_acquisition_context_identity(
            identity_material
        )

    def corrupt_missing_ledger(state) -> None:
        state.pop("financial_dispatch_shadow_ledger")

    def corrupt_missing_context(state) -> None:
        state["financial_dispatch_shadow_ledger"].pop(
            "financial_eligibility_context"
        )

    def corrupt_context_version(state) -> None:
        context_projection(state)["contract_version"] = (
            "financial-eligibility-acquisition-context-v9"
        )

    def corrupt_context_identity(state) -> None:
        context_projection(state)["context_identity"] = (
            "financial-eligibility-acquisition-context:v1:" + "0" * 64
        )

    def corrupt_context_thread(state) -> None:
        state["financial_dispatch_shadow_ledger"]["checkpoint_thread_id"] = (
            "fedcba9876543210"
        )

    def corrupt_context_run(state) -> None:
        context_projection(state)["run_id"] = "run:" + "0" * 64
        reidentify_context(state)

    def corrupt_context_asset(state) -> None:
        context_projection(state)["asset_configuration_signature"] = (
            "asset-config:v1:" + "0" * 64
        )
        reidentify_context(state)

    def corrupt_context_instrument(state) -> None:
        context_projection(state)["instrument_identity"]["symbol"] = "600000.SS"
        reidentify_context(state)

    def corrupt_context_as_of(state) -> None:
        context_projection(state)["as_of_date"] = "2026-07-29"
        reidentify_context(state)

    def corrupt_context_scope(state) -> None:
        context_projection(state)["availability_scope"] = "strict_pit_eligible"
        reidentify_context(state)

    def corrupt_context_purpose(state) -> None:
        context_projection(state)["acquisition_purpose"] = "resume_reconstruction"
        reidentify_context(state)

    def corrupt_context_plan(state) -> None:
        context_projection(state)["routing_plan_signature"] = (
            "mainland-routing-plan:v1:" + "0" * 64
        )
        reidentify_context(state)

    def corrupt_context_eligibility(state) -> None:
        context_projection(state)["financial_eligibility_binding"]["policy_signature"] = (
            "mainland-financial-eligibility-policy:v2:" + "0" * 64
        )
        reidentify_context(state)

    def corrupt_context_unknown_company_type(state) -> None:
        resolution = context_projection(state)["company_type_resolution"]
        resolution["company_type"] = "unknown"
        resolution["method"] = "unresolved"
        reidentify_context(state)

    def corrupt_context_ambiguous_company_type(state) -> None:
        context_projection(state)["company_type_resolution"]["ambiguous"] = True
        reidentify_context(state)

    def corrupt_context_contradictory_company_type(state) -> None:
        context_projection(state)["company_type_resolution"]["contradictory"] = True
        reidentify_context(state)

    def corrupt_context_company_evidence(state) -> None:
        context_projection(state)["company_type_evidence"][
            "normalized_resolver_inputs"
        ] = []
        reidentify_context(state)

    def corrupt_context_listing_authority(state) -> None:
        context_projection(state)["listing_provenance"]["authoritative"] = False
        reidentify_context(state)

    def corrupt_context_listing_provider(state) -> None:
        context_projection(state)["listing_provenance"]["provider_id"] = "yfinance"
        reidentify_context(state)

    def corrupt_context_listing_date(state) -> None:
        context_projection(state)["listing_date"] = "2027-01-01"
        reidentify_context(state)

    def corrupt_context_periods(state) -> None:
        context_projection(state)["acquisition_reporting_period_ends"].pop()
        reidentify_context(state)

    def corrupt_context_cache_binding(state) -> None:
        state["financial_dispatch_shadow_ledger"]["provider_subrequest_cache"][
            "financial_eligibility_context_identity"
        ] = "financial-eligibility-acquisition-context:v1:" + "0" * 64

    def corrupt_context_request_binding(state) -> None:
        state["financial_dispatch_shadow_ledger"]["qualified_statement_selections"][0]["terminal"][
            "selection_request"
        ]["financial_eligibility_context_binding"]["context_identity"] = (
            "financial-eligibility-acquisition-context:v1:" + "0" * 64
        )

    def corrupt_truncated_ledger(state) -> None:
        state["financial_dispatch_shadow_ledger"] = {"contract_version": "1.3"}

    def corrupt_cross_run(state) -> None:
        state["run_id"] = "run:" + "f" * 64

    def corrupt_missing_artifact(state) -> None:
        artifact = available_entry(state)["artifact"]
        artifact["artifact_sha256"] = "1" * 64
        artifact["artifact_ref"] = "provider-subrequest-artifact=sha256:" + "1" * 64

    def corrupt_digest(state) -> None:
        entry = available_entry(state)
        original_artifact = entry["artifact"]
        assert original_artifact is not None
        payload = next(iter(original_artifact_bytes.values()))
        wrong_digest = "2" * 64
        wrong_path = artifact_root / "sha256" / wrong_digest[:2] / f"{wrong_digest}.bin"
        wrong_path.parent.mkdir(parents=True, exist_ok=True)
        wrong_path.write_bytes(payload)
        original_artifact["artifact_sha256"] = wrong_digest
        original_artifact["artifact_ref"] = f"provider-subrequest-artifact=sha256:{wrong_digest}"

    def corrupt_payload(state) -> None:
        artifact = available_entry(state)["artifact"]
        malformed_payload = b'{"truncated":'
        malformed_digest = sha256(malformed_payload).hexdigest()
        malformed_path = (
            artifact_root
            / "sha256"
            / malformed_digest[:2]
            / f"{malformed_digest}.bin"
        )
        malformed_path.parent.mkdir(parents=True, exist_ok=True)
        malformed_path.write_bytes(malformed_payload)
        artifact.update(
            {
                "artifact_sha256": malformed_digest,
                "artifact_ref": (
                    f"provider-subrequest-artifact=sha256:{malformed_digest}"
                ),
                "byte_length": len(malformed_payload),
            }
        )

    def corrupt_byte_length(state) -> None:
        available_entry(state)["artifact"]["byte_length"] += 1

    def corrupt_media_type(state) -> None:
        available_entry(state)["artifact"]["media_type"] = "text/plain"

    def corrupt_asset(state) -> None:
        state["asset_configuration"]["asset_configuration_signature"] = (
            "asset-config:v1:" + "0" * 64
        )

    def corrupt_instrument(state) -> None:
        state["evidence_state"]["instrument_identity"]["symbol"] = "600000.SS"

    def corrupt_plan(state) -> None:
        state["capability_routing_plan"]["plan_signature"] = "mainland-routing-plan:v1:" + "0" * 64

    def corrupt_rollout(state) -> None:
        state["capability_routing_rollout"]["disposition"] = "qualified_authoritative"

    def corrupt_completeness_version(state) -> None:
        state["capability_routing_plan"]["completeness_policy_version"] = (
            "mainland-financial-completeness-v1"
        )

    def corrupt_eligibility(state) -> None:
        state["financial_eligibility_binding"]["policy_signature"] = (
            "mainland-financial-eligibility-policy:v2:" + "0" * 64
        )

    def corrupt_topology(state) -> None:
        state["financial_dispatch_shadow_ledger"]["qualified_statement_selections"][0]["terminal"][
            "routing_result"
        ]["provider_calls"][0]["provider_id"] = "rogue_provider"

    def corrupt_capacity_scope_topology(state) -> None:
        entry = available_entry(state)
        forged_key = ProviderSubrequestKey.create(
            provider_id=key.provider_id,
            upstream_service_id=key.upstream_service_id,
            account_scope=key.account_scope,
            capacity_scope="rogue_scope",
            instrument_identity=asset.instrument_identity,
            requested_range_start=key.requested_range_start,
            requested_range_end=key.requested_range_end,
            requested_fields=key.requested_fields,
            as_of_date=key.as_of_date,
            qualification_profile=key.qualification_profile,
            normalizer_version=key.normalizer_version,
            data_usage_mode=key.data_usage_mode,
        )
        entry["key"] = forged_key.model_dump(mode="json")
        for attempt_event in entry["attempt_events"]:
            attempt_event["request_key"] = forged_key.subrequest_key
            attempt_event["capacity_scope"] = forged_key.capacity_scope
        provider_call = state["financial_dispatch_shadow_ledger"][
            "qualified_statement_selections"
        ][0]["terminal"]["routing_result"]["provider_calls"][0]
        provider_call["subrequest_keys"] = [forged_key.subrequest_key]
        for attempt_event in provider_call["physical_attempt_events"]:
            attempt_event["request_key"] = forged_key.subrequest_key
            attempt_event["capacity_scope"] = forged_key.capacity_scope
        manifest = state["financial_dispatch_shadow_ledger"][
            "qualified_statement_selections"
        ][0]["terminal"]["routing_result"]["manifest"]
        for attempt_event in manifest["physical_attempt_events"]:
            attempt_event["request_key"] = forged_key.subrequest_key
            attempt_event["capacity_scope"] = forged_key.capacity_scope
        state["financial_dispatch_shadow_ledger"]["provider_subrequest_cache"][
            "entries"
        ].sort(key=lambda item: item["key"]["subrequest_key"])

    def corrupt_account_scope(state) -> None:
        available_entry(state)["key"]["account_scope"] = "public"

    def corrupt_data_usage(state) -> None:
        available_entry(state)["key"]["data_usage_mode"] = "commercial"

    def corrupt_qualification_profile(state) -> None:
        available_entry(state)["key"]["qualification_profile"] = "legacy"

    def corrupt_normalizer(state) -> None:
        available_entry(state)["key"]["normalizer_version"] = "mainland-financial-normalizer-v1"

    def corrupt_cache_version(state) -> None:
        state["financial_dispatch_shadow_ledger"]["provider_subrequest_cache"][
            "contract_version"
        ] = "9.9"

    def corrupt_ledger_version(state) -> None:
        state["financial_dispatch_shadow_ledger"]["contract_version"] = "1.2"

    def corrupt_attempt_progress(state) -> None:
        available_entry(state)["attempt_events"][0]["attempt_index"] = 2

    def corrupt_final_attempt_count(state) -> None:
        available_entry(state)["attempt_events"][0]["final_physical_attempt_count"] = 2

    def corrupt_interrupted_available(state) -> None:
        available_entry(state)["attempt_events"][-1]["outcome"] = "abandoned"

    def corrupt_available_without_artifact(state) -> None:
        available_entry(state)["artifact"] = None

    def corrupt_unavailable_with_artifact(state) -> None:
        unavailable_entry(state)["artifact"] = copy.deepcopy(available_entry(state)["artifact"])

    def corrupt_request_key_binding(state) -> None:
        state["financial_dispatch_shadow_ledger"]["qualified_statement_selections"][0]["terminal"][
            "routing_result"
        ]["provider_calls"][0]["subrequest_keys"][0] = unavailable_entry(state)["key"][
            "subrequest_key"
        ]

    unsafe_restore_text = (
        "https://example.invalid/provider?token=checkpoint-secret "
        "raw-provider-exception-canary"
    )

    def corrupt_unsafe_projection(state) -> None:
        available_entry(state)["raw_text"] = unsafe_restore_text

    corruptions = (
        ("missing_ledger", corrupt_missing_ledger),
        ("missing_context", corrupt_missing_context),
        ("context_version", corrupt_context_version),
        ("context_identity", corrupt_context_identity),
        ("context_thread", corrupt_context_thread),
        ("context_run", corrupt_context_run),
        ("context_asset", corrupt_context_asset),
        ("context_instrument", corrupt_context_instrument),
        ("context_as_of", corrupt_context_as_of),
        ("context_scope", corrupt_context_scope),
        ("context_purpose", corrupt_context_purpose),
        ("context_plan", corrupt_context_plan),
        ("context_eligibility", corrupt_context_eligibility),
        ("context_unknown_company_type", corrupt_context_unknown_company_type),
        ("context_ambiguous_company_type", corrupt_context_ambiguous_company_type),
        (
            "context_contradictory_company_type",
            corrupt_context_contradictory_company_type,
        ),
        ("context_company_evidence", corrupt_context_company_evidence),
        ("context_listing_authority", corrupt_context_listing_authority),
        ("context_listing_provider", corrupt_context_listing_provider),
        ("context_listing_date", corrupt_context_listing_date),
        ("context_periods", corrupt_context_periods),
        ("context_cache_binding", corrupt_context_cache_binding),
        ("context_request_binding", corrupt_context_request_binding),
        ("truncated_ledger", corrupt_truncated_ledger),
        ("cross_run", corrupt_cross_run),
        ("missing_artifact", corrupt_missing_artifact),
        ("digest", corrupt_digest),
        ("payload", corrupt_payload),
        ("byte_length", corrupt_byte_length),
        ("media_type", corrupt_media_type),
        ("asset", corrupt_asset),
        ("instrument", corrupt_instrument),
        ("plan", corrupt_plan),
        ("rollout", corrupt_rollout),
        ("completeness_version", corrupt_completeness_version),
        ("eligibility", corrupt_eligibility),
        ("topology", corrupt_topology),
        ("capacity_scope_topology", corrupt_capacity_scope_topology),
        ("account_scope", corrupt_account_scope),
        ("data_usage", corrupt_data_usage),
        ("qualification_profile", corrupt_qualification_profile),
        ("normalizer", corrupt_normalizer),
        ("cache_version", corrupt_cache_version),
        ("ledger_version", corrupt_ledger_version),
        ("attempt_progress", corrupt_attempt_progress),
        ("final_attempt_count", corrupt_final_attempt_count),
        ("interrupted_available", corrupt_interrupted_available),
        ("available_without_artifact", corrupt_available_without_artifact),
        ("unavailable_with_artifact", corrupt_unavailable_with_artifact),
        ("request_key_binding", corrupt_request_key_binding),
        ("unsafe_projection", corrupt_unsafe_projection),
    )
    for case_name, corrupt in corruptions:
        invalid_state = copy.deepcopy(checkpoint_state)
        corrupt(invalid_state)
        invalid_shadow_ledger = invalid_state.get(
            "financial_dispatch_shadow_ledger"
        )
        if (
            isinstance(invalid_shadow_ledger, dict)
            and "provider_subrequest_cache" in invalid_shadow_ledger
        ):
            invalid_state["financial_dispatch_ledger"][
                "provider_subrequest_cache"
            ] = copy.deepcopy(
                invalid_shadow_ledger["provider_subrequest_cache"]
            )
        invalid_checkpoint_bytes = json.dumps(
            invalid_state,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        invalid_artifact_bytes = artifact_bytes()
        events.clear()
        invalid_composition = build_registered_production_qualified_routing_composition(
            blueprint=blueprint,
            runtime_factories=runtime_factories,
        )
        with pytest.raises(ValueError) as error:
            invalid_composition.bind_run(
                ProductionQualifiedRoutingRunBinding(
                    run_id=run_id,
                    asset_configuration=asset,
                    routing_plan=plan,
                    checkpoint_compatibility_known=True,
                    resume_from_checkpoint=True,
                    checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
                    checkpoint_state=MappingProxyType(invalid_state),
                    restore_financial_dispatcher=restore_financial_dispatcher,
                    restore_financial_eligibility_context=(
                        restore_financial_eligibility_context
                    ),
                )
            )
        assert not any(event.startswith("client:") for event in events), case_name
        if case_name == "capacity_scope_topology":
            assert events == [], case_name
        assert invalid_composition.runtime is None, case_name
        assert coordinator_sequences == 0, case_name
        if case_name in {"missing_artifact", "digest", "payload", "byte_length"}:
            assert [event for event in events if event.startswith("close:")] == [
                "close:subrequest_cache",
                "close:coordinator",
                "close:coordinator_store",
            ], case_name
        assert (
            json.dumps(
                invalid_state,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            == invalid_checkpoint_bytes
        ), case_name
        assert artifact_bytes() == invalid_artifact_bytes, case_name
        assert getattr(error.value, "reason", None) is not None, case_name
        assert getattr(error.value, "diagnostic_code", None) == str(
            error.value
        ), case_name
        diagnostic = str(error.value)
        assert str(tmp_path) not in diagnostic, case_name
        assert "deterministic-fixture-only" not in diagnostic, case_name
        assert '{"fixture":"terminal"}' not in diagnostic, case_name
        assert unsafe_restore_text not in diagnostic, case_name
        assert "example.invalid" not in diagnostic, case_name
        assert "raw-provider-exception-canary" not in diagnostic, case_name
        rendered_traceback = "".join(
            traceback.format_exception(error.value)
        )
        assert str(tmp_path) not in rendered_traceback, case_name
        assert "deterministic-fixture-only" not in rendered_traceback, case_name
        assert unsafe_restore_text not in rendered_traceback, case_name
        invalid_composition.close()
        invalid_composition.close()

    events.clear()
    wrong_thread_composition = (
        build_registered_production_qualified_routing_composition(
            blueprint=blueprint,
            runtime_factories=runtime_factories,
        )
    )
    with pytest.raises(FinancialDispatchCheckpointError) as wrong_thread_error:
        wrong_thread_composition.bind_run(
            ProductionQualifiedRoutingRunBinding(
                run_id=run_id,
                asset_configuration=asset,
                routing_plan=plan,
                checkpoint_compatibility_known=True,
                resume_from_checkpoint=True,
                checkpoint_thread_id="fedcba9876543210",
                checkpoint_state=MappingProxyType(checkpoint_state),
                restore_financial_dispatcher=restore_financial_dispatcher,
                restore_financial_eligibility_context=(
                    restore_financial_eligibility_context
                ),
            )
        )
    assert wrong_thread_error.value.reason is (
        FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
    )
    assert events == []
    wrong_thread_composition.close()

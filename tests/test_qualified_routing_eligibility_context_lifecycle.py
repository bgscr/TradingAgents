from __future__ import annotations

import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import tradingagents.dataflows.financial_eligibility_metadata as metadata_module
import tradingagents.qualified_routing_eligibility_lifecycle as eligibility_lifecycle_module
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    preflight_mainland_capability_routing,
    prepare_mainland_temporal_capability_routing_plan,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityProviderResponse,
    FinancialProviderPeriodResponse,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialIndicatorEvidenceRequirements,
    FinancialListingProvenance,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialIndicatorRequestArguments,
    FinancialToolRequest,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialCompanyTypeEvidenceProjection,
    FinancialEligibilityAcquisitionFailureReason,
    FinancialEligibilityAcquisitionResult,
    FinancialEligibilityMetadataBinding,
    FinancialListingMetadata,
    FinancialListingMetadataProjection,
    build_financial_eligibility_acquisition_context,
    derive_mainland_financial_acquisition_period_supersets,
)
from tradingagents.dataflows.financial_eligibility_metadata import (
    FinancialEligibilityMetadataAcquirer,
)
from tradingagents.dataflows.financial_request_factory import (
    build_financial_indicator_routing_request,
    build_financial_statement_routing_request,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.graph.financial_tools import (
    FinancialDispatchToolNode,
    FinancialEligibilityAcquisitionContextOwner,
    QualifiedFinancialRoutingComposition,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.market_history import (
    MarketSession,
    MarketSessionStatus,
    ProvenanceClass,
    StoredSessionCalendar,
)
from tradingagents.qualified_routing_composition import (
    ProductionFinancialEligibilityRuntimeInputs,
    ProductionQualifiedRoutingRuntime,
    ProductionQualifiedRoutingRuntimeAssembly,
)
from tradingagents.qualified_routing_lifecycle import (
    ProductionQualifiedRoutingLifecycleError,
    ProductionQualifiedRoutingLifecycleFailureReason,
)

_AS_OF = date(2026, 8, 8)
_OBSERVED_AT = datetime(2026, 8, 8, 1, 2, tzinfo=timezone.utc)
_RETRIEVED_AT = datetime(2026, 8, 8, 1, 3, tzinfo=timezone.utc)


def _qualified_asset_and_plan(monkeypatch: pytest.MonkeyPatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": (
                "statements",
                "financial_indicators",
            ),
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    asset = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    preflight = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert preflight.plan is not None
    return asset, preflight.plan


def _context(asset, plan, *, run_label: str = "ticket-04a-run"):
    run_id = "run:" + sha256(run_label.encode()).hexdigest()
    resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        classifier_metadata={"industry": "bank"},
        present_fields=("customer_deposits",),
    )
    listing_date = date(2007, 5, 15)
    annual, reporting = derive_mainland_financial_acquisition_period_supersets(
        run_as_of_date=_AS_OF,
        listing_date=listing_date,
        policy=plan.financial_eligibility_policy,
    )
    evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        provider_id="tushare",
        artifact_identity="provider-artifact:v1:" + "1" * 64,
        source_ref="acq.v1:financial.company-type:tushare",
        observed_at=_OBSERVED_AT,
        retrieved_at=_RETRIEVED_AT,
        provider_declared_type=resolution.provider_declared_type,
        provider_declaration_qualified=resolution.provider_declaration_qualified,
        normalized_resolver_inputs=resolution.classifier_inputs,
    )
    result = build_financial_eligibility_acquisition_context(
        canonical_run_id=run_id,
        run_id=run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        authoritative_as_of_date=_AS_OF,
        as_of_date=_AS_OF,
        authoritative_availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan=plan,
        routing_plan_signature=plan.plan_signature,
        company_type_resolution=resolution,
        company_type_evidence=evidence,
        listing_date=listing_date,
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref="provider-artifact:v1:" + "2" * 64,
            observed_at=_OBSERVED_AT,
        ),
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        acquisition_annual_period_ends=annual,
        acquisition_reporting_period_ends=reporting,
    )
    assert result.context is not None
    return result.context


class _SuccessfulAcquirer:
    def __init__(self, context) -> None:
        self.context = context
        self.calls = 0

    def acquire(self, request, **_kwargs) -> FinancialEligibilityAcquisitionResult:
        self.calls += 1
        assert request.run_id == self.context.run_id
        assert request.as_of_date == self.context.as_of_date
        return FinancialEligibilityAcquisitionResult.succeeded(self.context)


class _BlockingAcquirer:
    def __init__(self, context) -> None:
        self.context = context
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        self._lock = Lock()

    def acquire(self, _request, **_kwargs) -> FinancialEligibilityAcquisitionResult:
        with self._lock:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5)
        return FinancialEligibilityAcquisitionResult.succeeded(self.context)


class _TerminalAcquirer:
    def __init__(self, terminal) -> None:
        self.terminal = terminal
        self.calls = 0

    def acquire(self, _request, **_kwargs):
        self.calls += 1
        if isinstance(self.terminal, BaseException):
            raise self.terminal
        return self.terminal


class _BlockingTerminalAcquirer(_TerminalAcquirer):
    def __init__(self, terminal) -> None:
        super().__init__(terminal)
        self.entered = Event()
        self.release = Event()

    def acquire(self, request, **kwargs):
        self.entered.set()
        assert self.release.wait(timeout=5)
        return super().acquire(request, **kwargs)


def _raw_routing(
    plan,
    *,
    provider_subrequest_cache: ProviderSubrequestCache | None = None,
    statement_request_factory=build_financial_statement_routing_request,
) -> QualifiedFinancialRoutingComposition:
    def unavailable(request):
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=request.provider_id,
            reason="no_data",
        )

    return QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources=dict.fromkeys(("tushare", "akshare_sina", "yfinance"), unavailable),
        ),
        statement_request_factory=statement_request_factory,
        indicator_request_factory=build_financial_indicator_routing_request,
        provider_subrequest_cache=provider_subrequest_cache,
    )


def _statement_request() -> FinancialToolRequest:
    return FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_AS_OF,
        material_arguments={},
        tool_call_id="statement-first-use",
    )


def _indicator_request() -> FinancialToolRequest:
    families = (FinancialRatioFamily.PROFIT, FinancialRatioFamily.OPERATION)
    requirements = FinancialIndicatorEvidenceRequirements.create(
        requested_families=families,
        canonical_evidence_contract_id="canonical-evidence-contract.v1",
        canonical_evidence_contract_required_families=(FinancialRatioFamily.PROFIT,),
        capability_profile_id="equity.v1",
        capability_profile_required_families=(FinancialRatioFamily.OPERATION,),
        strategy_rules_id="mainland-equity-strategy-rules.v1",
    )
    return FinancialToolRequest(
        tool_name="get_fundamentals",
        statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=FinancialReportingFrequency.NOT_APPLICABLE,
        as_of_date=_AS_OF,
        material_arguments={},
        tool_call_id="indicator-first-use",
        indicator_request_arguments=FinancialIndicatorRequestArguments(
            requested_families=families,
            family_evidence_requirements=requirements,
        ),
    )


def _context_owned_shadow_node(
    *,
    asset,
    plan,
    owner: FinancialEligibilityAcquisitionContextOwner,
    run_id: str,
) -> tuple[FinancialDispatchToolNode, dict[str, object], list[int], bytes]:
    assert plan.financial_eligibility_policy is not None
    assert plan.financial_eligibility_policy.authority_disposition == (
        "shadow_non_authoritative"
    )
    assert plan.financial_eligibility_policy.qualified_v1_authority_enabled is False
    legacy_calls = [0]

    def balance_sheet(symbol: str, frequency: str, current_date: str) -> str:
        legacy_calls[0] += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            f"# Data retrieved on: {current_date} 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": balance_sheet},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    fixed_clock = lambda: datetime(2026, 8, 8, 12, tzinfo=timezone.utc)  # noqa: E731
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_context_owner=owner,
        clock=fixed_clock,
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_balance_sheet",
                        "args": {
                            "ticker": asset.instrument_identity.symbol,
                            "freq": "quarterly",
                            "curr_date": _AS_OF.isoformat(),
                        },
                        "id": "node-context-first-use",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "run_id": run_id,
        "trade_date": _AS_OF.isoformat(),
        "asset_configuration": asset.model_dump(mode="json"),
        "financial_eligibility_binding": (
            plan.financial_eligibility_binding.model_dump(mode="json")
        ),
        "evidence_state": EvidenceState(
            instrument_identity=asset.instrument_identity,
            financial_eligibility_binding=plan.financial_eligibility_binding,
        ).model_dump(mode="json"),
    }
    baseline_config = dict(config)
    baseline_config["mainland_capability_routing_mode"] = "legacy"
    baseline_config.pop("mainland_capability_routing_plan_signature", None)
    baseline = FinancialDispatchToolNode(
        config=baseline_config,
        vendor_methods=vendor_methods,
        clock=fixed_clock,
    )(state)
    assert legacy_calls == [1]
    baseline_bytes = json.dumps(
        {
            "messages": [
                message.model_dump(mode="json") for message in baseline["messages"]
            ],
            "evidence_state": baseline["evidence_state"],
            "financial_dispatch_ledger": baseline["financial_dispatch_ledger"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    legacy_calls[0] = 0
    return node, state, legacy_calls, baseline_bytes


def _authoritative_result_bytes(result: dict[str, object]) -> bytes:
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


@pytest.mark.unit
def test_first_financial_request_binds_context_before_factory_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    acquirer = _SuccessfulAcquirer(context)
    cleanup_events: list[str] = []
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: cleanup_events.append("interrupt"),
        close_runtime=lambda: cleanup_events.append("close"),
    )

    assert owner.context is None
    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as raised:
        _ = owner.statement_request_factory
    assert raised.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_UNBOUND
    )

    result = owner.acquire_context(as_of_date=_AS_OF)

    assert result.context is owner.context
    assert result.context == context
    assert acquirer.calls == 1
    assert cleanup_events == []
    routing_request = owner.statement_request_factory(_statement_request())
    assert routing_request.financial_eligibility_context_binding.context_identity == (
        context.context_identity
    )


@pytest.mark.unit
def test_concurrent_first_requests_join_one_context_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    acquirer = _BlockingAcquirer(context)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    start = Barrier(9)

    def acquire() -> FinancialEligibilityAcquisitionResult:
        start.wait(timeout=5)
        return owner.acquire_context(as_of_date=_AS_OF)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(acquire) for _ in range(8)]
        start.wait(timeout=5)
        assert acquirer.entered.wait(timeout=5)
        acquirer.release.set()
        results = [future.result(timeout=5) for future in futures]

    assert acquirer.calls == 1
    assert all(result is results[0] for result in results)
    assert all(result.context is owner.context for result in results)


@pytest.mark.unit
def test_concurrent_statement_and_indicator_first_requests_share_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    acquirer = _BlockingAcquirer(context)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    start = Barrier(3)

    def statement():
        start.wait(timeout=5)
        acquisition = owner.acquire_context(as_of_date=_AS_OF)
        return acquisition, owner.statement_request_factory(_statement_request())

    def indicator():
        start.wait(timeout=5)
        acquisition = owner.acquire_context(as_of_date=_AS_OF)
        return acquisition, owner.indicator_request_factory(_indicator_request())

    with ThreadPoolExecutor(max_workers=2) as executor:
        statement_future = executor.submit(statement)
        indicator_future = executor.submit(indicator)
        start.wait(timeout=5)
        assert acquirer.entered.wait(timeout=5)
        acquirer.release.set()
        statement_result, statement_request = statement_future.result(timeout=5)
        indicator_result, indicator_request = indicator_future.result(timeout=5)

    assert acquirer.calls == 1
    assert statement_result is indicator_result
    assert statement_result.context is owner.context
    assert (
        statement_request.financial_eligibility_context_binding.context_identity
        == context.context_identity
    )
    assert (
        indicator_request.financial_eligibility_context_binding.context_identity
        == context.context_identity
    )


@pytest.mark.unit
def test_context_binding_is_idempotent_and_rejects_every_material_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=_SuccessfulAcquirer(context),
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )

    first = owner.bind_context(context)

    assert owner.bind_context(context.model_copy()) is first
    mismatches = (
        {"context_identity": "financial-eligibility-acquisition-context:v1:" + "9" * 64},
        {"run_id": "run:" + "8" * 64},
        {"asset_configuration_signature": (context.asset_configuration_signature[:-1] + "8")},
        {"routing_plan_signature": context.routing_plan_signature[:-1] + "8"},
        {"financial_eligibility_binding": None},
        {"as_of_date": context.as_of_date + timedelta(days=1)},
        {"availability_scope": FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE},
    )
    for update in mismatches:
        with pytest.raises(ProductionQualifiedRoutingLifecycleError) as raised:
            owner.bind_context(context.model_copy(update=update))
        assert raised.value.reason is (
            ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_BINDING_INVALID
        )
        assert owner.context is first


@pytest.mark.unit
def test_first_bind_revalidates_the_complete_context_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=_SuccessfulAcquirer(context),
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    malformed = context.model_copy(
        update={"context_identity": ("financial-eligibility-acquisition-context:v1:" + "9" * 64)}
    )

    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as raised:
        owner.bind_context(malformed)

    assert raised.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_BINDING_INVALID
    )
    assert owner.context is None
    with pytest.raises(ProductionQualifiedRoutingLifecycleError):
        _ = owner.statement_request_factory


@pytest.mark.unit
def test_runtime_assembly_rejects_unvalidated_retained_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _asset, plan = _qualified_asset_and_plan(monkeypatch)

    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as raised:
        ProductionQualifiedRoutingRuntimeAssembly(
            financial_routing=_raw_routing(plan),
            retained_company_type_evidence=("opaque raw_text",),
        )

    assert raised.value.reason is (ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID)


@pytest.mark.unit
def test_typed_acquisition_failure_leaves_slot_unbound_and_cleanup_errors_do_not_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    failure = FinancialEligibilityAcquisitionResult.failed(
        FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE
    )
    acquirer = _TerminalAcquirer(failure)
    cleanup_events: list[str] = []

    def interrupt() -> None:
        cleanup_events.append("interrupt")

    def close() -> None:
        cleanup_events.append("close")
        raise SystemExit(
            "raw cleanup token=credential https://provider.invalid/?secret=value C:\\private"
        )

    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=interrupt,
        close_runtime=close,
    )

    result = owner.acquire_context(as_of_date=_AS_OF)

    assert result is failure
    assert owner.context is None
    assert cleanup_events == ["interrupt", "close"]
    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as raised:
        _ = owner.financial_routing
    assert raised.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_UNBOUND
    )


@pytest.mark.unit
def test_temporal_failure_leaves_no_routing_or_factories_and_preserves_cleanup_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, legacy_plan = _qualified_asset_and_plan(monkeypatch)
    plan = prepare_mainland_temporal_capability_routing_plan(legacy_plan)
    failure = FinancialEligibilityAcquisitionResult.failed(
        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE,
        temporal_proof_unavailable=True,
    )
    cleanup_events: list[str] = []
    fixed_at = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
    calendar = StoredSessionCalendar(
        calendar_revision_id="market-session-calendar:v1:" + "7" * 64,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provider_dataset_id="provider-dataset:fixture-calendar-v1",
        observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        sessions=(MarketSession(_AS_OF, MarketSessionStatus.OPEN),),
    )

    def close() -> None:
        cleanup_events.append("close")
        raise SystemExit("cleanup token=must-not-replace-temporal-failure")

    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id="run:" + sha256(b"ticket-02h-temporal-failure").hexdigest(),
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=_TerminalAcquirer(failure),
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        eligibility_clock=lambda: fixed_at,
        calendar_provider=lambda: calendar,
        interrupt_runtime=lambda: cleanup_events.append("interrupt"),
        close_runtime=close,
    )

    result = owner.acquire_context(as_of_date=_AS_OF)

    assert result is failure
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE
    )
    assert owner.context is None
    assert cleanup_events == ["interrupt", "close"]
    for attribute in (
        "financial_routing",
        "statement_request_factory",
        "indicator_request_factory",
    ):
        with pytest.raises(ProductionQualifiedRoutingLifecycleError) as raised:
            getattr(owner, attribute)
        assert raised.value.reason is (
            ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_UNBOUND
        )
    assert "must-not-replace" not in result.model_dump_json()


@pytest.mark.unit
@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_acquisition_base_exception_remains_primary_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    original = failure_type(
        "raw provider token=credential https://provider.invalid/?secret=value C:\\private"
    )
    events: list[str] = []
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=_TerminalAcquirer(original),
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: events.append("interrupt"),
        close_runtime=lambda: events.append("close"),
    )

    with pytest.raises(failure_type) as raised:
        owner.acquire_context(as_of_date=_AS_OF)

    assert raised.value is original
    assert owner.context is None
    assert events == ["interrupt", "close"]


@pytest.mark.unit
def test_interrupt_during_acquisition_publishes_only_interrupted_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    acquirer = _BlockingAcquirer(context)
    events: list[str] = []
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: events.append("interrupt"),
        close_runtime=lambda: events.append("close"),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        leader = executor.submit(owner.acquire_context, as_of_date=_AS_OF)
        assert acquirer.entered.wait(timeout=5)
        owner.interrupt()
        follower = owner.acquire_context(as_of_date=_AS_OF)
        acquirer.release.set()
        leader_result = leader.result(timeout=5)

    assert follower is leader_result
    assert follower.context is None
    assert follower.failure is not None
    assert follower.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
    )
    assert owner.context is None
    assert events == ["interrupt", "close"]
    owner.interrupt()
    owner.close()
    assert events == ["interrupt", "close"]


@pytest.mark.unit
def test_interrupt_after_metadata_return_cannot_publish_or_bind_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    events: list[str] = []
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=_SuccessfulAcquirer(context),
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: events.append("interrupt"),
        close_runtime=lambda: events.append("close"),
    )
    original_bind = owner.bind_context

    def interrupt_before_bind(candidate):
        owner.interrupt()
        return original_bind(candidate)

    monkeypatch.setattr(owner, "bind_context", interrupt_before_bind)

    result = owner.acquire_context(as_of_date=_AS_OF)

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
    )
    assert owner.context is None
    assert events == ["interrupt", "close"]


@pytest.mark.unit
def test_interrupt_after_success_revokes_exposed_and_captured_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    events: list[str] = []
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=_SuccessfulAcquirer(context),
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: events.append("interrupt"),
        close_runtime=lambda: events.append("close"),
    )
    assert owner.acquire_context(as_of_date=_AS_OF).context is owner.context
    captured_factory = owner.statement_request_factory

    owner.interrupt()

    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as property_error:
        _ = owner.statement_request_factory
    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as captured_error:
        captured_factory(_statement_request())
    assert property_error.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.INTERRUPTED
    )
    assert captured_error.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.INTERRUPTED
    )
    assert events == ["interrupt", "close"]


@pytest.mark.unit
def test_request_construction_base_exception_releases_single_flight_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    original = KeyboardInterrupt("raw request-construction failure")
    events: list[str] = []
    acquirer = _SuccessfulAcquirer(context)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: events.append("interrupt"),
        close_runtime=lambda: events.append("close"),
    )

    def fail_request(**_kwargs):
        raise original

    monkeypatch.setattr(
        eligibility_lifecycle_module,
        "FinancialEligibilityMetadataAcquisitionRequest",
        fail_request,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        owner.acquire_context(as_of_date=_AS_OF)
    assert owner._acquiring is False
    follower = owner.acquire_context(as_of_date=_AS_OF)

    assert raised.value is original
    assert follower.context is None
    assert follower.failure is not None
    assert follower.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
    )
    assert acquirer.calls == 0
    assert owner.context is None
    assert events == ["interrupt", "close"]


@pytest.mark.unit
def test_concurrent_retained_metadata_path_resolves_once_with_zero_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    binding = FinancialEligibilityMetadataBinding(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
    )
    retained_company_type = FinancialCompanyTypeEvidenceProjection(
        metadata_binding=binding,
        evidence=context.company_type_evidence,
    )
    retained_listing = FinancialListingMetadataProjection(
        metadata_binding=binding,
        metadata=FinancialListingMetadata(
            instrument_identity=asset.instrument_identity,
            as_of_date=_AS_OF,
            listing_date=context.listing_date,
            listing_provenance=context.listing_provenance,
        ),
    )
    provider_calls = 0

    def provider_call(_request):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("retained metadata must prevent provider work")

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources=dict.fromkeys(
            plan.route_for(metadata_module.MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE),
            provider_call,
        ),
        capability_sources={
            metadata_module.MainlandCapability.ISSUER_LIFECYCLE: {"baostock": provider_call}
        },
    )
    resolver_calls = 0
    original_resolver = metadata_module.resolve_financial_company_type

    def resolve_once(**kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(**kwargs)

    monkeypatch.setattr(metadata_module, "resolve_financial_company_type", resolve_once)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=FinancialEligibilityMetadataAcquirer(
            routing_plan=plan,
            router=router,
            stop_signal=Event(),
        ),
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        retained_company_type_evidence=(retained_company_type,),
        retained_listing_metadata=(retained_listing,),
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    start = Barrier(9)

    def acquire():
        start.wait(timeout=5)
        return owner.acquire_context(as_of_date=_AS_OF)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(acquire) for _ in range(8)]
        start.wait(timeout=5)
        results = [future.result(timeout=5) for future in futures]

    assert resolver_calls == 1
    assert provider_calls == 0
    assert all(result is results[0] for result in results)


@pytest.mark.unit
def test_concurrent_first_use_resolves_once_and_acquires_listing_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    binding = FinancialEligibilityMetadataBinding(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
    )
    retained_company_type = FinancialCompanyTypeEvidenceProjection(
        metadata_binding=binding,
        evidence=context.company_type_evidence,
    )
    listing = FinancialListingMetadata(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        listing_date=context.listing_date,
        listing_provenance=context.listing_provenance,
    )
    listing_calls = 0
    listing_lock = Lock()

    def lifecycle_source(_request):
        nonlocal listing_calls
        with listing_lock:
            listing_calls += 1
        return FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            retained_artifact_identities=(listing.listing_provenance.source_ref,),
            candidate_count=1,
            subrequest_keys=("provider-subrequest:v1:" + "4" * 64,),
            physical_attempt_ids=("provider-physical-attempt=sha256:" + "5" * 64,),
            listing_metadata=listing,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": lifecycle_source,
            }
        },
    )
    resolver_calls = 0
    original_resolver = metadata_module.resolve_financial_company_type

    def resolve_once(**kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(**kwargs)

    monkeypatch.setattr(metadata_module, "resolve_financial_company_type", resolve_once)
    routing = QualifiedFinancialRoutingComposition(
        router=router,
        statement_request_factory=build_financial_statement_routing_request,
        indicator_request_factory=build_financial_indicator_routing_request,
    )
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=FinancialEligibilityMetadataAcquirer(
            routing_plan=plan,
            router=router,
            stop_signal=Event(),
        ),
        unbound_financial_routing=routing,
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        retained_company_type_evidence=(retained_company_type,),
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    start = Barrier(9)

    def acquire():
        start.wait(timeout=5)
        return owner.acquire_context(as_of_date=_AS_OF)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(acquire) for _ in range(8)]
        start.wait(timeout=5)
        results = [future.result(timeout=5) for future in futures]

    assert resolver_calls == 1
    assert listing_calls == 1
    assert all(result is results[0] for result in results)
    assert results[0].context is owner.context


@pytest.mark.unit
def test_graph_host_binds_slot_without_acquiring_until_financial_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    acquirer = _SuccessfulAcquirer(context)
    runtime = ProductionQualifiedRoutingRuntime(
        run_id=context.run_id,
        financial_routing=None,
        stop_signal=Event(),
        resource_kinds=(),
    )

    class Composition:
        def __init__(self) -> None:
            self.events: list[str] = []

        def bind_run(self, run_binding):
            assert run_binding.run_id == context.run_id
            installer = run_binding.install_financial_eligibility_runtime
            if installer is not None:
                installer(
                    ProductionFinancialEligibilityRuntimeInputs(
                        financial_routing=_raw_routing(plan),
                        metadata_acquirer=acquirer,
                    )
                )
            return runtime

        def interrupt(self) -> None:
            self.events.append("interrupt")

        def close(self) -> None:
            self.events.append("close")

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

    composition = Composition()
    host = SimpleNamespace(
        _managed_qualified_routing_composition=composition,
        _financial_eligibility_context_owner=None,
        asset_configuration=asset,
        capability_routing_plan=plan,
        qualified_financial_routing=composition,
        config={
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
        },
        graph_setup=Setup(),
        selected_analysts=("fundamentals",),
        _active_checkpointer=None,
    )
    host._create_tool_nodes = lambda: TradingAgentsGraph._create_tool_nodes(host)

    bound = TradingAgentsGraph.bind_qualified_routing_runtime(
        host,
        run_id=context.run_id,
        checkpoint_compatibility_known=True,
        analysis_as_of_date=_AS_OF,
    )

    owner = TradingAgentsGraph.financial_eligibility_context_owner.fget(host)
    assert bound is runtime
    assert bound.financial_routing is None
    assert bound.source_composition is None
    assert bound.dependencies is None
    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as runtime_error:
        _ = bound.statement_request_factory
    assert runtime_error.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_UNBOUND
    )
    assert owner is host._financial_eligibility_context_owner
    assert owner is not None
    assert acquirer.calls == 0
    assert TradingAgentsGraph.financial_eligibility_acquisition_context.fget(host) is None
    with pytest.raises(ProductionQualifiedRoutingLifecycleError):
        _ = owner.statement_request_factory

    result = owner.acquire_context(as_of_date=_AS_OF)

    assert acquirer.calls == 1
    assert result.context is owner.context
    assert TradingAgentsGraph.financial_eligibility_acquisition_context.fget(host) is owner.context
    assert owner.statement_request_factory(_statement_request())
    bound_context = owner.context

    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as raised:
        TradingAgentsGraph.bind_qualified_routing_runtime(
            host,
            run_id=context.run_id,
            checkpoint_compatibility_known=True,
            analysis_as_of_date=_AS_OF + timedelta(days=1),
        )
    assert raised.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_BINDING_INVALID
    )
    assert owner.context is bound_context
    assert composition.events == []


@pytest.mark.unit
def test_graph_initialization_failure_interrupts_installed_context_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    acquirer = _SuccessfulAcquirer(context)
    runtime = ProductionQualifiedRoutingRuntime(
        run_id=context.run_id,
        financial_routing=None,
        stop_signal=Event(),
        resource_kinds=(),
    )

    class Composition:
        def __init__(self) -> None:
            self.events: list[str] = []

        def bind_run(self, run_binding):
            installer = run_binding.install_financial_eligibility_runtime
            assert installer is not None
            installer(
                ProductionFinancialEligibilityRuntimeInputs(
                    financial_routing=_raw_routing(plan),
                    metadata_acquirer=acquirer,
                )
            )
            return runtime

        def interrupt(self) -> None:
            self.events.append("interrupt")

        def close(self) -> None:
            self.events.append("close")

    original = KeyboardInterrupt("graph initialization stopped")

    class Workflow:
        @staticmethod
        def compile(*, checkpointer=None):
            del checkpointer
            raise original

    class Setup:
        tool_nodes = None

        @staticmethod
        def setup_graph(_selected_analysts):
            return Workflow()

    composition = Composition()
    host = SimpleNamespace(
        _managed_qualified_routing_composition=composition,
        _financial_eligibility_context_owner=None,
        asset_configuration=asset,
        capability_routing_plan=plan,
        qualified_financial_routing=composition,
        config={
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
        },
        graph_setup=Setup(),
        selected_analysts=("fundamentals",),
        _active_checkpointer=None,
    )
    host._create_tool_nodes = lambda: TradingAgentsGraph._create_tool_nodes(host)

    with pytest.raises(KeyboardInterrupt) as raised:
        TradingAgentsGraph.bind_qualified_routing_runtime(
            host,
            run_id=context.run_id,
            checkpoint_compatibility_known=True,
            analysis_as_of_date=_AS_OF,
        )

    assert raised.value is original
    owner = host._financial_eligibility_context_owner
    assert owner is not None
    result = owner.acquire_context(as_of_date=_AS_OF)
    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
    )
    assert owner.context is None
    assert acquirer.calls == 0
    assert composition.events == ["interrupt", "close"]
    with pytest.raises(ProductionQualifiedRoutingLifecycleError):
        _ = owner.statement_request_factory


@pytest.mark.unit
def test_acquired_cross_binding_becomes_typed_failure_without_partial_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    expected = _context(asset, plan)
    wrong_run = _context(asset, plan, run_label="different-run")
    events: list[str] = []
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=expected.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=_TerminalAcquirer(
            FinancialEligibilityAcquisitionResult.succeeded(wrong_run)
        ),
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: events.append("interrupt"),
        close_runtime=lambda: events.append("close"),
    )

    result = owner.acquire_context(as_of_date=_AS_OF)

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
    )
    assert owner.context is None
    assert events == ["interrupt", "close"]


@pytest.mark.unit
def test_failed_single_flight_is_shared_without_follower_fallback_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan)
    failure = FinancialEligibilityAcquisitionResult.failed(
        FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE
    )
    acquirer = _BlockingTerminalAcquirer(failure)
    events: list[str] = []
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: events.append("interrupt"),
        close_runtime=lambda: events.append("close"),
    )
    start = Barrier(7)

    def acquire():
        start.wait(timeout=5)
        return owner.acquire_context(as_of_date=_AS_OF)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(acquire) for _ in range(6)]
        start.wait(timeout=5)
        assert acquirer.entered.wait(timeout=5)
        acquirer.release.set()
        results = [future.result(timeout=5) for future in futures]

    assert acquirer.calls == 1
    assert all(result is failure for result in results)
    assert owner.context is None
    assert events == ["interrupt", "close"]


@pytest.mark.unit
def test_financial_tool_node_creates_shadow_dispatcher_only_after_context_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    checkpoint_thread_id = "04a05b1c2d3e4f50"
    context = _context(
        asset,
        plan,
        run_label=f"checkpoint:{checkpoint_thread_id}",
    )
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "05b-cache"),
        run_scope_id=context.run_id,
    )
    acquirer = _SuccessfulAcquirer(context)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(
            plan,
            provider_subrequest_cache=cache,
        ),
        checkpoint_thread_id=checkpoint_thread_id,
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    legacy_calls = 0

    def balance_sheet(symbol: str, frequency: str, current_date: str) -> str:
        nonlocal legacy_calls
        legacy_calls += 1
        return (
            f"# Balance Sheet data for {symbol} ({frequency})\n"
            f"# Data retrieved on: {current_date} 12:00:00\n\n"
            "metric,2026-06-30\nTotal,100"
        )

    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": balance_sheet},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    node = FinancialDispatchToolNode(
        config=config,
        vendor_methods=vendor_methods,
        qualified_context_owner=owner,
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_balance_sheet",
                        "args": {
                            "ticker": asset.instrument_identity.symbol,
                            "freq": "quarterly",
                            "curr_date": _AS_OF.isoformat(),
                        },
                        "id": "node-context-first-use",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "run_id": context.run_id,
        "trade_date": _AS_OF.isoformat(),
        "asset_configuration": asset.model_dump(mode="json"),
        "financial_eligibility_binding": (
            plan.financial_eligibility_binding.model_dump(mode="json")
        ),
        "evidence_state": EvidenceState(
            instrument_identity=asset.instrument_identity,
            financial_eligibility_binding=plan.financial_eligibility_binding,
        ).model_dump(mode="json"),
    }

    assert owner.context is None
    result = node(state)

    assert legacy_calls == 1
    assert acquirer.calls == 1
    assert owner.context is not None
    assert "financial_dispatch_shadow_failure" not in result
    shadow_ledger = result["financial_dispatch_shadow_ledger"]
    binding = shadow_ledger["qualified_statement_selections"][0]["terminal"]["selection_request"][
        "financial_eligibility_context_binding"
    ]
    assert binding["context_identity"] == context.context_identity


@pytest.mark.unit
def test_financial_tool_node_projects_typed_context_acquisition_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan, run_label="ticket-07-context-result")
    failure = FinancialEligibilityAcquisitionResult.failed(
        FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE
    )
    acquirer = _TerminalAcquirer(failure)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    node, state, legacy_calls, baseline_bytes = _context_owned_shadow_node(
        asset=asset,
        plan=plan,
        owner=owner,
        run_id=context.run_id,
    )

    result = node(state)

    assert legacy_calls == [1]
    assert acquirer.calls == 1
    assert owner.context is None
    assert "Total,100" in result["messages"][0].content
    assert _authoritative_result_bytes(result) == baseline_bytes
    assert result["financial_dispatch_shadow_failure"] == (
        _single_localized_shadow_failure(
            {
                "stage": "context_result",
                "failure_kind": "typed_result",
                "typed_acquisition_reason": "listing_provenance_unavailable",
                "selection_kind": "statement",
                "context_constructed": False,
                "dispatcher_constructed": False,
            }
        )
    )
    assert "financial_dispatch_shadow_ledger" not in result


@pytest.mark.unit
def test_financial_tool_node_sanitizes_context_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan, run_label="ticket-07-context-exception")
    unsafe_exception = RuntimeError(
        "token=unsafe-message https://provider.invalid/private",
        {"credential": "test-unsafe-arg", "payload": "provider-body"},
    )
    acquirer = _TerminalAcquirer(unsafe_exception)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    node, state, legacy_calls, baseline_bytes = _context_owned_shadow_node(
        asset=asset,
        plan=plan,
        owner=owner,
        run_id=context.run_id,
    )

    result = node(state)

    assert legacy_calls == [1]
    assert acquirer.calls == 1
    assert owner.context is None
    assert "Total,100" in result["messages"][0].content
    assert _authoritative_result_bytes(result) == baseline_bytes
    assert result["financial_dispatch_shadow_failure"] == (
        _single_localized_shadow_failure(
            {
                "stage": "context_exception",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": False,
                "dispatcher_constructed": False,
            }
        )
    )
    serialized = json.dumps(result, default=str, sort_keys=True)
    for forbidden in (
        "unsafe-message",
        "provider.invalid",
        "test-unsafe-arg",
        "provider-body",
    ):
        assert forbidden not in serialized


@pytest.mark.unit
def test_financial_tool_node_distinguishes_context_owned_request_factory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan, run_label="ticket-07-request-factory")
    acquirer = _SuccessfulAcquirer(context)

    def failing_factory(_request, _context):
        raise RuntimeError(
            "credential=unsafe-factory-message",
            {"provider_payload": "unsafe-factory-arg"},
        )

    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(
            plan,
            statement_request_factory=failing_factory,
        ),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    node, state, legacy_calls, baseline_bytes = _context_owned_shadow_node(
        asset=asset,
        plan=plan,
        owner=owner,
        run_id=context.run_id,
    )

    result = node(state)

    assert legacy_calls == [1]
    assert acquirer.calls == 1
    assert owner.context == context
    assert "Total,100" in result["messages"][0].content
    assert _authoritative_result_bytes(result) == baseline_bytes
    assert result["financial_dispatch_shadow_failure"] == (
        _single_localized_shadow_failure(
            {
                "stage": "request_factory",
                "failure_kind": "exception",
                "exception_family": "builtins",
                "exception_class": "RuntimeError",
                "selection_kind": "statement",
                "context_constructed": True,
                "dispatcher_constructed": True,
            }
        )
    )
    assert "financial_dispatch_shadow_ledger" not in result
    serialized = json.dumps(result, default=str, sort_keys=True)
    assert "unsafe-factory-message" not in serialized
    assert "unsafe-factory-arg" not in serialized


@pytest.mark.unit
def test_financial_tool_node_distinguishes_dispatcher_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, plan = _qualified_asset_and_plan(monkeypatch)
    context = _context(asset, plan, run_label="ticket-07-dispatcher-construction")
    acquirer = _SuccessfulAcquirer(context)
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=context.run_id,
        asset_configuration=asset,
        routing_plan=plan,
        metadata_acquirer=acquirer,
        unbound_financial_routing=_raw_routing(plan),
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    node, state, legacy_calls, baseline_bytes = _context_owned_shadow_node(
        asset=asset,
        plan=plan,
        owner=owner,
        run_id=context.run_id,
    )
    state["financial_dispatch_shadow_ledger"] = {
        "contract_version": "unsafe-fake-version",
        "provider_payload": "must-not-survive",
    }

    result = node(state)

    assert legacy_calls == [1]
    assert acquirer.calls == 1
    assert owner.context == context
    assert "Total,100" in result["messages"][0].content
    assert _authoritative_result_bytes(result) == baseline_bytes
    assert result["financial_dispatch_shadow_failure"] == (
        _single_localized_shadow_failure(
            {
                "stage": "dispatcher_construction",
                "failure_kind": "exception",
                "exception_family": "financial_dispatch",
                "exception_class": "FinancialDispatchCheckpointError",
                "typed_reason": "financial_eligibility_policy_mismatch",
                "selection_kind": "statement",
                "context_constructed": True,
                "dispatcher_constructed": False,
            }
        )
    )
    serialized = json.dumps(result, default=str, sort_keys=True)
    assert "unsafe-fake-version" not in serialized
    assert "must-not-survive" not in serialized

from __future__ import annotations

import copy
import importlib.util
import json
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from threading import Event
from types import MappingProxyType, SimpleNamespace

import pytest

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingCheckpointAction,
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingPlan,
    MainlandCapabilityRoutingRunProjection,
    preflight_mainland_capability_routing,
    start_new_mainland_capability_routing_run,
)
from tradingagents.dataflows.acquisition import AcquisitionUnavailableReason
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityProviderResponse,
    FinancialCompanyTypeEvidenceProviderResponse,
    FinancialProviderPeriodResponse,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialCompanyTypeClassifierInput,
    FinancialConsolidationScope,
    FinancialListingProvenance,
    FinancialReportingFrequency,
    FinancialStatementType,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialCompanyTypeEvidenceBinding,
    FinancialEligibilityRequestBinding,
    FinancialListingMetadata,
    build_financial_eligibility_acquisition_context,
    derive_mainland_financial_acquisition_period_supersets,
)
from tradingagents.dataflows.financial_eligibility_metadata import (
    FinancialEligibilityMetadataAcquirer,
)
from tradingagents.dataflows.financial_request_factory import (
    build_financial_statement_routing_request,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactRef,
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestCacheError,
    ProviderSubrequestCacheFailureReason,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.graph.financial_tools import FinancialDispatchToolNode
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.qualified_routing_composition import (
    PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    ProductionQualifiedRoutingRunBinding,
    ProductionQualifiedRoutingRuntimeAssembly,
    ProductionQualifiedRoutingRuntimeFactories,
    build_production_qualified_routing_composition_blueprint,
    build_registered_production_qualified_routing_composition,
)
from tradingagents.qualified_routing_eligibility_lifecycle import (
    FinancialEligibilityAcquisitionContextOwner,
    QualifiedFinancialRoutingComposition,
)
from tradingagents.qualified_routing_lifecycle import (
    ProductionQualifiedRoutingLifecycleError,
    ProductionQualifiedRoutingLifecycleFailureReason,
)

_AS_OF = date(2026, 8, 8)
_NOW = datetime(2026, 8, 8, 1, 2, tzinfo=timezone.utc)
_CHECKPOINT_THREAD_ID = "0123456789abcdef"


def _context_identity(label: str) -> str:
    return (
        "financial-eligibility-acquisition-context:v1:" + sha256(label.encode("utf-8")).hexdigest()
    )


def _run_bindings_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path):
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
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert preflight.plan is not None
    return config, asset, preflight.plan


def _context_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path):
    config, asset, plan = _run_bindings_fixture(monkeypatch, tmp_path)
    run_id = "run:" + sha256(f"checkpoint:{_CHECKPOINT_THREAD_ID}".encode()).hexdigest()
    resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        classifier_metadata={"industry": "bank"},
        present_fields=("customer_deposits", "loans_and_advances"),
    )
    evidence = FinancialCompanyTypeEvidenceBinding.create(
        provider_id="tushare",
        artifact_identity=("provider-artifact:v1:" + sha256(b"company-type-artifact").hexdigest()),
        source_ref="acq.v1:financial.company-type:fixture",
        observed_at=_NOW,
        retrieved_at=_NOW,
        provider_declared_type=resolution.provider_declared_type,
        provider_declaration_qualified=resolution.provider_declaration_qualified,
        normalized_resolver_inputs=resolution.classifier_inputs,
    )
    listing_date = date(1987, 4, 1)
    annual, reporting = derive_mainland_financial_acquisition_period_supersets(
        run_as_of_date=_AS_OF,
        listing_date=listing_date,
        policy=plan.financial_eligibility_policy,
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
            source_ref=("provider-artifact:v1:" + sha256(b"listing-artifact").hexdigest()),
            observed_at=_NOW,
        ),
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        acquisition_annual_period_ends=annual,
        acquisition_reporting_period_ends=reporting,
    )
    assert result.context is not None
    return config, asset, plan, result.context


def _fresh_context_owner_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    checkpoint_thread_id: str,
):
    config, asset, plan = _run_bindings_fixture(monkeypatch, tmp_path)
    source_calls: list[str] = []

    def company_type_source(request):
        source_calls.append("company_type:tushare")
        evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
            instrument_identity=asset.instrument_identity,
            as_of_date=_AS_OF,
            provider_id="tushare",
            artifact_identity=(
                "provider-artifact:v1:" + sha256(b"fresh-company-type").hexdigest()
            ),
            source_ref="acq.v1:financial.company-type:fresh-fixture",
            observed_at=_NOW,
            retrieved_at=_NOW,
            provider_declared_type=FinancialCompanyType.BANK,
            provider_declaration_qualified=True,
            normalized_resolver_inputs=(
                FinancialCompanyTypeClassifierInput(
                    key="metadata:industry",
                    value="bank",
                ),
                FinancialCompanyTypeClassifierInput(
                    key="present_field",
                    value="customer_deposits",
                ),
            ),
        )
        return FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=evidence,
        )

    listing_metadata = FinancialListingMetadata(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        listing_date=date(1987, 4, 1),
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref=(
                "provider-artifact:v1:" + sha256(b"fresh-listing").hexdigest()
            ),
            observed_at=_NOW,
        ),
    )

    def listing_source(_request):
        source_calls.append("listing:baostock")
        return FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            retained_artifact_identities=(
                listing_metadata.listing_provenance.source_ref,
            ),
            candidate_count=1,
            subrequest_keys=("provider-subrequest:v1:" + "4" * 64,),
            physical_attempt_ids=("provider-physical-attempt=sha256:" + "5" * 64,),
            listing_metadata=listing_metadata,
        )

    def unavailable_statement(request):
        source_calls.append(f"statement:{request.provider_id}")
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=request.provider_id,
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources=dict.fromkeys(
            plan.route_for(MainlandCapability.BALANCE_SHEET),
            unavailable_statement,
        ),
        company_type_evidence_sources={"tushare": company_type_source},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": listing_source,
            }
        },
    )
    run_id = "run:" + sha256(
        f"checkpoint:{checkpoint_thread_id}".encode()
    ).hexdigest()
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(
            tmp_path / f"fresh-artifacts-{checkpoint_thread_id}"
        ),
        run_scope_id=run_id,
    )
    routing = QualifiedFinancialRoutingComposition(
        router=router,
        statement_request_factory=build_financial_statement_routing_request,
        provider_subrequest_cache=cache,
    )
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=run_id,
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
        checkpoint_thread_id=checkpoint_thread_id,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )
    return config, asset, plan, owner, source_calls


@pytest.mark.unit
def test_context_bound_cache_checkpoint_restores_only_for_exact_context(
    tmp_path,
) -> None:
    run_id = "run:" + sha256(b"ticket-05a-run").hexdigest()
    context_identity = _context_identity("ticket-05a-context")
    original = ProviderSubrequestCache(
        coordinator=object(),  # no coordinator work is reachable in this test
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
        run_scope_id=run_id,
    )

    original.bind_financial_eligibility_context(
        context_contract_version=FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
        context_identity=context_identity,
    )
    checkpoint = original.checkpoint()

    assert checkpoint == {
        "contract_version": "1.1",
        "run_scope_id": run_id,
        "financial_eligibility_context_contract_version": (
            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION
        ),
        "financial_eligibility_context_identity": context_identity,
        "entries": [],
    }

    restored = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "restored-artifacts"),
        run_scope_id=run_id,
    )
    restored.bind_financial_eligibility_context(
        context_contract_version=FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
        context_identity=context_identity,
    )
    restored.restore_checkpoint(checkpoint)
    assert restored.checkpoint() == checkpoint


@pytest.mark.unit
@pytest.mark.parametrize("historical", (True, False))
def test_context_bound_cache_rejects_pre_context_or_mismatched_checkpoint(
    tmp_path,
    historical: bool,
) -> None:
    run_id = "run:" + sha256(b"ticket-05a-run").hexdigest()
    expected_identity = _context_identity("ticket-05a-context")
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
        run_scope_id=run_id,
    )
    cache.bind_financial_eligibility_context(
        context_contract_version=FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
        context_identity=expected_identity,
    )
    checkpoint = {
        "contract_version": "1.0" if historical else "1.1",
        "run_scope_id": run_id,
        "entries": [],
    }
    if not historical:
        checkpoint.update(
            {
                "financial_eligibility_context_contract_version": (
                    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION
                ),
                "financial_eligibility_context_identity": _context_identity("different-context"),
            }
        )

    with pytest.raises(ProviderSubrequestCacheError) as error:
        cache.restore_checkpoint(checkpoint)

    assert error.value.reason is ProviderSubrequestCacheFailureReason.CONTEXT_MISMATCH
    assert cache.checkpoint()["entries"] == []


@pytest.mark.unit
def test_missing_restore_artifact_failure_does_not_chain_a_local_path(
    tmp_path,
) -> None:
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "machine-local-root")
    missing = ProviderSubrequestArtifactRef(
        artifact_ref="provider-subrequest-artifact=sha256:" + "0" * 64,
        artifact_sha256="0" * 64,
        byte_length=2,
        media_type="application/json",
    )

    with pytest.raises(ProviderSubrequestCacheError) as raised:
        artifact_store.read(missing)

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.reason is ProviderSubrequestCacheFailureReason.ARTIFACT_CORRUPT
    assert raised.value.__cause__ is None
    assert str(tmp_path) not in rendered


@pytest.mark.unit
def test_fresh_checkpoint_persists_complete_safe_context_projection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, asset, plan, owner, source_calls = _fresh_context_owner_fixture(
        monkeypatch,
        tmp_path,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    acquisition = owner.acquire_context(as_of_date=_AS_OF)
    assert acquisition.context is not None
    context = acquisition.context
    routing = owner.financial_routing
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_AS_OF,
        tool_call_id="ticket-05a-fresh",
    )
    selection_request = owner.statement_request_factory(request)
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(config)
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["tool_vendors"] = dict.fromkeys(vendor_methods, "scripted")
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        provider_subrequest_cache=routing.provider_subrequest_cache,
        qualified_statement_router=routing.router,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )

    dispatcher.dispatch_statement_selection(request, selection_request)
    checkpoint = dispatcher.checkpoint_ledger()

    assert checkpoint["contract_version"] == "1.4"
    assert owner.context is context
    assert context.run_id == owner.run_id
    assert source_calls == [
        "company_type:tushare",
        "listing:baostock",
        *(
            f"statement:{provider_id}"
            for provider_id in plan.route_for(MainlandCapability.BALANCE_SHEET)
        ),
    ]
    assert checkpoint["checkpoint_thread_id"] == _CHECKPOINT_THREAD_ID
    assert checkpoint["financial_eligibility_context"] == context.safe_projection()
    assert checkpoint["provider_subrequest_cache"]["contract_version"] == "1.1"
    assert (
        checkpoint["provider_subrequest_cache"]["financial_eligibility_context_identity"]
        == context.context_identity
    )
    restored_binding = checkpoint["qualified_statement_selections"][0]["terminal"][
        "selection_request"
    ]["financial_eligibility_context_binding"]
    assert restored_binding == FinancialEligibilityRequestBinding.from_context(context).model_dump(
        mode="json"
    )
    serialized = json.dumps(checkpoint, sort_keys=True)
    for forbidden in (
        "fixture-only",
        "raw_text",
        "https://example.invalid/path?token=secret",
        str(tmp_path),
    ):
        assert forbidden not in serialized


@pytest.mark.unit
def test_start_new_uses_a_distinct_run_id_and_fresh_ticket_02b_acquisition(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_thread_id = "fedcba9876543210"
    _config, _asset, plan, owner, source_calls = _fresh_context_owner_fixture(
        monkeypatch,
        tmp_path,
        checkpoint_thread_id=new_thread_id,
    )
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
    historical_bytes = historical_plan.model_dump_json()
    previous_run_id = "run:" + "a" * 64

    compatibility = start_new_mainland_capability_routing_run(
        active_plan=plan,
        checkpoint_plan=historical_plan,
        previous_run_id=previous_run_id,
        new_run_id=owner.run_id,
    )

    assert compatibility.action is MainlandCapabilityRoutingCheckpointAction.START_NEW
    assert compatibility.previous_run_id == previous_run_id
    assert compatibility.new_run_id == owner.run_id
    assert compatibility.previous_run_id != compatibility.new_run_id
    assert compatibility.provider_io_performed is False
    assert source_calls == []

    acquisition = owner.acquire_context(as_of_date=_AS_OF)

    assert acquisition.context is not None
    assert acquisition.context.run_id == compatibility.new_run_id
    assert source_calls == ["company_type:tushare", "listing:baostock"]
    assert historical_plan.model_dump_json() == historical_bytes


@pytest.mark.unit
def test_restored_context_is_single_assigned_before_factories_are_exposed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, asset, plan, context = _context_fixture(monkeypatch, tmp_path)
    owner = FinancialEligibilityAcquisitionContextOwner.from_restored_context(
        run_id=context.run_id,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
        asset_configuration=asset,
        routing_plan=plan,
        context=context,
        expected_as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )

    assert owner.context is context
    assert owner.context_identity == context.context_identity
    assert owner.checkpoint_thread_id == _CHECKPOINT_THREAD_ID
    assert owner.acquire_context(as_of_date=_AS_OF).context == context
    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as unbound:
        _ = owner.statement_request_factory
    assert unbound.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_UNBOUND
    )

    def project(request, restored_context):
        return request, restored_context

    owner.install_restored_financial_routing(
        QualifiedFinancialRoutingComposition(
            router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
            ),
            statement_request_factory=project,
        )
    )
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_AS_OF,
        tool_call_id="restored-owner",
    )
    bound_request, restored_context = owner.statement_request_factory(request)
    assert restored_context is context
    assert bound_request.financial_eligibility_context_binding == (
        FinancialEligibilityRequestBinding.from_context(context)
    )
    assert owner.bind_context(context) is context

    different = context.model_copy(
        update={"context_identity": _context_identity("different-context")}
    )
    with pytest.raises(ProductionQualifiedRoutingLifecycleError) as mismatch:
        owner.bind_context(different)
    assert mismatch.value.reason is (
        ProductionQualifiedRoutingLifecycleFailureReason.ELIGIBILITY_CONTEXT_BINDING_INVALID
    )
    assert owner.context is context


@pytest.mark.unit
def test_failed_graph_restore_rolls_back_the_ticket_04a_host_slot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, asset, plan, context = _context_fixture(monkeypatch, tmp_path)
    events: list[str] = []

    class FailingComposition:
        def bind_run(self, binding):
            assert binding.restore_financial_eligibility_context is not None
            restored_owner = binding.restore_financial_eligibility_context(context)
            assert restored_owner.context is context
            events.append("context:bound")
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
            )

        def interrupt(self) -> None:
            events.append("composition:interrupt")

        def close(self) -> None:
            events.append("composition:close")

    host = SimpleNamespace(
        _managed_qualified_routing_composition=FailingComposition(),
        _financial_eligibility_context_owner=None,
        asset_configuration=asset,
        capability_routing_plan=plan,
        tool_nodes={"fundamentals": FinancialDispatchToolNode(config=config)},
    )

    with pytest.raises(FinancialDispatchCheckpointError):
        TradingAgentsGraph.bind_qualified_routing_runtime(
            host,
            run_id=context.run_id,
            checkpoint_compatibility_known=True,
            resume_from_checkpoint=True,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
            checkpoint_state=MappingProxyType({}),
            analysis_as_of_date=_AS_OF,
        )

    assert events == [
        "context:bound",
        "composition:interrupt",
        "composition:close",
    ]
    assert host._financial_eligibility_context_owner is None


@dataclass
class _OwnedResource:
    name: str
    events: list[str]

    def interrupt(self) -> None:
        self.events.append(f"interrupt:{self.name}")

    def close(self) -> None:
        self.events.append(f"close:{self.name}")


@pytest.mark.unit
def test_exact_restore_binds_context_before_cache_dispatcher_and_factories(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, asset, plan, fresh_owner, source_events = _fresh_context_owner_fixture(
        monkeypatch,
        tmp_path,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    acquisition = fresh_owner.acquire_context(as_of_date=_AS_OF)
    assert acquisition.context is not None
    context = acquisition.context
    fresh_routing = fresh_owner.financial_routing
    cache = fresh_routing.provider_subrequest_cache
    assert cache is not None
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_AS_OF,
        tool_call_id="ticket-05a-original",
    )
    selection_request = fresh_owner.statement_request_factory(request)
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    qualified_config = copy.deepcopy(config)
    qualified_config.update(
        {
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    fresh_dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=qualified_config,
        vendor_methods=vendor_methods,
        provider_subrequest_cache=cache,
        qualified_statement_router=fresh_routing.router,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    original = fresh_dispatcher.dispatch_statement_selection(
        request,
        selection_request,
    )
    fresh_source_events = tuple(source_events)
    shadow_ledger = fresh_dispatcher.checkpoint_ledger()
    legacy_config = copy.deepcopy(qualified_config)
    legacy_config["mainland_capability_routing_mode"] = "legacy"
    legacy_config.pop("mainland_capability_routing_plan_signature")
    authoritative_ledger = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=legacy_config,
        vendor_methods=vendor_methods,
    ).checkpoint_ledger()
    checkpoint_state = {
        "run_id": context.run_id,
        "trade_date": _AS_OF.isoformat(),
        "asset_configuration": asset.model_dump(mode="json"),
        "capability_routing_plan": plan.model_dump(mode="json"),
        "capability_routing_rollout": (
            MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(mode="json")
        ),
        "financial_eligibility_binding": (
            plan.financial_eligibility_binding.model_dump(mode="json")
        ),
        "evidence_state": EvidenceState(
            instrument_identity=asset.instrument_identity,
            financial_eligibility_binding=plan.financial_eligibility_binding,
        ).model_dump(mode="json"),
        "financial_dispatch_ledger": authoritative_ledger,
        "financial_dispatch_shadow_ledger": shadow_ledger,
    }
    checkpoint_bytes = json.dumps(
        checkpoint_state,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    blueprint = build_production_qualified_routing_composition_blueprint(
        plan=plan,
        asset_configuration=asset,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        config=qualified_config,
        validation_workload=True,
        disposition=MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE,
        environment={"TUSHARE_TOKEN": "fixture-only"},
        dependency_availability=dict.fromkeys(
            ("akshare", "baostock", "tushare", "yfinance"),
            True,
        ),
        provider_source_coverage=dict.fromkeys(
            (
                "akshare",
                "akshare_sina",
                "baostock",
                "baostock_qualified_families",
                "tushare",
                "yfinance",
                "yfinance_derived",
            ),
            True,
        ),
        factory_contract_version=(PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION),
    )
    events: list[str] = []
    metadata_calls = 0
    restored_owner: FinancialEligibilityAcquisitionContextOwner | None = None

    class NeverAcquire:
        def acquire(self, *_args, **_kwargs):
            nonlocal metadata_calls
            metadata_calls += 1
            pytest.fail("restore must never invoke metadata acquisition")

    def build_cache(_coordinator, artifact_store, run_id, _stop_signal):
        events.append("create:cache")
        return ProviderSubrequestCache(
            coordinator=object(),
            artifact_store=artifact_store,
            run_scope_id=run_id,
        )

    def restore_statement_source(request):
        source_events.append(f"restore-statement:{request.provider_id}")
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=request.provider_id,
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    def assemble_runtime(**kwargs):
        events.append("phase:provider_adapters")
        restored_router = MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources=dict.fromkeys(
                plan.route_for(MainlandCapability.BALANCE_SHEET),
                restore_statement_source,
            ),
        )
        return ProductionQualifiedRoutingRuntimeAssembly(
            financial_routing=QualifiedFinancialRoutingComposition(
                router=restored_router,
                statement_request_factory=build_financial_statement_routing_request,
            ),
            dependencies=SimpleNamespace(subrequest_cache=kwargs["subrequest_cache"]),
            eligibility_metadata_acquirer=NeverAcquire(),
        )

    factories = ProductionQualifiedRoutingRuntimeFactories(
        open_coordinator_store=lambda _blueprint: _OwnedResource("coordinator_store", events),
        build_coordinator=lambda _store, _stop: _OwnedResource("coordinator", events),
        build_artifact_store=lambda _blueprint: ProviderSubrequestArtifactStore(
            tmp_path / "restored-artifacts"
        ),
        build_subrequest_cache=build_cache,
        build_tushare_client=lambda _blueprint, _stop: (
            events.append("client:tushare") or _OwnedResource("tushare", events)
        ),
        build_akshare_facade=lambda _blueprint, _stop: (
            events.append("client:akshare") or _OwnedResource("akshare", events)
        ),
        build_yahoo_transport=lambda _blueprint, _stop: (
            events.append("client:yfinance") or _OwnedResource("yfinance", events)
        ),
        build_baostock_transport=lambda _blueprint, _stop: (
            events.append("client:baostock") or _OwnedResource("baostock", events)
        ),
        assemble_runtime=assemble_runtime,
        phase_hook=lambda phase, _value: events.append(f"phase:{phase}"),
    )

    def restore_dispatcher(
        state,
        *,
        provider_subrequest_cache,
        routing_plan,
    ):
        events.append("restore:dispatcher")
        validation_router = MainlandFinancialCapabilityRouter(
            routing_plan=routing_plan,
            statement_sources={},
        )
        return FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            config=qualified_config,
            vendor_methods=vendor_methods,
            checkpoint_ledger=state["financial_dispatch_shadow_ledger"],
            provider_subrequest_cache=provider_subrequest_cache,
            qualified_statement_router=validation_router,
            run_scope_id=context.run_id,
        )

    def restore_context(restored_context):
        nonlocal restored_owner
        events.append("restore:context")
        restored_owner = FinancialEligibilityAcquisitionContextOwner.from_restored_context(
            run_id=context.run_id,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
            asset_configuration=asset,
            routing_plan=plan,
            context=restored_context,
            expected_as_of_date=_AS_OF,
            availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            interrupt_runtime=composition.interrupt,
            close_runtime=composition.close,
        )
        return restored_owner

    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=factories,
    )
    runtime = composition.bind_run(
        ProductionQualifiedRoutingRunBinding(
            run_id=context.run_id,
            asset_configuration=asset,
            routing_plan=plan,
            checkpoint_compatibility_known=True,
            resume_from_checkpoint=True,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
            checkpoint_state=MappingProxyType(checkpoint_state),
            restore_financial_dispatcher=restore_dispatcher,
            restore_financial_eligibility_context=restore_context,
        )
    )

    assert restored_owner is not None
    assert restored_owner.context == context
    assert runtime.financial_routing is None
    restored_routing = restored_owner.financial_routing
    restored_dispatcher = restored_routing.restored_shadow_dispatcher
    assert restored_dispatcher is not None
    tool_node = FinancialDispatchToolNode(
        config=qualified_config,
        qualified_statement_router=restored_routing.router,
        qualified_provider_subrequest_cache=(restored_routing.provider_subrequest_cache),
        restored_shadow_dispatcher=restored_dispatcher,
        qualified_context_owner=restored_owner,
    )
    node_dispatcher, _ = tool_node._dispatcher_from_state(
        checkpoint_state,
        shadow=True,
        qualified_router_override=restored_routing.router,
        provider_subrequest_cache_override=(restored_routing.provider_subrequest_cache),
    )
    assert node_dispatcher is restored_dispatcher
    repeated = restored_dispatcher.dispatch_statement_selection(
        request.model_copy(update={"tool_call_id": "ticket-05a-restored"}),
        selection_request,
    )
    assert repeated.disposition == "duplicate_suppressed"
    assert repeated.routing_result == original.routing_result
    assert tuple(source_events) == fresh_source_events
    assert metadata_calls == 0
    assert events.index("restore:context") < events.index("create:cache")
    assert events.index("restore:context") < events.index("restore:dispatcher")
    assert events.index("restore:dispatcher") < events.index("client:tushare")
    assert events.index("restore:dispatcher") < events.index("phase:provider_adapters")
    assert (
        json.dumps(
            checkpoint_state,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        == checkpoint_bytes
    )
    composition.close()

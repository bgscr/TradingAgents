from __future__ import annotations

import copy
import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import tradingagents.qualified_routing_composition as composition_module
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingFailureReason,
    MainlandCapabilityRoutingPreflightError,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityProviderResponse,
    FinancialCapabilityRoutingRequest,
    FinancialCompanyTypeEvidenceProviderResponse,
    FinancialIndicatorProviderGapRequest,
    FinancialIndicatorRoutingRequest,
    FinancialProviderGapRequest,
    FinancialProviderPeriodResponse,
    FinancialStatementRoutingRequest,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialIndicatorEvidenceRequirements,
    FinancialPeriodCandidate,
    FinancialPeriodIdentity,
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    financial_statement_field_declaration,
    resolve_financial_company_type,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)
from tradingagents.market_history import (
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
)
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.qualified_routing_composition import (
    PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    ProductionQualifiedBoundSource,
    ProductionQualifiedRoutingRunDependencies,
    ProductionQualifiedSourceKind,
    ProductionQualifiedSourceRegistration,
    ProductionQualifiedSourceUnavailableError,
    assemble_production_qualified_provider_sources,
    build_production_qualified_routing_composition_blueprint,
)

_PROVIDER_SOURCE_COVERAGE = {
    "akshare": True,
    "akshare_sina": True,
    "baostock": True,
    "baostock_qualified_families": True,
    "tushare": True,
    "yfinance": True,
    "yfinance_derived": True,
}


def _blueprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled=None):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    capabilities = enabled or [
        "statements",
        "financial_indicators",
        "adjustment_factors",
        "name_events",
    ]
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": capabilities,
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
        environment={"TUSHARE_TOKEN": "fixture-token"},
    )
    assert preflight.plan is not None
    plan = preflight.plan
    config["mainland_capability_routing_plan_signature"] = plan.plan_signature
    config["asset_configuration_signature"] = asset.asset_configuration_signature
    blueprint = build_production_qualified_routing_composition_blueprint(
        plan=plan,
        asset_configuration=asset,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        config=config,
        validation_workload=True,
        disposition=MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE,
        environment={"TUSHARE_TOKEN": "fixture-token"},
        dependency_availability={
            "akshare": True,
            "baostock": True,
            "tushare": True,
            "yfinance": True,
        },
        provider_source_coverage=dict(_PROVIDER_SOURCE_COVERAGE),
        factory_contract_version=PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    )
    return blueprint


def _history_config(tmp_path: Path) -> MarketHistoryConfig:
    root = tmp_path / "coordinator"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode="personal_research",
    )


class _ProviderClient:
    def __init__(self) -> None:
        self.calls = 0


def _registrations(
    built: list[str],
    *,
    sources=None,
    max_attempts=None,
    response_decoders=None,
):
    sources = sources or {}
    max_attempts = max_attempts or {}
    response_decoders = response_decoders or {}

    def registration(
        provider_id: str,
        endpoint_id: str,
        kind: ProductionQualifiedSourceKind,
        capabilities: tuple[MainlandCapability, ...],
    ) -> ProductionQualifiedSourceRegistration:
        def factory(dependencies):
            built.append(endpoint_id)
            source = sources.get(
                endpoint_id,
                lambda _request: pytest.fail(
                    f"unexpected source call: {endpoint_id}"
                ),
            )
            response_type = (
                FinancialCompanyTypeEvidenceProviderResponse
                if kind is ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE
                else FinancialCapabilityProviderResponse
                if kind is ProductionQualifiedSourceKind.CAPABILITY
                else FinancialProviderPeriodResponse
            )

            def physical_request(request):
                response = source(request)
                return response.model_dump_json().encode("utf-8")

            return ProductionQualifiedBoundSource.bind(
                dependencies=dependencies,
                physical_provider_id=(
                    "yfinance"
                    if provider_id == "yfinance_derived"
                    else "akshare_sina"
                    if provider_id == "akshare"
                    else provider_id
                ),
                physical_request=physical_request,
                response_decoder=response_decoders.get(
                    endpoint_id,
                    lambda _request, payload, _result: response_type.model_validate_json(payload),
                ),
                adapter=source,
                normalizer_version=f"{endpoint_id}-v1",
                max_physical_attempts=max_attempts.get(endpoint_id, 1),
            )

        return ProductionQualifiedSourceRegistration(
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            source_kind=kind,
            capabilities=capabilities,
            source_factory=factory,
        )

    statements = (
        MainlandCapability.BALANCE_SHEET,
        MainlandCapability.INCOME_STATEMENT,
        MainlandCapability.CASH_FLOW,
    )
    return (
        registration(
            "yfinance",
            "financial_statements",
            ProductionQualifiedSourceKind.STATEMENT,
            statements,
        ),
        registration(
            "akshare",
            "financial_indicators",
            ProductionQualifiedSourceKind.INDICATOR,
            (MainlandCapability.FINANCIAL_INDICATORS,),
        ),
        registration(
            "yfinance",
            "current_financial_profile",
            ProductionQualifiedSourceKind.INDICATOR,
            (MainlandCapability.FINANCIAL_INDICATORS,),
        ),
        registration(
            "yfinance",
            "current_profile_company_type_evidence",
            ProductionQualifiedSourceKind.COMPANY_TYPE_EVIDENCE,
            (MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE,),
        ),
        registration(
            "akshare",
            "adjustment_factors",
            ProductionQualifiedSourceKind.CAPABILITY,
            (MainlandCapability.ADJUSTMENT_FACTORS,),
        ),
        registration(
            "yfinance_derived",
            "derived_adjustment_factors",
            ProductionQualifiedSourceKind.CAPABILITY,
            (MainlandCapability.ADJUSTMENT_FACTORS,),
        ),
        registration(
            "akshare",
            "issuer_name_events",
            ProductionQualifiedSourceKind.CAPABILITY,
            (MainlandCapability.NAME_EVENTS,),
        ),
    )


def _identity() -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol="601328.SS",
        venue="XSHG",
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="registry:mainland:v1",
            retrieved_at="2026-07-29T00:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )


def _company_type_resolution(company_type: FinancialCompanyType):
    return resolve_financial_company_type(
        provider_declared_type=company_type,
        provider_declaration_qualified=True,
        classifier_metadata={},
        present_fields=(),
    )


def _indicator_requirements(
    families: tuple[FinancialRatioFamily, ...],
) -> FinancialIndicatorEvidenceRequirements:
    return FinancialIndicatorEvidenceRequirements.create(
        requested_families=families,
        canonical_evidence_contract_id="canonical-evidence-contract.v1",
        capability_profile_id="equity.v1",
        strategy_rules_id="mainland-equity-strategy-rules.v1",
    )


def test_indicator_company_type_provenance_is_versioned_without_legacy_drift() -> None:
    families = (FinancialRatioFamily.PROFIT,)
    common = {
        "instrument_identity": _identity(),
        "as_of_date": date(2026, 7, 30),
        "company_type": FinancialCompanyType.BANK,
        "consolidation_scope": FinancialConsolidationScope.CONSOLIDATED,
        "currency": "CNY",
        "ratio_families": families,
        "eligible_reporting_period_ends": (date(2026, 3, 31),),
    }
    resolution = _company_type_resolution(FinancialCompanyType.BANK)

    legacy = FinancialIndicatorRoutingRequest(**common)
    assert "company_type_resolution" not in legacy.model_dump(mode="json")
    with pytest.raises(ValueError, match="company-type provenance"):
        FinancialIndicatorRoutingRequest(
            **common,
            company_type_resolution=resolution,
        )

    v5 = {
        **common,
        "contract_version": "financial-indicator-routing-request-v5",
        "eligibility_policy_version": "mainland-financial-period-eligibility-v2",
        "requested_analysis_scope": "current_only",
        "family_evidence_requirements": _indicator_requirements(families),
    }
    with pytest.raises(ValueError, match="company-type provenance"):
        FinancialIndicatorRoutingRequest(**v5)
    request = FinancialIndicatorRoutingRequest(
        **v5,
        company_type_resolution=resolution,
    )
    assert request.model_dump(mode="json")["company_type_resolution"][
        "company_type"
    ] == "bank"


def _yahoo_statement_candidate(period_end: date) -> FinancialPeriodCandidate:
    observed_at = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    identity = _identity()
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id="yfinance",
        endpoint_id="balance_sheet",
        dataset_id="current_financials",
        schema_identity="yfinance-balance-sheet-fixture-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": identity.symbol,
            "period_end": period_end.isoformat(),
        },
        provider_metadata={"frequency": "quarterly"},
        retrieved_at=observed_at,
        observed_at=observed_at,
        payload={"period_end": period_end.isoformat(), "fixture": "safe"},
    )
    declaration = financial_statement_field_declaration(
        FinancialCompanyType.BANK,
        FinancialStatementType.BALANCE_SHEET,
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
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=identity,
            capability=FinancialCapability.STATEMENT,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            ratio_family=None,
            period_end=period_end,
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=FinancialCompanyType.BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            provider_revision_binding=artifact.artifact_identity,
        ),
        filing_metadata=FinancialFilingMetadata(
            ann_date=date(2026, 4, 30),
            f_ann_date=None,
            report_type="1",
            comp_type="2",
            update_flag="0",
            retrieved_at=observed_at,
            observed_at=observed_at,
            local_provider_revision_identity=artifact.artifact_identity,
        ),
        company_type_resolution=_company_type_resolution(FinancialCompanyType.BANK),
        fields=fields,
    )


@contextmanager
def _assembled_runtime(
    *,
    blueprint,
    tmp_path: Path,
    clients: dict[str, object],
    sources: dict[str, object],
    run_scope_id: str,
    coordinated_providers: tuple[str, ...],
    max_attempts: dict[str, int] | None = None,
    response_decoders: dict[str, object] | None = None,
    coordinator_setup=None,
):
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        registered_providers = tuple(
            dict.fromkeys((*coordinated_providers, "akshare_sina", "yfinance"))
        )
        for provider_id in registered_providers:
            account_scope = (
                blueprint.routing_plan.account_scope_label
                if provider_id == "tushare"
                else ""
            )
            upstream_id, service_name = upstream_service_identity_for_provider(
                provider_id,
                account_scope=account_scope,
            )
            coordinator.register_upstream_service(
                upstream_id,
                service_name,
                account_scope=account_scope,
            )
        if coordinator_setup is not None:
            coordinator_setup(coordinator)
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id=run_scope_id,
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients=clients,
            now=lambda: datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            sleep=lambda _seconds: None,
            owner_id=run_scope_id,
            lease_duration=timedelta(seconds=30),
        )
        yield assemble_production_qualified_provider_sources(
            blueprint=blueprint,
            dependencies=dependencies,
            source_registrations=_registrations(
                [],
                sources=sources,
                max_attempts=max_attempts,
                response_decoders=response_decoders,
            ),
        )


def test_source_composition_is_plan_ordered_run_owned_and_zero_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    built: list[str] = []
    clients = {
        provider_id: _ProviderClient()
        for provider_id in reversed(blueprint.factory_contract.dependency_ids)
    }
    module_names_before = frozenset(vars(composition_module))

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="qualified-run-02",
        )
        coordinator_calls = 0

        def unexpected_coordinator_call(**_kwargs):
            nonlocal coordinator_calls
            coordinator_calls += 1
            raise AssertionError("assembly must not enter the coordinator")

        monkeypatch.setattr(
            coordinator,
            "execute_direct_physical_request",
            unexpected_coordinator_call,
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients=clients,
            now=lambda: pytest.fail("assembly must not read the run clock"),
            sleep=lambda _seconds: pytest.fail("assembly must not sleep"),
            owner_id="qualified-run-02",
            lease_duration=timedelta(seconds=30),
        )

        composition = assemble_production_qualified_provider_sources(
            blueprint=blueprint,
            dependencies=dependencies,
            source_registrations=tuple(reversed(_registrations(built))),
        )

    plan = blueprint.routing_plan
    assert composition.router.routing_plan is plan
    assert composition.statement_source_ids == plan.route_for(
        MainlandCapability.BALANCE_SHEET
    )
    assert composition.indicator_source_ids == plan.route_for(
        MainlandCapability.FINANCIAL_INDICATORS
    )
    assert composition.capability_source_ids == (
        (
            MainlandCapability.ADJUSTMENT_FACTORS,
            plan.route_for(MainlandCapability.ADJUSTMENT_FACTORS),
        ),
        (
            MainlandCapability.SUSPENSION_STATUS,
            plan.route_for(MainlandCapability.SUSPENSION_STATUS),
        ),
        (
            MainlandCapability.NAME_EVENTS,
            plan.route_for(MainlandCapability.NAME_EVENTS),
        ),
        (
            MainlandCapability.ISSUER_LIFECYCLE,
            plan.route_for(MainlandCapability.ISSUER_LIFECYCLE),
        ),
    )
    assert MainlandCapability.DAILY_MARKET_SNAPSHOT not in {
        capability for capability, _providers in composition.capability_source_ids
    }
    assert plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT) == (
        "akshare",
        "baostock",
        "yfinance",
    )
    plan_source_pairs = {
        (route.capability, provider_id)
        for route in plan.routes
        if route.capability is not MainlandCapability.DAILY_MARKET_SNAPSHOT
        for provider_id in route.providers
    }
    bound_source_pairs = {
        (capability, binding.provider_id)
        for binding in composition.bindings
        for capability in binding.capabilities
    }
    assert bound_source_pairs == plan_source_pairs
    assert composition.company_type_evidence_source_ids == plan.route_for(
        MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
    )
    assert all(
        binding.coordinator is coordinator
        and binding.subrequest_cache is cache
        and binding.artifact_store is artifact_store
        for binding in composition.bindings
    )
    tushare_statement_endpoints = {
        binding.capabilities[0]: binding.endpoint_id
        for binding in composition.bindings
        if binding.provider_id == "tushare"
        and binding.source_kind is ProductionQualifiedSourceKind.STATEMENT
    }
    assert tushare_statement_endpoints == {
        MainlandCapability.BALANCE_SHEET: "balancesheet",
        MainlandCapability.INCOME_STATEMENT: "income",
        MainlandCapability.CASH_FLOW: "cashflow",
    }
    assert all("fixture-token" not in repr(value) for value in composition.bindings)
    assert "fixture-token" not in repr(composition)
    assert not any("?" in identity or "\\" in identity for identity in composition.source_binding_ids)
    assert built == [
        "financial_statements",
        "financial_indicators",
        "current_financial_profile",
        "adjustment_factors",
        "derived_adjustment_factors",
        "issuer_name_events",
        "current_profile_company_type_evidence",
    ]
    assert coordinator_calls == 0
    assert sum(client.calls for client in clients.values()) == 0
    assert frozenset(vars(composition_module)) == module_names_before
    with pytest.raises(FrozenInstanceError):
        composition.bindings = ()
    with pytest.raises(TypeError):
        dependencies.provider_clients["extra"] = object()


def _external_binding_request(binding, *, variant: int):
    period_end = date(2025 - variant, 12, 31)
    if binding.source_kind is ProductionQualifiedSourceKind.STATEMENT:
        return FinancialProviderGapRequest(
            provider_id=binding.provider_id,
            instrument_identity=_identity(),
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=date(2026, 7, 30),
            missing_annual_period_ends=(period_end,),
            missing_reporting_period_ends=(),
        )
    if binding.source_kind is ProductionQualifiedSourceKind.INDICATOR:
        return FinancialIndicatorProviderGapRequest(
            provider_id=binding.provider_id,
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.BANK,
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.BANK
            ),
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            missing_periods_by_family=(
                (FinancialRatioFamily.PROFIT, (period_end,)),
            ),
        )
    return FinancialCapabilityRoutingRequest(
        instrument_identity=_identity(),
        capability=binding.capabilities[0],
        range_start=date(2024 + variant, 1, 1),
        as_of_date=date(2026, 7, 30),
    )


def test_every_external_binding_shares_retry_singleflight_cache_and_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    attempts: dict[str, int] = {}
    attempts_lock = threading.Lock()
    endpoint_provider = {
        "financial_statements": "yfinance",
        "financial_indicators": "akshare",
        "current_financial_profile": "yfinance",
        "adjustment_factors": "akshare",
        "derived_adjustment_factors": "yfinance_derived",
        "issuer_name_events": "akshare",
    }
    endpoint_kind = {
        "financial_statements": ProductionQualifiedSourceKind.STATEMENT,
        "financial_indicators": ProductionQualifiedSourceKind.INDICATOR,
        "current_financial_profile": ProductionQualifiedSourceKind.INDICATOR,
        "adjustment_factors": ProductionQualifiedSourceKind.CAPABILITY,
        "derived_adjustment_factors": ProductionQualifiedSourceKind.CAPABILITY,
        "issuer_name_events": ProductionQualifiedSourceKind.CAPABILITY,
    }

    def retrying_source(endpoint_id: str):
        provider_id = endpoint_provider[endpoint_id]
        source_kind = endpoint_kind[endpoint_id]

        def source(request):
            with attempts_lock:
                attempt = attempts.get(endpoint_id, 0) + 1
                attempts[endpoint_id] = attempt
            if attempt % 2:
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.TIMEOUT,
                    retryable=True,
                )
            if source_kind is ProductionQualifiedSourceKind.CAPABILITY:
                return FinancialCapabilityProviderResponse.unavailable(
                    provider_id=provider_id,
                    capability=request.capability,
                    reason=AcquisitionUnavailableReason.NO_DATA,
                )
            return FinancialProviderPeriodResponse.unavailable(
                provider_id=provider_id,
                reason=AcquisitionUnavailableReason.NO_DATA,
            )

        return source

    sources = {
        endpoint_id: retrying_source(endpoint_id)
        for endpoint_id in endpoint_provider
    }
    clients = {
        provider_id: _ProviderClient()
        for provider_id in blueprint.factory_contract.dependency_ids
    }
    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources=sources,
        run_scope_id="qualified-external-matrix",
        coordinated_providers=(),
        max_attempts=dict.fromkeys(endpoint_provider, 2),
    ) as composition:
        external_bindings = tuple(
            binding
            for binding in composition.bindings
            if binding.endpoint_id in endpoint_provider
        )
        assert {binding.endpoint_id for binding in external_bindings} == set(
            endpoint_provider
        )
        all_subrequest_keys: set[str] = set()
        all_attempt_ids: set[str] = set()
        for binding in external_bindings:
            request = _external_binding_request(binding, variant=0)
            with ThreadPoolExecutor(max_workers=4) as pool:
                concurrent = tuple(pool.map(binding.source, (request,) * 4))
            first = concurrent[0]
            assert all(result == first for result in concurrent)
            assert attempts[binding.endpoint_id] == 2
            assert len(first.subrequest_keys) == 1
            assert len(first.physical_attempt_ids) == 2
            expected_physical_provider = (
                "akshare_sina"
                if binding.provider_id == "akshare"
                else "yfinance"
            )
            expected_upstream_id = upstream_service_identity_for_provider(
                expected_physical_provider
            )[0]
            period_attempt_events = tuple(
                getattr(first, "physical_attempt_events", ())
            )
            if period_attempt_events:
                assert len(period_attempt_events) == 2
                assert all(
                    event.upstream_service_id == expected_upstream_id
                    and event.capacity_scope == binding.endpoint_id
                    for event in period_attempt_events
                )

            repeated = binding.source(request)
            assert repeated.subrequest_keys == first.subrequest_keys
            assert repeated.physical_attempt_ids == first.physical_attempt_ids
            assert attempts[binding.endpoint_id] == 2

            changed = binding.source(
                _external_binding_request(binding, variant=1)
            )
            profile_cache_key = binding.endpoint_id == "current_financial_profile"
            assert attempts[binding.endpoint_id] == (2 if profile_cache_key else 4)
            if profile_cache_key:
                assert changed.subrequest_keys == first.subrequest_keys
                assert changed.physical_attempt_ids == first.physical_attempt_ids
            else:
                assert changed.subrequest_keys != first.subrequest_keys
                assert set(changed.physical_attempt_ids).isdisjoint(
                    first.physical_attempt_ids
                )

            composition.dependencies.coordinator.record_rate_limit(
                upstream_service_id=expected_upstream_id,
                cooldown_scope=binding.endpoint_id,
                observed_at=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
                retry_after=timedelta(seconds=60),
                provider_code="fixture_rate_limit",
            )
            cooled = binding.source(
                _external_binding_request(binding, variant=2)
            )
            if profile_cache_key:
                assert cooled == first
                assert attempts[binding.endpoint_id] == 2
            else:
                assert cooled.reason is AcquisitionUnavailableReason.RATE_LIMITED
                assert len(cooled.subrequest_keys) == 1
                assert cooled.physical_attempt_ids == ()
                assert attempts[binding.endpoint_id] == 4

            all_subrequest_keys.update(
                (*first.subrequest_keys, *changed.subrequest_keys, *cooled.subrequest_keys)
            )
            all_attempt_ids.update(
                (*first.physical_attempt_ids, *changed.physical_attempt_ids)
            )

    assert len(all_subrequest_keys) == 16
    assert len(all_attempt_ids) == 22


def test_disabled_tushare_capabilities_construct_only_the_enabled_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    clients = {
        provider_id: _ProviderClient()
        for provider_id in blueprint.factory_contract.dependency_ids
    }
    statement_adapter = composition_module.TushareStatementAdapter
    constructed: list[str] = []

    def build_statement_adapter(**kwargs):
        constructed.append("statements")
        return statement_adapter(**kwargs)

    def unexpected_adapter(**_kwargs):
        pytest.fail("a disabled Tushare capability constructed an adapter")

    monkeypatch.setattr(
        composition_module,
        "TushareStatementAdapter",
        build_statement_adapter,
    )
    monkeypatch.setattr(
        composition_module,
        "TushareFinancialIndicatorAdapter",
        unexpected_adapter,
    )
    monkeypatch.setattr(
        composition_module,
        "TushareAdjustmentFactorAdapter",
        unexpected_adapter,
    )
    monkeypatch.setattr(
        composition_module,
        "TushareNameEventAdapter",
        unexpected_adapter,
    )

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="qualified-run-statements",
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients=clients,
            now=lambda: pytest.fail("assembly must not read the run clock"),
            sleep=lambda _seconds: pytest.fail("assembly must not sleep"),
            owner_id="qualified-run-statements",
            lease_duration=timedelta(seconds=30),
        )

        composition = assemble_production_qualified_provider_sources(
            blueprint=blueprint,
            dependencies=dependencies,
            source_registrations=_registrations([]),
        )

    assert constructed == ["statements"]
    assert all(
        not (
            binding.provider_id == "tushare"
            and MainlandCapability.FINANCIAL_INDICATORS in binding.capabilities
        )
        for binding in composition.bindings
    )


class _EmptyTushareStatements(_ProviderClient):
    def _empty(self, endpoint: str, kwargs: dict[str, object]) -> pd.DataFrame:
        self.calls += 1
        return pd.DataFrame()

    def balancesheet(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("balancesheet", kwargs)

    def income(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("income", kwargs)

    def cashflow(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("cashflow", kwargs)


class _EmptyAkshareSina(_ProviderClient):
    def stock_financial_report_sina(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        return pd.DataFrame()


class _CompleteTushareStatements(_ProviderClient):
    def balancesheet(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        common = {
            "ts_code": "601328.SH",
            "ann_date": "20260429",
            "f_ann_date": "20260430",
            "report_type": "1",
            "comp_type": "2",
            "update_flag": "0",
            "cash_reser_cb": 100,
            "depos": 800,
            "cost_fin_assets": 180,
            "fair_value_fin_assets": 120,
            "depos_in_oth_bfi": 120,
            "depos_oth_bfi": 90,
            "decr_in_disbur": 700,
            "total_assets": 1500,
            "total_hldr_eqy_inc_min_int": 200,
            "total_liab": 1300,
        }
        return pd.DataFrame(
            [
                {**common, "end_date": "20251231"},
                {**common, "end_date": "20260331", "update_flag": "1"},
            ]
        )


def test_complete_tushare_statement_bulk_projects_annual_and_reporting_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    tushare = _CompleteTushareStatements()
    clients = {
        "akshare": _ProviderClient(),
        "baostock": _ProviderClient(),
        "tushare": tushare,
        "yfinance": _ProviderClient(),
    }

    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources={},
        run_scope_id="qualified-statement-complete",
        coordinated_providers=("tushare",),
    ) as composition:
        routed = composition.router.route_statement(
            FinancialStatementRoutingRequest(
                instrument_identity=_identity(),
                statement_type=FinancialStatementType.BALANCE_SHEET,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=(date(2025, 12, 31),),
                eligible_reporting_period_ends=(date(2026, 3, 31),),
            )
        )

    assert tuple(call.provider_id for call in routed.provider_calls) == ("tushare",)
    assert {candidate.period_identity.period_end for candidate in routed.selected_candidates} == {
        date(2025, 12, 31),
        date(2026, 3, 31),
    }
    assert routed.missing_annual_period_ends == ()
    assert routed.missing_reporting_period_ends == ()
    assert len(routed.physical_attempt_ids) == 1
    assert tushare.calls == 1


def test_statement_gaps_continue_tushare_then_akshare_sina_then_yahoo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    tushare = _EmptyTushareStatements()
    akshare = _EmptyAkshareSina()
    clients = {
        "akshare": akshare,
        "baostock": _ProviderClient(),
        "tushare": tushare,
        "yfinance": _ProviderClient(),
    }
    yahoo_calls: list[object] = []

    def yahoo_source(request):
        yahoo_calls.append(request)
        return FinancialProviderPeriodResponse.unavailable(
            provider_id="yfinance",
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        for provider_id, account_scope in (
            ("tushare", blueprint.routing_plan.account_scope_label),
            ("akshare_sina", ""),
            ("yfinance", ""),
        ):
            upstream_id, service_name = upstream_service_identity_for_provider(
                provider_id,
                account_scope=account_scope,
            )
            coordinator.register_upstream_service(
                upstream_id,
                service_name,
                account_scope=account_scope,
            )
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="qualified-statement-fallback",
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients=clients,
            now=lambda: datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            sleep=lambda _seconds: None,
            owner_id="qualified-statement-fallback",
            lease_duration=timedelta(seconds=30),
        )
        composition = assemble_production_qualified_provider_sources(
            blueprint=blueprint,
            dependencies=dependencies,
            source_registrations=_registrations(
                [],
                sources={"financial_statements": yahoo_source},
            ),
        )

        routed = composition.router.route_statement(
            FinancialStatementRoutingRequest(
                instrument_identity=_identity(),
                statement_type=FinancialStatementType.BALANCE_SHEET,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=(date(2025, 12, 31),),
                eligible_reporting_period_ends=(date(2026, 3, 31),),
            )
        )

    assert tuple(call.provider_id for call in routed.provider_calls) == (
        "tushare",
        "akshare_sina",
        "yfinance",
    )
    assert tushare.calls == 1
    assert akshare.calls == 1
    assert len(yahoo_calls) == 1
    assert len(routed.physical_attempt_ids) == 3
    assert len(routed.provider_calls[-1].physical_attempt_ids) == 1


def test_yahoo_statement_candidates_remain_degraded_current_only_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    target = date(2026, 3, 31)
    candidate = _yahoo_statement_candidate(target)

    def yahoo_statement(_request):
        return FinancialProviderPeriodResponse.available(
            provider_id="yfinance",
            response_artifact=candidate.artifact,
            retained_artifacts=(candidate.artifact,),
            candidates=(candidate,),
            subrequest_keys=(),
            physical_attempt_ids=(),
        )

    clients = {
        "akshare": _EmptyAkshareSina(),
        "baostock": _ProviderClient(),
        "tushare": _EmptyTushareStatements(),
        "yfinance": _ProviderClient(),
    }
    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources={"financial_statements": yahoo_statement},
        run_scope_id="qualified-yahoo-current-only",
        coordinated_providers=("tushare", "akshare_sina"),
    ) as composition:
        routed = composition.router.route_statement(
            FinancialStatementRoutingRequest(
                instrument_identity=_identity(),
                statement_type=FinancialStatementType.BALANCE_SHEET,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=(),
                eligible_reporting_period_ends=(target,),
            )
        )

    assert routed.selected_candidates == (candidate,)
    assert routed.manifest.selections[0].provider_id == "yfinance"
    assert routed.manifest.selections[0].disposition == "current_only"
    assert routed.manifest.selections[0].strict_pit_eligible is False
    assert len(routed.provider_calls[-1].physical_attempt_ids) == 1


def test_external_yahoo_retry_budget_is_coordinated_and_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    physical_calls = 0

    def retrying_yahoo(_request):
        nonlocal physical_calls
        physical_calls += 1
        if physical_calls == 1:
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.TIMEOUT,
                retryable=True,
                error_code="fixture_timeout",
            )
        return FinancialProviderPeriodResponse.unavailable(
            provider_id="yfinance",
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    clients = {
        "akshare": _EmptyAkshareSina(),
        "baostock": _ProviderClient(),
        "tushare": _EmptyTushareStatements(),
        "yfinance": _ProviderClient(),
    }
    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources={"financial_statements": retrying_yahoo},
        run_scope_id="qualified-yahoo-retry",
        coordinated_providers=("tushare", "akshare_sina"),
        max_attempts={"financial_statements": 2},
    ) as composition:
        routed = composition.router.route_statement(
            FinancialStatementRoutingRequest(
                instrument_identity=_identity(),
                statement_type=FinancialStatementType.BALANCE_SHEET,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=(date(2025, 12, 31),),
                eligible_reporting_period_ends=(date(2026, 3, 31),),
            )
        )

    yahoo = routed.provider_calls[-1]
    assert physical_calls == 2
    assert len(yahoo.physical_attempt_ids) == 2
    assert tuple(event.attempt_index for event in yahoo.physical_attempt_events) == (
        1,
        2,
    )
    assert routed.manifest.acquisition_outcomes[-1].reason is (
        AcquisitionUnavailableReason.NO_DATA
    )


def test_external_yahoo_cooldown_skip_performs_zero_physical_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    physical_calls = 0
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

    def unexpected_yahoo(_request):
        nonlocal physical_calls
        physical_calls += 1
        return FinancialProviderPeriodResponse.unavailable(
            provider_id="yfinance",
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    def establish_cooldown(coordinator: ProviderRequestCoordinator) -> None:
        upstream_id, _service_name = upstream_service_identity_for_provider("yfinance")
        coordinator.record_rate_limit(
            upstream_service_id=upstream_id,
            cooldown_scope="financial_statements",
            observed_at=now,
            retry_after=timedelta(seconds=60),
            provider_code="fixture_capacity",
        )

    clients = {
        "akshare": _EmptyAkshareSina(),
        "baostock": _ProviderClient(),
        "tushare": _EmptyTushareStatements(),
        "yfinance": _ProviderClient(),
    }
    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources={"financial_statements": unexpected_yahoo},
        run_scope_id="qualified-yahoo-cooldown",
        coordinated_providers=("tushare", "akshare_sina"),
        coordinator_setup=establish_cooldown,
    ) as composition:
        routed = composition.router.route_statement(
            FinancialStatementRoutingRequest(
                instrument_identity=_identity(),
                statement_type=FinancialStatementType.BALANCE_SHEET,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=(date(2025, 12, 31),),
                eligible_reporting_period_ends=(date(2026, 3, 31),),
            )
        )

    yahoo = routed.provider_calls[-1]
    assert physical_calls == 0
    assert len(yahoo.subrequest_keys) == 1
    assert yahoo.physical_attempt_ids == ()
    assert yahoo.physical_attempt_events == ()
    assert routed.manifest.acquisition_outcomes[-1].reason is (
        AcquisitionUnavailableReason.RATE_LIMITED
    )


def test_identical_external_yahoo_requests_single_flight_through_run_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    physical_calls = 0
    started = threading.Event()
    release = threading.Event()

    def blocking_yahoo(_request):
        nonlocal physical_calls
        physical_calls += 1
        started.set()
        assert release.wait(timeout=5)
        return FinancialProviderPeriodResponse.unavailable(
            provider_id="yfinance",
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    request = FinancialStatementRoutingRequest(
        instrument_identity=_identity(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=(date(2025, 12, 31),),
        eligible_reporting_period_ends=(date(2026, 3, 31),),
    )
    clients = {
        "akshare": _EmptyAkshareSina(),
        "baostock": _ProviderClient(),
        "tushare": _EmptyTushareStatements(),
        "yfinance": _ProviderClient(),
    }
    with (
        _assembled_runtime(
            blueprint=blueprint,
            tmp_path=tmp_path,
            clients=clients,
            sources={"financial_statements": blocking_yahoo},
            run_scope_id="qualified-yahoo-single-flight",
            coordinated_providers=("tushare", "akshare_sina"),
        ) as composition,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        leader = pool.submit(composition.router.route_statement, request)
        assert started.wait(timeout=5)
        follower = pool.submit(composition.router.route_statement, request)
        release.set()
        results = (leader.result(timeout=5), follower.result(timeout=5))

    yahoo_calls = tuple(result.provider_calls[-1] for result in results)
    assert physical_calls == 1
    assert len(yahoo_calls[0].physical_attempt_ids) == 1
    assert yahoo_calls[0].subrequest_keys == yahoo_calls[1].subrequest_keys
    assert yahoo_calls[0].physical_attempt_ids == yahoo_calls[1].physical_attempt_ids


class _EmptyTushareIndicators(_ProviderClient):
    def fina_indicator(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        return pd.DataFrame()


class _CompleteTushareIndicators(_ProviderClient):
    _PERIODS = (
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
        date(2026, 3, 31),
    )

    def fina_indicator(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        return pd.DataFrame(
            [
                {
                    "ts_code": "601328.SH",
                    "ann_date": (period_end + timedelta(days=25)).strftime("%Y%m%d"),
                    "end_date": period_end.strftime("%Y%m%d"),
                    "netprofit_margin": 28 + index,
                    "profit_to_gr": 31 + index,
                    "roa": 1.1 + index / 100,
                    "roe": 12 + index,
                    "roic": 10 + index,
                }
                for index, period_end in enumerate(self._PERIODS)
            ]
        )


def test_complete_tushare_indicator_bulk_stops_the_plan_after_one_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["financial_indicators"])
    tushare = _CompleteTushareIndicators()
    company_type_resolution = _company_type_resolution(FinancialCompanyType.BANK)
    clients = {
        "akshare": _ProviderClient(),
        "baostock": _ProviderClient(),
        "tushare": tushare,
        "yfinance": _ProviderClient(),
    }

    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources={},
        run_scope_id="qualified-indicator-complete",
        coordinated_providers=("tushare",),
    ) as composition:
        routed = composition.router.route_indicators(
            FinancialIndicatorRoutingRequest(
                contract_version="financial-indicator-routing-request-v5",
                eligibility_policy_version=(
                    "mainland-financial-period-eligibility-v2"
                ),
                requested_analysis_scope="current_only",
                instrument_identity=_identity(),
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                company_type_resolution=company_type_resolution,
                consolidation_scope=FinancialConsolidationScope.UNKNOWN,
                currency="CNY",
                ratio_families=(FinancialRatioFamily.PROFIT,),
                family_evidence_requirements=_indicator_requirements(
                    (FinancialRatioFamily.PROFIT,)
                ),
                eligible_reporting_period_ends=tuple(
                    reversed(_CompleteTushareIndicators._PERIODS)
                ),
            )
        )

    assert tuple(call.provider_id for call in routed.provider_calls) == ("tushare",)
    assert routed.complete_families == (FinancialRatioFamily.PROFIT,)
    assert routed.missing_periods_by_family == ()
    assert len(routed.physical_attempt_ids) == 1
    assert tushare.calls == 1
    assert all(
        candidate.company_type_resolution == company_type_resolution
        for candidate in routed.selected_candidates
    )


class _EmptyBaoStockRatios(_ProviderClient):
    def __init__(self) -> None:
        super().__init__()
        self.endpoints: list[tuple[str, int, int]] = []

    def _empty(self, endpoint: str, *, year: int, quarter: int, **_kwargs) -> pd.DataFrame:
        self.calls += 1
        self.endpoints.append((endpoint, year, quarter))
        return pd.DataFrame()

    def query_profit_data(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("profit", **kwargs)

    def query_operation_data(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("operation", **kwargs)

    def query_growth_data(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("growth", **kwargs)

    def query_balance_data(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("balance", **kwargs)

    def query_cash_flow_data(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("cash_flow", **kwargs)

    def query_dupont_data(self, **kwargs: object) -> pd.DataFrame:
        return self._empty("dupont", **kwargs)


def test_indicator_source_requests_only_exact_baostock_family_period_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["financial_indicators"])
    tushare = _EmptyTushareIndicators()
    baostock = _EmptyBaoStockRatios()
    clients = {
        "akshare": _ProviderClient(),
        "baostock": baostock,
        "tushare": tushare,
        "yfinance": _ProviderClient(),
    }
    external_calls = {"akshare": 0, "yfinance": 0}

    def unavailable(provider_id: str):
        def source(_request):
            external_calls[provider_id] += 1
            return FinancialProviderPeriodResponse.unavailable(
                provider_id=provider_id,
                reason=AcquisitionUnavailableReason.NO_DATA,
            )

        return source

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        for provider_id, account_scope in (
            ("tushare", blueprint.routing_plan.account_scope_label),
            ("baostock", ""),
            ("akshare_sina", ""),
            ("yfinance", ""),
        ):
            upstream_id, service_name = upstream_service_identity_for_provider(
                provider_id,
                account_scope=account_scope,
            )
            coordinator.register_upstream_service(
                upstream_id,
                service_name,
                account_scope=account_scope,
            )
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="qualified-indicator-fallback",
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients=clients,
            now=lambda: datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            sleep=lambda _seconds: None,
            owner_id="qualified-indicator-fallback",
            lease_duration=timedelta(seconds=30),
        )
        composition = assemble_production_qualified_provider_sources(
            blueprint=blueprint,
            dependencies=dependencies,
            source_registrations=_registrations(
                [],
                sources={
                    "financial_indicators": unavailable("akshare"),
                    "current_financial_profile": unavailable("yfinance"),
                },
            ),
        )

        routed = composition.router.route_indicators(
            FinancialIndicatorRoutingRequest(
                contract_version="financial-indicator-routing-request-v5",
                eligibility_policy_version=(
                    "mainland-financial-period-eligibility-v2"
                ),
                requested_analysis_scope="current_only",
                instrument_identity=_identity(),
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                company_type_resolution=_company_type_resolution(
                    FinancialCompanyType.BANK
                ),
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                ratio_families=(
                    FinancialRatioFamily.PROFIT,
                    FinancialRatioFamily.CASH_FLOW,
                ),
                family_evidence_requirements=_indicator_requirements(
                    (
                        FinancialRatioFamily.PROFIT,
                        FinancialRatioFamily.CASH_FLOW,
                    )
                ),
                eligible_reporting_period_ends=(
                    date(2026, 3, 31),
                    date(2025, 12, 31),
                ),
            )
        )

    assert tuple(call.provider_id for call in routed.provider_calls) == (
        "tushare",
        "akshare",
        "baostock_qualified_families",
        "yfinance",
    )
    assert tushare.calls == 1
    assert baostock.endpoints == [
        ("profit", 2026, 1),
        ("profit", 2025, 4),
        ("cash_flow", 2026, 1),
        ("cash_flow", 2025, 4),
    ]
    assert external_calls == {"akshare": 1, "yfinance": 1}
    assert len(routed.provider_calls[1].physical_attempt_ids) == 1
    assert len(routed.provider_calls[-1].physical_attempt_ids) == 1
    assert len(routed.physical_attempt_ids) == 7


class _TushareFactorNames(_ProviderClient):
    def __init__(self) -> None:
        super().__init__()
        self.endpoints: list[str] = []

    def adj_factor(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        self.endpoints.append("adj_factor")
        return pd.DataFrame(
            {
                "ts_code": ["601328.SH"],
                "trade_date": ["20260723"],
                "adj_factor": ["1.250000"],
            }
        )

    def namechange(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        self.endpoints.append("namechange")
        return pd.DataFrame(
            {
                "ts_code": ["601328.SH"],
                "name": ["*ST文本不构成状态"],
                "start_date": ["20200101"],
                "end_date": [""],
                "change_reason": ["descriptive fixture"],
            }
        )


class _BaoStockCapabilities(_ProviderClient):
    def __init__(self) -> None:
        super().__init__()
        self.endpoints: list[str] = []

    def _frame(self, endpoint: str, frame: pd.DataFrame) -> pd.DataFrame:
        self.calls += 1
        self.endpoints.append(endpoint)
        return frame.copy(deep=True)

    def query_raw_daily(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame(
            "raw",
            pd.DataFrame(
                {
                    "date": ["2026-07-23", "2026-07-24"],
                    "code": ["sh.601328", "sh.601328"],
                    "open": ["6.0", "6.1"],
                    "high": ["6.2", "6.3"],
                    "low": ["5.9", "6.0"],
                    "close": ["6.1", "6.2"],
                    "preclose": ["6.0", "6.1"],
                    "volume": ["100", "120"],
                    "amount": ["600", "744"],
                }
            ),
        )

    def query_forward_adjustment_factors(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame(
            "factors",
            pd.DataFrame(
                {
                    "code": ["sh.601328"],
                    "dividOperateDate": ["2026-07-01"],
                    "foreAdjustFactor": ["0.5000000000"],
                }
            ),
        )

    def query_session_status(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame(
            "status",
            pd.DataFrame(
                {
                    "date": ["2026-07-23", "2026-07-24"],
                    "code": ["sh.601328", "sh.601328"],
                    "tradestatus": ["1", "1"],
                    "isST": ["0", "0"],
                    "preclose": ["6.0", "6.1"],
                }
            ),
        )

    def query_lifecycle(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame(
            "lifecycle",
            pd.DataFrame(
                {
                    "code": ["sh.601328"],
                    "code_name": ["交通银行"],
                    "ipoDate": ["2007-05-15"],
                    "outDate": [""],
                    "type": ["1"],
                    "status": ["1"],
                }
            ),
        )


def test_baostock_strict_status_lifecycle_and_tushare_names_preserve_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(
        tmp_path,
        monkeypatch,
        enabled=["adjustment_factors", "name_events"],
    )
    tushare = _TushareFactorNames()
    baostock = _BaoStockCapabilities()
    clients = {
        "akshare": _ProviderClient(),
        "baostock": baostock,
        "tushare": tushare,
        "yfinance": _ProviderClient(),
    }

    def unavailable_capability(provider_id: str, capability: MainlandCapability):
        return lambda _request: FinancialCapabilityProviderResponse.unavailable(
            provider_id=provider_id,
            capability=capability,
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        for provider_id, account_scope in (
            ("tushare", blueprint.routing_plan.account_scope_label),
            ("baostock", ""),
        ):
            upstream_id, service_name = upstream_service_identity_for_provider(
                provider_id,
                account_scope=account_scope,
            )
            coordinator.register_upstream_service(
                upstream_id,
                service_name,
                account_scope=account_scope,
            )
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="qualified-authority",
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients=clients,
            now=lambda: datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            sleep=lambda _seconds: None,
            owner_id="qualified-authority",
            lease_duration=timedelta(seconds=30),
        )
        composition = assemble_production_qualified_provider_sources(
            blueprint=blueprint,
            dependencies=dependencies,
            source_registrations=_registrations(
                [],
                sources={
                    "adjustment_factors": unavailable_capability(
                        "akshare", MainlandCapability.ADJUSTMENT_FACTORS
                    ),
                    "derived_adjustment_factors": unavailable_capability(
                        "yfinance_derived", MainlandCapability.ADJUSTMENT_FACTORS
                    ),
                    "issuer_name_events": unavailable_capability(
                        "akshare", MainlandCapability.NAME_EVENTS
                    ),
                },
            ),
        )
        common = {
            "instrument_identity": _identity(),
            "range_start": date(2026, 7, 23),
            "as_of_date": date(2026, 7, 24),
        }
        factors = composition.router.route_capability(
            FinancialCapabilityRoutingRequest(
                capability=MainlandCapability.ADJUSTMENT_FACTORS,
                **common,
            )
        )
        status = composition.router.route_capability(
            FinancialCapabilityRoutingRequest(
                capability=MainlandCapability.SUSPENSION_STATUS,
                **common,
            )
        )
        lifecycle = composition.router.route_capability(
            FinancialCapabilityRoutingRequest(
                capability=MainlandCapability.ISSUER_LIFECYCLE,
                **common,
            )
        )
        names = composition.router.route_capability(
            FinancialCapabilityRoutingRequest(
                capability=MainlandCapability.NAME_EVENTS,
                **common,
            )
        )

    assert factors.selected_provider == "baostock"
    assert factors.strict_bundle_established is True
    assert len(factors.strict_bundle_component_artifact_identities) == 3
    assert status.selected_provider == "baostock"
    assert status.authoritative_provider == "baostock"
    assert status.current_tradeability == "tradeable"
    assert lifecycle.selected_provider == "baostock"
    assert names.selected_provider == "tushare"
    assert names.current_tradeability == "unknown"
    assert names.status_or_lifecycle_fact_created is False
    assert names.source_facts_created is False
    assert baostock.endpoints == ["raw", "factors", "status", "lifecycle"]
    assert tushare.endpoints == ["namechange"]
    factor_binding = next(
        binding
        for binding in composition.bindings
        if binding.provider_id == "tushare"
        and MainlandCapability.ADJUSTMENT_FACTORS in binding.capabilities
    )
    name_binding = next(
        binding
        for binding in composition.bindings
        if binding.provider_id == "tushare"
        and MainlandCapability.NAME_EVENTS in binding.capabilities
    )
    assert factor_binding.endpoint_id == "adj_factor"
    assert name_binding.endpoint_id == "namechange"
    assert factor_binding.binding_id != name_binding.binding_id


def test_external_source_raw_exception_is_sanitized_at_the_composition_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    clients = {
        "akshare": _EmptyAkshareSina(),
        "baostock": _ProviderClient(),
        "tushare": _EmptyTushareStatements(),
        "yfinance": _ProviderClient(),
    }
    canary = "fixture-token https://provider.invalid?a=secret C:\\private\\payload.json"

    def failing_yahoo(_request):
        raise RuntimeError(canary)

    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources={"financial_statements": failing_yahoo},
        run_scope_id="qualified-source-safety",
        coordinated_providers=("tushare", "akshare_sina"),
    ) as composition:
        routed = composition.router.route_statement(
            FinancialStatementRoutingRequest(
                instrument_identity=_identity(),
                statement_type=FinancialStatementType.BALANCE_SHEET,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=(date(2025, 12, 31),),
                eligible_reporting_period_ends=(date(2026, 3, 31),),
            )
        )

    assert routed.provider_calls[-1].provider_id == "yfinance"
    yahoo = routed.provider_calls[-1]
    yahoo_outcome = routed.manifest.acquisition_outcomes[-1]
    assert yahoo_outcome.provider_id == "yfinance"
    assert yahoo_outcome.reason is AcquisitionUnavailableReason.PROVIDER_ERROR
    assert len(yahoo.subrequest_keys) == 1
    assert len(yahoo.physical_attempt_ids) == 1
    assert len(yahoo.physical_attempt_events) == 1
    event = yahoo.physical_attempt_events[0]
    assert yahoo.subrequest_keys == (event.request_key,)
    assert yahoo.physical_attempt_ids == (event.attempt_event_id,)
    assert event.upstream_service_id == upstream_service_identity_for_provider(
        "yfinance"
    )[0]
    assert event.capacity_scope == "financial_statements"
    assert event.outcome.value == "provider_error"
    assert event.retryable is False
    assert canary not in repr(routed)
    assert "provider.invalid" not in repr(routed)
    assert "private" not in repr(routed)


def test_external_source_preserves_typed_safe_unavailable_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["statements"])
    clients = {
        "akshare": _EmptyAkshareSina(),
        "baostock": _ProviderClient(),
        "tushare": _EmptyTushareStatements(),
        "yfinance": _ProviderClient(),
    }
    def typed_yahoo_failure(_request):
        return FinancialProviderPeriodResponse.unavailable(
            provider_id="yfinance",
            reason=AcquisitionUnavailableReason.PROVIDER_ERROR,
        )

    def typed_decoder_failure(_request, _payload):
        raise ProductionQualifiedSourceUnavailableError(
            FinancialProviderPeriodResponse.unavailable(
                provider_id="yfinance",
                reason=AcquisitionUnavailableReason.TIMEOUT,
            )
        )

    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources={"financial_statements": typed_yahoo_failure},
        run_scope_id="qualified-source-typed-failure",
        coordinated_providers=("tushare", "akshare_sina"),
        response_decoders={"financial_statements": typed_decoder_failure},
    ) as composition:
        routed = composition.router.route_statement(
            FinancialStatementRoutingRequest(
                instrument_identity=_identity(),
                statement_type=FinancialStatementType.BALANCE_SHEET,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=(date(2025, 12, 31),),
                eligible_reporting_period_ends=(date(2026, 3, 31),),
            )
        )

    yahoo = routed.provider_calls[-1]
    assert yahoo.provider_id == "yfinance"
    assert len(yahoo.subrequest_keys) == 1
    assert yahoo.subrequest_keys[0].startswith("provider-subrequest:v1:")
    assert len(yahoo.physical_attempt_ids) == 1
    assert yahoo.physical_attempt_ids[0].startswith(
        "provider-physical-attempt=sha256:"
    )
    assert routed.physical_attempt_ids[-1] == yahoo.physical_attempt_ids[0]
    assert routed.manifest.acquisition_outcomes[-1].reason is (
        AcquisitionUnavailableReason.TIMEOUT
    )


def test_missing_duplicate_and_unknown_source_coverage_fail_before_factories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    clients = {
        provider_id: _ProviderClient()
        for provider_id in blueprint.factory_contract.dependency_ids
    }

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="qualified-coverage",
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients=clients,
            now=lambda: pytest.fail("coverage validation must not read the clock"),
            sleep=lambda _seconds: pytest.fail("coverage validation must not sleep"),
            owner_id="qualified-coverage",
            lease_duration=timedelta(seconds=30),
        )
        for case in (
            "missing",
            "duplicate",
            "split_duplicate",
            "unknown",
            "ratio_as_statement",
        ):
            built: list[str] = []
            registrations = _registrations(built)
            if case == "missing":
                registrations = registrations[:-1]
            elif case == "duplicate":
                registrations = (*registrations, registrations[-1])
            elif case == "split_duplicate":
                registrations = (
                    *registrations[1:],
                    *(
                        ProductionQualifiedSourceRegistration(
                            provider_id="yfinance",
                            endpoint_id="financial_statements",
                            source_kind=ProductionQualifiedSourceKind.STATEMENT,
                            capabilities=(capability,),
                            source_factory=lambda _dependencies: lambda _request: None,
                        )
                        for capability in (
                            MainlandCapability.BALANCE_SHEET,
                            MainlandCapability.INCOME_STATEMENT,
                            MainlandCapability.CASH_FLOW,
                        )
                    ),
                )
            elif case == "unknown":
                registrations = (
                    *registrations,
                    ProductionQualifiedSourceRegistration(
                        provider_id="unknown_provider",
                        endpoint_id="financial_indicators",
                        source_kind=ProductionQualifiedSourceKind.INDICATOR,
                        capabilities=(MainlandCapability.FINANCIAL_INDICATORS,),
                        source_factory=lambda _dependencies: lambda _request: None,
                    ),
                )
            else:
                registrations = (
                    *registrations,
                    ProductionQualifiedSourceRegistration(
                        provider_id="baostock_qualified_families",
                        endpoint_id="qualified_ratio_families",
                        source_kind=ProductionQualifiedSourceKind.STATEMENT,
                        capabilities=(MainlandCapability.BALANCE_SHEET,),
                        source_factory=lambda _dependencies: lambda _request: None,
                    ),
                )

            with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
                assemble_production_qualified_provider_sources(
                    blueprint=blueprint,
                    dependencies=dependencies,
                    source_registrations=registrations,
                )

            assert raised.value.failure.reason is (
                MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
            )
            assert built == []

        mismatched_artifact_store = ProviderSubrequestArtifactStore(
            tmp_path / "other-artifacts"
        )
        mismatched_dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=mismatched_artifact_store,
            provider_clients=clients,
            now=lambda: pytest.fail("dependency validation must not read the clock"),
            sleep=lambda _seconds: pytest.fail(
                "dependency validation must not sleep"
            ),
            owner_id="qualified-coverage-mismatch",
            lease_duration=timedelta(seconds=30),
        )
        built = []
        with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
            assemble_production_qualified_provider_sources(
                blueprint=blueprint,
                dependencies=mismatched_dependencies,
                source_registrations=_registrations(built),
            )

        assert raised.value.failure.reason is (
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_DEPENDENCY_MISSING
        )
        assert built == []

        registrations = _registrations([])
        unbound_registration = ProductionQualifiedSourceRegistration(
            provider_id=registrations[0].provider_id,
            endpoint_id=registrations[0].endpoint_id,
            source_kind=registrations[0].source_kind,
            capabilities=registrations[0].capabilities,
            source_factory=lambda _dependencies: lambda _request: None,
        )
        with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
            assemble_production_qualified_provider_sources(
                blueprint=blueprint,
                dependencies=dependencies,
                source_registrations=(unbound_registration, *registrations[1:]),
            )

        assert raised.value.failure.reason is (
            MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
        )


def test_route_provider_rejects_an_incompatible_physical_provider_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    clients = {
        provider_id: _ProviderClient()
        for provider_id in blueprint.factory_contract.dependency_ids
    }
    physical_calls = 0

    def incompatible_factory(dependencies):
        def physical_request(_request):
            nonlocal physical_calls
            physical_calls += 1
            return b"{}"

        return ProductionQualifiedBoundSource.bind(
            dependencies=dependencies,
            physical_provider_id="yfinance",
            physical_request=physical_request,
            response_decoder=lambda _request, _payload, _result: None,
            adapter=physical_request,
            normalizer_version="financial-indicators-v1",
        )

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="qualified-provider-mismatch",
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients=clients,
            now=lambda: pytest.fail("provider validation must not read the clock"),
            sleep=lambda _seconds: pytest.fail("provider validation must not sleep"),
            owner_id="qualified-provider-mismatch",
            lease_duration=timedelta(seconds=30),
        )
        registrations = tuple(
            ProductionQualifiedSourceRegistration(
                provider_id=registration.provider_id,
                endpoint_id=registration.endpoint_id,
                source_kind=registration.source_kind,
                capabilities=registration.capabilities,
                source_factory=(
                    incompatible_factory
                    if registration.provider_id == "akshare"
                    and registration.source_kind
                    is ProductionQualifiedSourceKind.INDICATOR
                    else registration.source_factory
                ),
            )
            for registration in _registrations([])
        )

        with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
            assemble_production_qualified_provider_sources(
                blueprint=blueprint,
                dependencies=dependencies,
                source_registrations=registrations,
            )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID
    )
    assert physical_calls == 0


def test_source_identity_rejects_reserved_secret_material() -> None:
    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        ProductionQualifiedSourceRegistration(
            provider_id="yfinance",
            endpoint_id="fixture-token",
            source_kind=ProductionQualifiedSourceKind.INDICATOR,
            capabilities=(MainlandCapability.FINANCIAL_INDICATORS,),
            source_factory=lambda _dependencies: lambda _request: None,
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
    )


def test_factor_and_name_capabilities_require_independent_registrations() -> None:
    with pytest.raises(MainlandCapabilityRoutingPreflightError) as raised:
        ProductionQualifiedSourceRegistration(
            provider_id="akshare",
            endpoint_id="combined_factor_name",
            source_kind=ProductionQualifiedSourceKind.CAPABILITY,
            capabilities=(
                MainlandCapability.ADJUSTMENT_FACTORS,
                MainlandCapability.NAME_EVENTS,
            ),
            source_factory=lambda _dependencies: pytest.fail(
                "invalid registration must not construct a source"
            ),
        )

    assert raised.value.failure.reason is (
        MainlandCapabilityRoutingFailureReason.QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE
    )


class _BaoStockIncompleteFactors(_BaoStockCapabilities):
    def query_forward_adjustment_factors(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame("factors", pd.DataFrame())


class _EmptyTushareFactorNames(_TushareFactorNames):
    def adj_factor(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        self.endpoints.append("adj_factor")
        return pd.DataFrame()


def test_factor_fallback_never_promotes_tushare_or_yahoo_to_strict_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(
        tmp_path,
        monkeypatch,
        enabled=["adjustment_factors", "name_events"],
    )
    tushare = _TushareFactorNames()
    baostock = _BaoStockIncompleteFactors()

    def unavailable_capability(provider_id: str, capability: MainlandCapability):
        return lambda _request: FinancialCapabilityProviderResponse.unavailable(
            provider_id=provider_id,
            capability=capability,
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    sources = {
        "adjustment_factors": unavailable_capability(
            "akshare", MainlandCapability.ADJUSTMENT_FACTORS
        ),
        "derived_adjustment_factors": unavailable_capability(
            "yfinance_derived", MainlandCapability.ADJUSTMENT_FACTORS
        ),
        "issuer_name_events": unavailable_capability(
            "akshare", MainlandCapability.NAME_EVENTS
        ),
    }
    clients = {
        "akshare": _ProviderClient(),
        "baostock": baostock,
        "tushare": tushare,
        "yfinance": _ProviderClient(),
    }
    request = FinancialCapabilityRoutingRequest(
        instrument_identity=_identity(),
        capability=MainlandCapability.ADJUSTMENT_FACTORS,
        range_start=date(2026, 7, 23),
        as_of_date=date(2026, 7, 24),
    )

    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources=sources,
        run_scope_id="qualified-factor-fallback",
        coordinated_providers=("baostock", "tushare"),
    ) as composition:
        factors = composition.router.route_capability(request)
        names = composition.router.route_capability(
            request.model_copy(update={"capability": MainlandCapability.NAME_EVENTS})
        )

    assert tuple(call.provider_id for call in factors.provider_calls) == (
        "baostock",
        "tushare",
    )
    assert factors.selected_provider == "tushare"
    assert factors.strict_bundle_established is False
    assert factors.provider_history_bundle_identity is None
    assert names.selected_provider == "tushare"
    assert tushare.endpoints == ["adj_factor", "namechange"]
    assert factors.provider_calls[1].subrequest_keys != names.provider_calls[0].subrequest_keys

    yahoo_tushare = _EmptyTushareFactorNames()
    yahoo_baostock = _BaoStockIncompleteFactors()

    def yahoo_derived(_request):
        return FinancialCapabilityProviderResponse.available(
            provider_id="yfinance_derived",
            capability=MainlandCapability.ADJUSTMENT_FACTORS,
            retained_artifact_identities=("yahoo-derived-factor-artifact",),
            candidate_count=1,
            subrequest_keys=("provider-subrequest:v1:" + "f" * 64,),
            physical_attempt_ids=(),
            derived=True,
            degraded=True,
        )

    yahoo_sources = {
        **sources,
        "derived_adjustment_factors": yahoo_derived,
    }
    yahoo_clients = {
        "akshare": _ProviderClient(),
        "baostock": yahoo_baostock,
        "tushare": yahoo_tushare,
        "yfinance": _ProviderClient(),
    }
    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path / "yahoo",
        clients=yahoo_clients,
        sources=yahoo_sources,
        run_scope_id="qualified-yahoo-factor",
        coordinated_providers=("baostock", "tushare"),
    ) as composition:
        yahoo = composition.router.route_capability(request)

    assert tuple(call.provider_id for call in yahoo.provider_calls) == (
        "baostock",
        "tushare",
        "akshare",
        "yfinance_derived",
    )
    assert yahoo.selected_provider == "yfinance_derived"
    assert yahoo.strict_bundle_established is False
    assert yahoo.derived is True
    assert yahoo.degraded is True


def test_yahoo_factor_registration_cannot_omit_derived_degraded_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch, enabled=["adjustment_factors"])

    def unavailable_capability(provider_id: str):
        return lambda request: FinancialCapabilityProviderResponse.unavailable(
            provider_id=provider_id,
            capability=request.capability,
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    def mislabeled_yahoo(request):
        return FinancialCapabilityProviderResponse.available(
            provider_id="yfinance_derived",
            capability=request.capability,
            retained_artifact_identities=("safe-yahoo-factor-artifact",),
            candidate_count=1,
            subrequest_keys=(),
            physical_attempt_ids=(),
        )

    sources = {
        "adjustment_factors": unavailable_capability("akshare"),
        "derived_adjustment_factors": mislabeled_yahoo,
        "issuer_name_events": unavailable_capability("akshare"),
    }
    clients = {
        "akshare": _ProviderClient(),
        "baostock": _BaoStockIncompleteFactors(),
        "tushare": _EmptyTushareFactorNames(),
        "yfinance": _ProviderClient(),
    }
    request = FinancialCapabilityRoutingRequest(
        instrument_identity=_identity(),
        capability=MainlandCapability.ADJUSTMENT_FACTORS,
        range_start=date(2026, 7, 23),
        as_of_date=date(2026, 7, 24),
    )

    with _assembled_runtime(
        blueprint=blueprint,
        tmp_path=tmp_path,
        clients=clients,
        sources=sources,
        run_scope_id="qualified-yahoo-label-safety",
        coordinated_providers=("baostock", "tushare"),
    ) as composition:
        routed = composition.router.route_capability(request)

    assert tuple(call.provider_id for call in routed.provider_calls) == (
        "baostock",
        "tushare",
        "akshare",
        "yfinance_derived",
    )
    assert routed.selected_provider is None
    assert routed.provider_calls[-1].reason is AcquisitionUnavailableReason.PROVIDER_ERROR
    assert routed.derived is False
    assert routed.degraded is False

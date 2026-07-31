from __future__ import annotations

import copy
import importlib.util
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityProviderResponse,
    FinancialCapabilityRoutingRequest,
    FinancialIndicatorProviderGapRequest,
    FinancialIndicatorRoutingRequest,
    FinancialProviderGapRequest,
    FinancialProviderPeriodResponse,
    FinancialStatementRoutingRequest,
    MainlandFinancialCapabilityRouter,
    financial_period_response_from_indicator_adapter,
    financial_period_response_from_statement_adapter,
    render_indicator_routing_result,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialListingProvenance,
    FinancialPeriodCandidate,
    FinancialPeriodIdentity,
    FinancialPeriodRejectionReason,
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    financial_ratio_field_declaration,
    financial_statement_field_declaration,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialProvider,
    FinancialProviderVariant,
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)
from tradingagents.graph.financial_tools import (
    FinancialDispatchToolNode,
    QualifiedFinancialRoutingComposition,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph

_OBSERVED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
_ANNUAL_PERIODS = tuple(date(year, 12, 31) for year in range(2021, 2026))
_REPORTING_PERIODS = (
    date(2024, 9, 30),
    date(2024, 12, 31),
    date(2025, 3, 31),
    date(2025, 6, 30),
    date(2025, 9, 30),
    date(2025, 12, 31),
    date(2026, 3, 31),
    date(2026, 6, 30),
)


def _identity() -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol="600895.SS",
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


def _qualified_plan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account_scope_label: str = "personal-research-primary",
):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": [
                "statements",
                "financial_indicators",
                "adjustment_factors",
                "name_events",
            ],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": account_scope_label,
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    preflight = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "deterministic-fixture-only"},
    )
    assert preflight.plan is not None
    return preflight.plan


def _candidate(
    *,
    provider_id: str,
    statement_type: FinancialStatementType,
    period_end: date,
    frequency: FinancialReportingFrequency,
    value: str = "1",
    company_type: FinancialCompanyType = FinancialCompanyType.INDUSTRIAL_NON_BANK,
    currency: str = "CNY",
    consolidation_scope: FinancialConsolidationScope = (
        FinancialConsolidationScope.CONSOLIDATED
    ),
    ann_date: date | None = date(2026, 7, 1),
    f_ann_date: date | None = date(2026, 7, 2),
    core_field_limit: int | None = None,
    normalized_unit: str | None = None,
    instrument_identity: InstrumentIdentityEvidence | None = None,
) -> FinancialPeriodCandidate:
    identity = instrument_identity or _identity()
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id=provider_id,
        endpoint_id=statement_type.value,
        dataset_id="single_stock_history",
        schema_identity=f"{provider_id}-{statement_type.value}-fixture-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": identity.symbol,
            "period_end": period_end.isoformat(),
            "frequency": frequency.value,
        },
        provider_metadata={"report_type": "1", "comp_type": "1"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload={"period_end": period_end.isoformat(), "value": value},
    )
    declaration_company_type = (
        FinancialCompanyType.INDUSTRIAL_NON_BANK
        if company_type is FinancialCompanyType.UNKNOWN
        else company_type
    )
    declaration = financial_statement_field_declaration(
        declaration_company_type,
        statement_type,
    )
    selected_fields = declaration.core_fields[:core_field_limit]
    fields = tuple(
        FinancialFieldValue(
            provider_field=field_name,
            original_value=value,
            original_unit=normalized_unit or declaration.normalized_unit,
            normalized_field=field_name,
            normalized_value=value,
            normalized_unit=normalized_unit or declaration.normalized_unit,
        )
        for field_name in selected_fields
    )
    resolution = resolve_financial_company_type(
        provider_declared_type=company_type,
        provider_declaration_qualified=company_type is not FinancialCompanyType.UNKNOWN,
        classifier_metadata={},
        present_fields=(
            () if company_type is FinancialCompanyType.UNKNOWN else selected_fields
        ),
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=identity,
            capability=FinancialCapability.STATEMENT,
            statement_type=statement_type,
            ratio_family=None,
            period_end=period_end,
            frequency=frequency,
            company_type=company_type,
            consolidation_scope=consolidation_scope,
            currency=currency,
            provider_revision_binding=artifact.artifact_identity,
        ),
        filing_metadata=FinancialFilingMetadata(
            ann_date=ann_date,
            f_ann_date=f_ann_date,
            report_type="1",
            comp_type="2" if company_type is FinancialCompanyType.BANK else "1",
            update_flag="0",
            retrieved_at=_OBSERVED_AT,
            observed_at=_OBSERVED_AT,
            local_provider_revision_identity=artifact.artifact_identity,
        ),
        company_type_resolution=resolution,
        fields=fields,
    )


def _ratio_candidate(
    *,
    provider_id: str,
    ratio_family: FinancialRatioFamily,
    period_end: date,
    value: str = "1",
    f_ann_date: date | None = date(2026, 7, 2),
    instrument_identity: InstrumentIdentityEvidence | None = None,
) -> FinancialPeriodCandidate:
    identity = instrument_identity or _identity()
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id=provider_id,
        endpoint_id="financial_indicators",
        dataset_id="single_stock_history",
        schema_identity=f"{provider_id}-{ratio_family.value}-fixture-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": identity.symbol,
            "period_end": period_end.isoformat(),
            "ratio_family": ratio_family.value,
        },
        provider_metadata={"report_type": "1", "comp_type": "1"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload={"period_end": period_end.isoformat(), "value": value},
    )
    declaration = financial_ratio_field_declaration(
        FinancialCompanyType.INDUSTRIAL_NON_BANK,
        ratio_family,
    )
    fields = tuple(
        FinancialFieldValue(
            provider_field=field_name,
            original_value=value,
            original_unit=declaration.normalized_unit,
            normalized_field=field_name,
            normalized_value=value,
            normalized_unit=declaration.normalized_unit,
        )
        for field_name in declaration.normalized_fields
    )
    resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        provider_declaration_qualified=True,
        classifier_metadata={},
        present_fields=declaration.normalized_fields,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=identity,
            capability=FinancialCapability.RATIO_FAMILY,
            statement_type=None,
            ratio_family=ratio_family,
            period_end=period_end,
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            provider_revision_binding=artifact.artifact_identity,
        ),
        filing_metadata=FinancialFilingMetadata(
            ann_date=date(2026, 7, 1),
            f_ann_date=f_ann_date,
            report_type="1",
            comp_type="1",
            update_flag="0",
            retrieved_at=_OBSERVED_AT,
            observed_at=_OBSERVED_AT,
            local_provider_revision_identity=artifact.artifact_identity,
        ),
        company_type_resolution=resolution,
        fields=fields,
    )


def _available_response(
    provider_id: str,
    candidates: tuple[FinancialPeriodCandidate, ...],
) -> FinancialProviderPeriodResponse:
    artifacts = tuple(candidate.artifact for candidate in candidates)
    return FinancialProviderPeriodResponse.available(
        provider_id=provider_id,
        response_artifact=artifacts[0],
        retained_artifacts=artifacts,
        candidates=candidates,
        subrequest_keys=(f"{provider_id}-bulk-statement",),
        physical_attempt_ids=(f"{provider_id}-attempt-1",),
    )


def _statement_request() -> FinancialStatementRoutingRequest:
    return FinancialStatementRoutingRequest(
        instrument_identity=_identity(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=_ANNUAL_PERIODS,
        eligible_reporting_period_ends=_REPORTING_PERIODS,
    )


def test_complete_tushare_statement_coverage_stops_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, FinancialProviderGapRequest]] = []
    candidates = tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.ANNUAL,
        )
        for period in _ANNUAL_PERIODS
    ) + tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
        )
        for period in _REPORTING_PERIODS
    )

    def source(provider_id: str, response: FinancialProviderPeriodResponse):
        def invoke(request: FinancialProviderGapRequest) -> FinancialProviderPeriodResponse:
            calls.append((provider_id, request))
            return response

        return invoke

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": source("tushare", _available_response("tushare", candidates)),
            "akshare_sina": source(
                "akshare_sina",
                FinancialProviderPeriodResponse.unavailable(
                    provider_id="akshare_sina",
                    reason="no_data",
                ),
            ),
            "yfinance": source(
                "yfinance",
                FinancialProviderPeriodResponse.unavailable(
                    provider_id="yfinance",
                    reason="no_data",
                ),
            ),
        },
    )

    result = router.route_statement(_statement_request())

    assert [provider_id for provider_id, _ in calls] == ["tushare"]
    assert result.route == ("tushare", "akshare_sina", "yfinance")
    assert len(result.manifest.selections) == 13
    assert {item.provider_id for item in result.manifest.selections} == {"tushare"}
    assert result.manifest.aggregate_completeness.complete is True
    assert result.missing_annual_period_ends == ()
    assert result.missing_reporting_period_ends == ()
    assert result.physical_attempt_ids == ("tushare-attempt-1",)


def test_statement_failures_fall_back_independently_while_siblings_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = date(2026, 6, 30)
    calls: list[tuple[str, FinancialStatementType]] = []

    def tushare(
        request: FinancialProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        calls.append(("tushare", request.statement_type))
        if request.statement_type is FinancialStatementType.BALANCE_SHEET:
            return FinancialProviderPeriodResponse.unavailable(
                provider_id="tushare",
                reason="provider_error",
                physical_attempt_ids=("tushare-balance-attempt",),
            )
        candidate = _candidate(
            provider_id="tushare",
            statement_type=request.statement_type,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
        )
        return _available_response("tushare", (candidate,))

    def akshare(
        request: FinancialProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        calls.append(("akshare_sina", request.statement_type))
        candidate = _candidate(
            provider_id="akshare_sina",
            statement_type=request.statement_type,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
        )
        return _available_response("akshare_sina", (candidate,))

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": tushare,
            "akshare_sina": akshare,
        },
    )
    results = tuple(
        router.route_statement(
            FinancialStatementRoutingRequest(
                instrument_identity=_identity(),
                statement_type=statement_type,
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                eligible_annual_period_ends=(),
                eligible_reporting_period_ends=(period,),
            )
        )
        for statement_type in (
            FinancialStatementType.BALANCE_SHEET,
            FinancialStatementType.INCOME_STATEMENT,
            FinancialStatementType.CASH_FLOW,
        )
    )

    assert calls == [
        ("tushare", FinancialStatementType.BALANCE_SHEET),
        ("akshare_sina", FinancialStatementType.BALANCE_SHEET),
        ("tushare", FinancialStatementType.INCOME_STATEMENT),
        ("tushare", FinancialStatementType.CASH_FLOW),
    ]
    assert [
        result.selected_candidates[0].artifact.dataset.provider_id
        for result in results
    ] == ["akshare_sina", "tushare", "tushare"]
    assert results[0].manifest.acquisition_outcomes[1].reason is (
        AcquisitionUnavailableReason.PROVIDER_ERROR
    )


def test_mixed_periods_fall_back_only_gaps_and_preserve_tushare_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, FinancialProviderGapRequest]] = []
    missing_period = _REPORTING_PERIODS[-1]
    complete = tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.ANNUAL,
        )
        for period in _ANNUAL_PERIODS
    ) + tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
        )
        for period in _REPORTING_PERIODS
        if period != missing_period
    )
    sparse = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=missing_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        core_field_limit=1,
    )
    akshare_fill = _candidate(
        provider_id="akshare_sina",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=missing_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
    )

    def source(provider_id: str, response: FinancialProviderPeriodResponse):
        def invoke(request: FinancialProviderGapRequest) -> FinancialProviderPeriodResponse:
            calls.append((provider_id, request))
            return response

        return invoke

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": source(
                "tushare",
                _available_response("tushare", (*complete, sparse)),
            ),
            "akshare_sina": source(
                "akshare_sina",
                _available_response("akshare_sina", (akshare_fill,)),
            ),
            "yfinance": source(
                "yfinance",
                FinancialProviderPeriodResponse.unavailable(
                    provider_id="yfinance",
                    reason="no_data",
                ),
            ),
        },
    )

    result = router.route_statement(_statement_request())

    assert [provider for provider, _ in calls] == ["tushare", "akshare_sina"]
    assert calls[1][1].missing_annual_period_ends == ()
    assert calls[1][1].missing_reporting_period_ends == (missing_period,)
    assert calls[1][1].conflicted_reporting_period_ends == ()
    selected_by_period = {
        candidate.period_identity.period_end: candidate.artifact.dataset.provider_id
        for candidate in result.selected_candidates
        if candidate.period_identity.frequency is FinancialReportingFrequency.QUARTERLY
    }
    assert selected_by_period[missing_period] == "akshare_sina"
    assert set(selected_by_period.values()) == {"tushare", "akshare_sina"}
    assert result.manifest.aggregate_completeness.complete is True
    assert any(
        FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in item.reasons
        for item in result.manifest.rejected_periods
    )


def test_yahoo_final_gap_is_current_only_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _REPORTING_PERIODS[-1]
    yahoo = _candidate(
        provider_id="yfinance",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=target,
        frequency=FinancialReportingFrequency.QUARTERLY,
        f_ann_date=None,
    )
    calls: list[str] = []

    def unavailable(provider_id: str):
        def invoke(_request: FinancialProviderGapRequest) -> FinancialProviderPeriodResponse:
            calls.append(provider_id)
            return FinancialProviderPeriodResponse.unavailable(
                provider_id=provider_id,
                reason="no_data",
                physical_attempt_ids=(f"{provider_id}-attempt-1",),
            )

        return invoke

    def yahoo_source(
        _request: FinancialProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        calls.append("yfinance")
        return _available_response("yfinance", (yahoo,))

    request = _statement_request().model_copy(
        update={
            "eligible_annual_period_ends": (),
            "eligible_reporting_period_ends": (target,),
        }
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": unavailable("tushare"),
            "akshare_sina": unavailable("akshare_sina"),
            "yfinance": yahoo_source,
        },
    ).route_statement(request)

    assert calls == ["tushare", "akshare_sina", "yfinance"]
    assert len(result.manifest.selections) == 1
    assert result.manifest.selections[0].provider_id == "yfinance"
    assert result.manifest.selections[0].disposition == "current_only"
    assert result.manifest.selections[0].strict_pit_eligible is False


def test_bank_company_type_and_since_listing_exception_are_period_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annual = (date(2025, 12, 31),)
    reporting = (date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30))
    candidates = tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.ANNUAL,
            company_type=FinancialCompanyType.BANK,
        )
        for period in annual
    ) + tuple(
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=FinancialCompanyType.BANK,
        )
        for period in reporting
    )
    request = FinancialStatementRoutingRequest(
        instrument_identity=_identity(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=annual,
        eligible_reporting_period_ends=reporting,
        listing_date=date(2025, 1, 15),
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref="issuer_lifecycle",
            observed_at=_OBSERVED_AT,
        ),
    )
    router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": lambda _request: _available_response("tushare", candidates),
        },
    )

    result = router.route_statement(request)

    exception = result.manifest.aggregate_completeness.since_listing_exception
    assert exception is not None
    assert exception.supported is True
    assert result.manifest.aggregate_completeness.complete is True
    assert {
        candidate.filing_metadata.comp_type for candidate in result.selected_candidates
    } == {"2"}

    missing = router.route_statement(
        request.model_copy(
            update={
                "eligible_reporting_period_ends": (
                    *reporting,
                    date(2026, 9, 30),
                )
            }
        )
    )
    missing_exception = (
        missing.manifest.aggregate_completeness.since_listing_exception
    )
    assert missing_exception is not None
    assert missing_exception.supported is False
    assert missing.manifest.aggregate_completeness.complete is False


def test_incompatible_candidate_rejects_only_its_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_period = _REPORTING_PERIODS[-2]
    bad_period = _REPORTING_PERIODS[-1]
    good = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=good_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
    )
    bad = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=bad_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        currency="USD",
        consolidation_scope=FinancialConsolidationScope.PARENT,
        normalized_unit="USD",
    )
    request = _statement_request().model_copy(
        update={
            "eligible_annual_period_ends": (),
            "eligible_reporting_period_ends": (good_period, bad_period),
        }
    )
    calls: list[FinancialProviderGapRequest] = []

    def akshare(request: FinancialProviderGapRequest) -> FinancialProviderPeriodResponse:
        calls.append(request)
        return FinancialProviderPeriodResponse.unavailable(
            provider_id="akshare_sina",
            reason="no_data",
        )

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": lambda _request: _available_response("tushare", (good, bad)),
            "akshare_sina": akshare,
        },
    ).route_statement(request)

    assert len(result.selected_candidates) == 1
    assert result.selected_candidates[0].period_identity.period_end == good_period
    assert calls[0].missing_reporting_period_ends == (bad_period,)
    assert calls[0].conflicted_reporting_period_ends == ()
    reasons = result.manifest.rejected_periods[0].reasons
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY in reasons
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE in reasons
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT in reasons


def test_equivalent_overlap_is_retained_unselected_and_conflict_is_period_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlap_period = _REPORTING_PERIODS[-1]
    independent_period = _REPORTING_PERIODS[-2]
    first_overlap = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=overlap_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
    )
    independent = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=independent_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
    )
    equivalent = _candidate(
        provider_id="akshare_sina",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=overlap_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
    )
    request = _statement_request().model_copy(
        update={
            "eligible_annual_period_ends": (),
            "eligible_reporting_period_ends": (
                independent_period,
                overlap_period,
                date(2024, 3, 31),
            ),
        }
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": lambda _request: _available_response(
                "tushare",
                (first_overlap, independent),
            ),
            "akshare_sina": lambda _request: _available_response(
                "akshare_sina",
                (equivalent,),
            ),
        },
    ).route_statement(request)

    assert len(result.manifest.overlaps) == 1
    assert result.manifest.overlaps[0].disposition == "equivalent_unselected"
    selected_overlap = [
        item
        for item in result.selected_candidates
        if item.period_identity.period_end == overlap_period
    ]
    assert len(selected_overlap) == 1
    assert selected_overlap[0].artifact.dataset.provider_id == "tushare"
    assert len(result.manifest.provider_candidates) == 3

    conflicting = _candidate(
        provider_id="akshare_sina",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=overlap_period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        value="2",
    )
    conflicted = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": lambda _request: _available_response(
                "tushare",
                (first_overlap, independent),
            ),
            "akshare_sina": lambda _request: _available_response(
                "akshare_sina",
                (conflicting,),
            ),
        },
    ).route_statement(request)

    assert conflicted.conflicted_reporting_period_ends == (overlap_period,)
    assert {
        item.period_identity.period_end for item in conflicted.selected_candidates
    } == {independent_period}
    assert conflicted.manifest.conflicts
    assert all(
        overlap_period
        in {
            first_overlap.period_identity.period_end,
            conflicting.period_identity.period_end,
        }
        for _ in conflicted.manifest.conflicts
    )


def test_ratio_artifact_cannot_satisfy_statement_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = _REPORTING_PERIODS[-1]
    ratio = _ratio_candidate(
        provider_id="tushare",
        ratio_family=FinancialRatioFamily.PROFIT,
        period_end=period,
    )
    request = _statement_request().model_copy(
        update={
            "eligible_annual_period_ends": (),
            "eligible_reporting_period_ends": (period,),
        }
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": lambda _request: _available_response("tushare", (ratio,)),
        },
    ).route_statement(request)

    assert result.selected_candidates == ()
    assert result.missing_reporting_period_ends == (period,)
    assert FinancialPeriodRejectionReason.CAPABILITY_MISMATCH in (
        result.manifest.rejected_periods[0].reasons
    )


def test_indicator_routing_calls_only_missing_families_and_exact_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tushare_profit = tuple(
        _ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=period,
        )
        for period in _REPORTING_PERIODS
    )
    tushare_growth = tuple(
        _ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.GROWTH,
            period_end=period,
        )
        for period in _REPORTING_PERIODS[:-1]
    )
    akshare_growth = _ratio_candidate(
        provider_id="akshare",
        ratio_family=FinancialRatioFamily.GROWTH,
        period_end=_REPORTING_PERIODS[-1],
    )
    calls: list[tuple[str, FinancialIndicatorProviderGapRequest]] = []

    def source(provider_id: str, response: FinancialProviderPeriodResponse):
        def invoke(
            request: FinancialIndicatorProviderGapRequest,
        ) -> FinancialProviderPeriodResponse:
            calls.append((provider_id, request))
            return response

        return invoke

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            "tushare": source(
                "tushare",
                _available_response(
                    "tushare",
                    (*tushare_profit, *tushare_growth),
                ),
            ),
            "akshare": source(
                "akshare",
                _available_response("akshare", (akshare_growth,)),
            ),
            "baostock_qualified_families": source(
                "baostock_qualified_families",
                FinancialProviderPeriodResponse.unavailable(
                    provider_id="baostock_qualified_families",
                    reason="no_data",
                ),
            ),
        },
    )
    result = router.route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(
                FinancialRatioFamily.PROFIT,
                FinancialRatioFamily.GROWTH,
            ),
            eligible_reporting_period_ends=_REPORTING_PERIODS,
        )
    )

    assert [provider for provider, _ in calls] == ["tushare", "akshare"]
    assert calls[1][1].missing_periods_by_family == (
        (FinancialRatioFamily.GROWTH, (_REPORTING_PERIODS[-1],)),
    )
    assert set(result.complete_families) == {
        FinancialRatioFamily.PROFIT,
        FinancialRatioFamily.GROWTH,
    }
    assert result.missing_periods_by_family == ()
    assert len(result.family_manifests) == 2


def test_complete_tushare_indicators_stop_lower_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = tuple(
        _ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=period,
        )
        for period in _REPORTING_PERIODS
    )
    calls: list[str] = []

    def source(provider_id: str, response: FinancialProviderPeriodResponse):
        def invoke(
            _request: FinancialIndicatorProviderGapRequest,
        ) -> FinancialProviderPeriodResponse:
            calls.append(provider_id)
            return response

        return invoke

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            "tushare": source(
                "tushare",
                _available_response("tushare", candidates),
            ),
            "akshare": source(
                "akshare",
                FinancialProviderPeriodResponse.unavailable(
                    provider_id="akshare",
                    reason="no_data",
                ),
            ),
        },
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=_REPORTING_PERIODS,
        )
    )

    assert calls == ["tushare"]
    assert result.complete_families == (FinancialRatioFamily.PROFIT,)
    assert result.physical_attempt_ids == ("tushare-attempt-1",)


def test_sparse_baostock_family_preserves_sibling_and_yahoo_is_degraded_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profit = tuple(
        _ratio_candidate(
            provider_id="baostock_qualified_families",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=period,
        )
        for period in _REPORTING_PERIODS
    )
    growth = tuple(
        _ratio_candidate(
            provider_id="baostock_qualified_families",
            ratio_family=FinancialRatioFamily.GROWTH,
            period_end=period,
        )
        for period in _REPORTING_PERIODS[:-1]
    )
    baostock_artifacts = tuple(
        candidate.artifact for candidate in (*profit, *growth)
    )
    baostock = FinancialProviderPeriodResponse.available(
        provider_id="baostock_qualified_families",
        response_artifact=baostock_artifacts[0],
        retained_artifacts=baostock_artifacts,
        candidates=(*profit, *growth),
        subrequest_keys=tuple(
            f"baostock-{family.value}-{period.year}-Q{((period.month - 1) // 3) + 1}"
            for family in (FinancialRatioFamily.PROFIT, FinancialRatioFamily.GROWTH)
            for period in _REPORTING_PERIODS
        ),
        physical_attempt_ids=tuple(
            f"baostock-attempt-{index}"
            for index in range(1, len(_REPORTING_PERIODS) * 2 + 1)
        ),
    )
    yahoo_artifact = _ratio_candidate(
        provider_id="yfinance",
        ratio_family=FinancialRatioFamily.GROWTH,
        period_end=_REPORTING_PERIODS[-1],
    ).artifact
    yahoo = FinancialProviderPeriodResponse.available(
        provider_id="yfinance",
        response_artifact=yahoo_artifact,
        retained_artifacts=(yahoo_artifact,),
        candidates=(),
        subrequest_keys=("yahoo-current-profile",),
        physical_attempt_ids=("yahoo-attempt-1",),
        degraded_current_profile=True,
    )
    calls: list[tuple[str, FinancialIndicatorProviderGapRequest]] = []

    def unavailable(provider_id: str):
        def invoke(
            request: FinancialIndicatorProviderGapRequest,
        ) -> FinancialProviderPeriodResponse:
            calls.append((provider_id, request))
            return FinancialProviderPeriodResponse.unavailable(
                provider_id=provider_id,
                reason="no_data",
            )

        return invoke

    def available(provider_id: str, response: FinancialProviderPeriodResponse):
        def invoke(
            request: FinancialIndicatorProviderGapRequest,
        ) -> FinancialProviderPeriodResponse:
            calls.append((provider_id, request))
            return response

        return invoke

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            "tushare": unavailable("tushare"),
            "akshare": unavailable("akshare"),
            "baostock_qualified_families": available(
                "baostock_qualified_families",
                baostock,
            ),
            "yfinance": available("yfinance", yahoo),
        },
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(
                FinancialRatioFamily.PROFIT,
                FinancialRatioFamily.GROWTH,
            ),
            eligible_reporting_period_ends=_REPORTING_PERIODS,
        )
    )

    assert [provider for provider, _ in calls] == [
        "tushare",
        "akshare",
        "baostock_qualified_families",
        "yfinance",
    ]
    assert calls[2][1].missing_periods_by_family == (
        (FinancialRatioFamily.PROFIT, tuple(reversed(_REPORTING_PERIODS))),
        (FinancialRatioFamily.GROWTH, tuple(reversed(_REPORTING_PERIODS))),
    )
    assert calls[3][1].missing_periods_by_family == (
        (FinancialRatioFamily.GROWTH, (_REPORTING_PERIODS[-1],)),
    )
    assert result.complete_families == (FinancialRatioFamily.PROFIT,)
    assert result.missing_periods_by_family == (
        (FinancialRatioFamily.GROWTH, (_REPORTING_PERIODS[-1],)),
    )
    assert result.current_profile_degraded is True
    assert result.current_profile_provider == "yfinance"
    assert len(result.physical_attempt_ids) == 17
    assert (
        '"status":"insufficient_history_with_degraded_current_profile"'
        in render_indicator_routing_result(result)
    )


def test_indicator_adapter_projection_retains_every_subrequest_and_row_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = _REPORTING_PERIODS[-1]
    candidate = _ratio_candidate(
        provider_id="tushare",
        ratio_family=FinancialRatioFamily.PROFIT,
        period_end=period,
    )
    available = SimpleNamespace(
        request=SimpleNamespace(subrequest_key="provider-subrequest:v1:" + "1" * 64),
        outcome=SimpleNamespace(kind=SimpleNamespace(value="available")),
        attempt_events=(
            SimpleNamespace(
                attempt_event_id="provider-physical-attempt=sha256:" + "2" * 64
            ),
        ),
        artifact=candidate.artifact,
        row_artifacts=(),
        candidates=(candidate,),
        row_rejections=(
            SimpleNamespace(
                row_identity="tushare-indicator-row:v1:" + "3" * 64,
                artifact_identity=candidate.artifact.artifact_identity,
                ratio_family=FinancialRatioFamily.PROFIT,
                candidate_identity=None,
                reasons=(FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,),
            ),
        ),
    )
    empty = SimpleNamespace(
        request=SimpleNamespace(subrequest_key="provider-subrequest:v1:" + "4" * 64),
        outcome=SimpleNamespace(kind=SimpleNamespace(value="empty")),
        attempt_events=(
            SimpleNamespace(
                attempt_event_id="provider-physical-attempt=sha256:" + "5" * 64
            ),
        ),
        artifact=None,
        row_artifacts=(),
        candidates=(),
        row_rejections=(),
    )
    response = financial_period_response_from_indicator_adapter(
        provider_id="tushare",
        result=SimpleNamespace(operational_results=(available, empty)),
    )

    assert tuple(item.outcome for item in response.subrequest_outcomes) == (
        "available",
        "unavailable",
    )
    assert response.subrequest_outcomes[1].reason is (
        AcquisitionUnavailableReason.EMPTY_FRAME
    )
    assert len(response.adapter_row_rejections) == 1
    assert response.adapter_row_rejections[0].reasons == (
        FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,
    )

    routed = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={"tushare": lambda _request: response},
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=(period,),
        )
    )

    reasons = tuple(
        outcome.reason
        for outcome in routed.family_manifests[0].acquisition_outcomes
        if outcome.reason is not None
    )
    assert AcquisitionUnavailableReason.EMPTY_FRAME in reasons
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA in reasons
    assert routed.provider_calls[0].adapter_row_rejections == (
        response.adapter_row_rejections
    )


def test_statement_adapter_projection_retains_subrequest_and_row_rejection() -> None:
    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=_ANNUAL_PERIODS[-1],
        frequency=FinancialReportingFrequency.ANNUAL,
    )
    response = financial_period_response_from_statement_adapter(
        provider_id="tushare",
        result=SimpleNamespace(
            subrequest_key="provider-subrequest:v1:" + "6" * 64,
            outcome=SimpleNamespace(kind=SimpleNamespace(value="available")),
            attempt_events=(
                SimpleNamespace(
                    attempt_event_id="provider-physical-attempt=sha256:" + "7" * 64
                ),
            ),
            artifact=candidate.artifact,
            row_artifacts=(candidate.artifact,),
            annual_candidates=(candidate,),
            reporting_period_candidates=(),
            row_rejections=(
                SimpleNamespace(
                    row_identity="tushare-statement-row:v1:" + "8" * 64,
                    artifact_identity=candidate.artifact.artifact_identity,
                    candidate_identities=(candidate.candidate_identity,),
                    reasons=(
                        FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,
                    ),
                ),
            ),
        ),
    )

    assert tuple(item.outcome for item in response.subrequest_outcomes) == (
        "available",
    )
    assert response.adapter_row_rejections[0].candidate_identities == (
        candidate.candidate_identity,
    )
    assert response.adapter_row_rejections[0].reasons == (
        FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,
    )


def test_indicator_equivalent_overlap_is_retained_and_unselected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlapping_period = _REPORTING_PERIODS[-2]
    final_gap = _REPORTING_PERIODS[-1]
    tushare_candidates = tuple(
        _ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=period,
        )
        for period in _REPORTING_PERIODS
        if period != final_gap
    )
    akshare_candidates = (
        _ratio_candidate(
            provider_id="akshare",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=final_gap,
        ),
        _ratio_candidate(
            provider_id="akshare",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=overlapping_period,
        ),
    )
    lower_calls: list[FinancialIndicatorProviderGapRequest] = []

    def unavailable(
        request: FinancialIndicatorProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        lower_calls.append(request)
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=request.provider_id,
            reason=AcquisitionUnavailableReason.NO_DATA,
        )

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            "tushare": lambda _request: _available_response(
                "tushare",
                tushare_candidates,
            ),
            "akshare": lambda _request: _available_response(
                "akshare",
                akshare_candidates,
            ),
            "baostock_qualified_families": unavailable,
            "yfinance": unavailable,
        },
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=_REPORTING_PERIODS,
        )
    )

    manifest = result.family_manifests[0]
    assert len(manifest.overlaps) == 1
    assert manifest.overlaps[0].disposition == "equivalent_unselected"
    assert manifest.conflicts == ()
    assert {
        candidate.period_identity.period_end
        for candidate in result.selected_candidates
    } == set(_REPORTING_PERIODS)
    assert result.missing_periods_by_family == ()
    assert lower_calls == []


def test_factor_route_stops_after_complete_baostock_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def source(provider_id: str, *, strict_bundle_complete: bool = False):
        def invoke(
            request: FinancialCapabilityRoutingRequest,
        ) -> FinancialCapabilityProviderResponse:
            calls.append(provider_id)
            assert request.capability is MainlandCapability.ADJUSTMENT_FACTORS
            retained_artifacts = (f"provider-artifact:{provider_id}",)
            bundle_identity = None
            bundle_components: tuple[str, ...] = ()
            if strict_bundle_complete:
                bundle_components = (
                    "provider-artifact:baostock-raw",
                    "provider-artifact:baostock-factor",
                    "provider-artifact:baostock-status",
                )
                retained_artifacts = bundle_components
                bundle_identity = (
                    "baostock-provider-history-bundle:v1:" + "b" * 64
                )
            return FinancialCapabilityProviderResponse.available(
                provider_id=provider_id,
                capability=request.capability,
                retained_artifact_identities=retained_artifacts,
                candidate_count=1,
                subrequest_keys=(f"subrequest:{provider_id}",),
                physical_attempt_ids=(f"attempt:{provider_id}",),
                provider_history_bundle_identity=bundle_identity,
                strict_bundle_component_artifact_identities=bundle_components,
            )

        return invoke

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        capability_sources={
            MainlandCapability.ADJUSTMENT_FACTORS: {
                "baostock": source(
                    "baostock",
                    strict_bundle_complete=True,
                ),
                "tushare": source("tushare"),
                "akshare": source("akshare"),
                "yfinance_derived": source("yfinance_derived"),
            }
        },
    ).route_capability(
        FinancialCapabilityRoutingRequest(
            instrument_identity=_identity(),
            capability=MainlandCapability.ADJUSTMENT_FACTORS,
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
    )

    assert calls == ["baostock"]
    assert result.selected_provider == "baostock"
    assert result.strict_bundle_provider == "baostock"
    assert result.strict_bundle_established is True
    assert result.provider_history_bundle_identity == (
        "baostock-provider-history-bundle:v1:" + "b" * 64
    )
    assert result.degraded is False
    assert result.physical_attempt_ids == ("attempt:baostock",)


def test_factor_strict_bundle_requires_bound_raw_factor_and_status_artifacts() -> None:
    with pytest.raises(ValueError, match="complete BaoStock bundle binding"):
        FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=MainlandCapability.ADJUSTMENT_FACTORS,
            retained_artifact_identities=("provider-artifact:baostock-factor",),
            candidate_count=1,
            subrequest_keys=("subrequest:baostock-factor",),
            physical_attempt_ids=("attempt:baostock-factor",),
            provider_history_bundle_identity=(
                "baostock-provider-history-bundle:v1:" + "c" * 64
            ),
        )


def test_rejected_non_statement_artifact_is_retained_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def rejected(
        request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        calls.append("tushare")
        return FinancialCapabilityProviderResponse.rejected(
            provider_id="tushare",
            capability=request.capability,
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE,
            retained_artifact_identities=("provider-artifact:tushare-rejected",),
            subrequest_keys=("subrequest:tushare-rejected",),
            physical_attempt_ids=("attempt:tushare-rejected",),
        )

    def fallback(
        request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        calls.append("akshare")
        return FinancialCapabilityProviderResponse.available(
            provider_id="akshare",
            capability=request.capability,
            retained_artifact_identities=("provider-artifact:akshare-name",),
            candidate_count=1,
            subrequest_keys=("subrequest:akshare-name",),
            physical_attempt_ids=("attempt:akshare-name",),
        )

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        capability_sources={
            MainlandCapability.NAME_EVENTS: {
                "tushare": rejected,
                "akshare": fallback,
            }
        },
    ).route_capability(
        FinancialCapabilityRoutingRequest(
            instrument_identity=_identity(),
            capability=MainlandCapability.NAME_EVENTS,
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
    )

    assert calls == ["tushare", "akshare"]
    assert result.selected_provider == "akshare"
    assert result.retained_artifact_identities == (
        "provider-artifact:tushare-rejected",
        "provider-artifact:akshare-name",
    )
    assert result.physical_attempt_ids == (
        "attempt:tushare-rejected",
        "attempt:akshare-name",
    )


def test_factor_fallback_never_promotes_tushare_or_yahoo_to_strict_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unavailable(
        request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        calls.append("baostock")
        return FinancialCapabilityProviderResponse.unavailable(
            provider_id="baostock",
            capability=request.capability,
            reason=AcquisitionUnavailableReason.NO_DATA,
            physical_attempt_ids=("attempt:baostock",),
        )

    def tushare(
        request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        calls.append("tushare")
        return FinancialCapabilityProviderResponse.available(
            provider_id="tushare",
            capability=request.capability,
            retained_artifact_identities=("provider-artifact:tushare",),
            candidate_count=1,
            subrequest_keys=("subrequest:tushare",),
            physical_attempt_ids=("attempt:tushare",),
        )

    request = FinancialCapabilityRoutingRequest(
        instrument_identity=_identity(),
        capability=MainlandCapability.ADJUSTMENT_FACTORS,
        range_start=date(2021, 1, 1),
        as_of_date=date(2026, 7, 30),
    )
    tushare_result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        capability_sources={
            MainlandCapability.ADJUSTMENT_FACTORS: {
                "baostock": unavailable,
                "tushare": tushare,
            }
        },
    ).route_capability(request)

    assert calls == ["baostock", "tushare"]
    assert tushare_result.selected_provider == "tushare"
    assert tushare_result.strict_bundle_provider == "baostock"
    assert tushare_result.strict_bundle_established is False
    assert tushare_result.degraded is False

    yahoo_calls: list[str] = []

    def yahoo(
        routed_request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        yahoo_calls.append("yfinance_derived")
        return FinancialCapabilityProviderResponse.available(
            provider_id="yfinance_derived",
            capability=routed_request.capability,
            retained_artifact_identities=("provider-artifact:yfinance-derived",),
            candidate_count=1,
            subrequest_keys=("subrequest:yfinance-derived",),
            physical_attempt_ids=("attempt:yfinance-derived",),
            derived=True,
            degraded=True,
        )

    yahoo_result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        capability_sources={
            MainlandCapability.ADJUSTMENT_FACTORS: {
                "baostock": lambda routed_request: (
                    FinancialCapabilityProviderResponse.unavailable(
                        provider_id="baostock",
                        capability=routed_request.capability,
                        reason=AcquisitionUnavailableReason.NO_DATA,
                    )
                ),
                "tushare": lambda routed_request: (
                    FinancialCapabilityProviderResponse.unavailable(
                        provider_id="tushare",
                        capability=routed_request.capability,
                        reason=AcquisitionUnavailableReason.NO_DATA,
                    )
                ),
                "akshare": lambda routed_request: (
                    FinancialCapabilityProviderResponse.unavailable(
                        provider_id="akshare",
                        capability=routed_request.capability,
                        reason=AcquisitionUnavailableReason.NO_DATA,
                    )
                ),
                "yfinance_derived": yahoo,
            }
        },
    ).route_capability(request)

    assert yahoo_calls == ["yfinance_derived"]
    assert yahoo_result.selected_provider == "yfinance_derived"
    assert yahoo_result.strict_bundle_established is False
    assert yahoo_result.derived is True
    assert yahoo_result.degraded is True


def test_status_and_name_routes_preserve_authority_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_calls: list[str] = []

    def status(
        request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        status_calls.append("baostock")
        return FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=request.capability,
            retained_artifact_identities=("provider-artifact:baostock-status",),
            candidate_count=1,
            subrequest_keys=("subrequest:baostock-status",),
            physical_attempt_ids=("attempt:baostock-status",),
            current_tradeability="suspended",
        )

    name_calls: list[str] = []

    def name(
        request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        name_calls.append("tushare")
        return FinancialCapabilityProviderResponse.available(
            provider_id="tushare",
            capability=request.capability,
            retained_artifact_identities=("provider-artifact:tushare-name",),
            candidate_count=1,
            subrequest_keys=("subrequest:tushare-name",),
            physical_attempt_ids=("attempt:tushare-name",),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        capability_sources={
            MainlandCapability.SUSPENSION_STATUS: {"baostock": status},
            MainlandCapability.NAME_EVENTS: {"tushare": name},
        },
    )
    status_result = router.route_capability(
        FinancialCapabilityRoutingRequest(
            instrument_identity=_identity(),
            capability=MainlandCapability.SUSPENSION_STATUS,
            range_start=date(2026, 7, 30),
            as_of_date=date(2026, 7, 30),
        )
    )
    name_result = router.route_capability(
        FinancialCapabilityRoutingRequest(
            instrument_identity=_identity(),
            capability=MainlandCapability.NAME_EVENTS,
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
    )

    assert status_calls == ["baostock"]
    assert status_result.selected_provider == "baostock"
    assert status_result.authoritative_provider == "baostock"
    assert status_result.current_tradeability == "suspended"
    assert status_result.source_facts_created is False
    assert name_calls == ["tushare"]
    assert name_result.selected_provider == "tushare"
    assert name_result.current_tradeability == "unknown"
    assert name_result.status_or_lifecycle_fact_created is False
    assert name_result.source_facts_created is False


def test_factor_status_name_lifecycle_and_daily_authority_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
    )

    daily = router.capability_policy(
        MainlandCapability.DAILY_MARKET_SNAPSHOT
    )
    assert daily.providers == ("akshare", "baostock", "yfinance")
    assert "tushare" not in daily.providers

    factors = router.capability_policy(MainlandCapability.ADJUSTMENT_FACTORS)
    assert factors.providers == (
        "baostock",
        "tushare",
        "akshare",
        "yfinance_derived",
    )
    assert factors.strict_bundle_provider == "baostock"
    assert factors.authoritative_provider == "baostock"
    assert factors.non_strict_providers == (
        "tushare",
        "akshare",
        "yfinance_derived",
    )
    assert factors.degraded_providers == ("yfinance_derived",)

    status = router.capability_policy(MainlandCapability.SUSPENSION_STATUS)
    assert status.providers == ("baostock",)
    assert status.authoritative_provider == "baostock"
    assert status.supplemental_providers == ("tushare",)

    names = router.capability_policy(MainlandCapability.NAME_EVENTS)
    assert names.providers == ("tushare", "akshare")
    assert names.authoritative_provider is None

    lifecycle = router.capability_policy(MainlandCapability.ISSUER_LIFECYCLE)
    assert lifecycle.providers == ("baostock",)
    assert lifecycle.authoritative_provider == "baostock"


def test_unknown_company_type_and_missing_announcement_reject_only_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown_period = _REPORTING_PERIODS[-2]
    missing_ann_period = _REPORTING_PERIODS[-1]
    candidates = (
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=unknown_period,
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=FinancialCompanyType.UNKNOWN,
        ),
        _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=missing_ann_period,
            frequency=FinancialReportingFrequency.QUARTERLY,
            ann_date=None,
        ),
    )
    request = _statement_request().model_copy(
        update={
            "eligible_annual_period_ends": (),
            "eligible_reporting_period_ends": (
                unknown_period,
                missing_ann_period,
            ),
        }
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={
            "tushare": lambda _request: _available_response(
                "tushare",
                candidates,
            ),
        },
    ).route_statement(request)

    assert result.selected_candidates == ()
    rejected_reasons = {
        reason
        for rejection in result.manifest.rejected_periods
        for reason in rejection.reasons
    }
    assert FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE in rejected_reasons
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA in rejected_reasons


def test_dispatcher_singleflight_and_checkpoint_resume_reuse_selection_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    identity = asset.instrument_identity
    period = date(2026, 6, 30)
    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=identity,
    )
    selection_request = FinancialStatementRoutingRequest(
        instrument_identity=identity,
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
        tool_call_id="qualified-selection-1",
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

    def source(
        _request: FinancialProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        nonlocal calls
        calls += 1
        return _available_response("tushare", (candidate,))

    plan = _qualified_plan(monkeypatch)
    original = FinancialToolDispatcher(
        instrument_identity=identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": source},
        ),
    )
    first = original.dispatch_statement_selection(tool_request, selection_request)
    duplicate = original.dispatch_statement_selection(
        tool_request.model_copy(
            update={"tool_call_id": "qualified-selection-duplicate"}
        ),
        selection_request,
    )
    checkpoint = original.checkpoint_ledger()

    def unexpected_io(
        _request: FinancialProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        raise AssertionError("checkpoint resume repeated provider I/O")

    restored = FinancialToolDispatcher(
        instrument_identity=identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": unexpected_io},
        ),
        checkpoint_ledger=checkpoint,
    )
    resumed = restored.dispatch_statement_selection(
        tool_request.model_copy(
            update={"tool_call_id": "qualified-selection-resumed"}
        ),
        selection_request,
    )

    assert calls == 1
    assert first.disposition == "executed"
    assert duplicate.disposition == "duplicate_suppressed"
    assert resumed.disposition == "duplicate_suppressed"
    assert resumed.routing_result == first.routing_result
    assert resumed.rendered_value == first.rendered_value
    assert "qualified_statement_selections" in checkpoint
    assert "deterministic-fixture-only" not in str(checkpoint)
    assert "payload" not in resumed.rendered_value


def test_compiled_tool_node_uses_only_final_selected_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    identity = asset.instrument_identity
    period = date(2026, 6, 30)
    candidate = _candidate(
        provider_id="tushare",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        period_end=period,
        frequency=FinancialReportingFrequency.QUARTERLY,
        instrument_identity=identity,
    )
    selection_request = FinancialStatementRoutingRequest(
        instrument_identity=identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=(),
        eligible_reporting_period_ends=(period,),
    )
    calls = 0

    def source(
        _request: FinancialProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        nonlocal calls
        calls += 1
        return _available_response("tushare", (candidate,))

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
    qualified_router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={"tushare": source},
    )
    with pytest.raises(
        ValueError,
        match="requires explicit qualified_v1 mode",
    ):
        FinancialDispatchToolNode(
            config=copy.deepcopy(DEFAULT_CONFIG),
            vendor_methods=vendor_methods,
            qualified_statement_router=qualified_router,
            qualified_statement_request_factory=lambda _request: selection_request,
        )
    assert calls == 0

    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(vendor_methods, "legacy-placeholder")
    runtime_config["mainland_capability_routing_mode"] = "qualified_v1"
    node = FinancialDispatchToolNode(
        config=runtime_config,
        vendor_methods=vendor_methods,
        qualified_statement_router=qualified_router,
        qualified_statement_request_factory=lambda _request: selection_request,
    )
    tool_calls = [
        {
            "name": "get_balance_sheet",
            "args": {
                "ticker": identity.symbol,
                "freq": "quarterly",
                "curr_date": "2026-07-30",
            },
            "id": tool_call_id,
            "type": "tool_call",
        }
        for tool_call_id in ("qualified-node-1", "qualified-node-2")
    ]

    result = node(
        {
            "messages": [AIMessage(content="", tool_calls=tool_calls)],
            "evidence_state": EvidenceState(
                instrument_identity=identity
            ).model_dump(mode="json"),
            "asset_configuration": asset.model_dump(mode="json"),
            "trade_date": "2026-07-30",
        }
    )

    messages = tuple(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert calls == 1
    assert [message.tool_call_id for message in messages] == [
        "qualified-node-1",
        "qualified-node-2",
    ]
    assert messages[0].artifact["disposition"] == "executed"
    assert messages[1].artifact["disposition"] == "duplicate_suppressed"
    assert messages[0].content == messages[1].content
    assert '"provider":"tushare"' in messages[0].content
    assert '"payload"' not in messages[0].content
    assert len(result["financial_dispatch_ledger"]["qualified_statement_selections"]) == 1
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.1"


def test_compiled_tool_node_routes_fundamentals_through_indicator_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    identity = asset.instrument_identity
    candidates = tuple(
        _ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=period,
            instrument_identity=identity,
        )
        for period in _REPORTING_PERIODS
    )
    indicator_request = FinancialIndicatorRoutingRequest(
        instrument_identity=identity,
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        ratio_families=(FinancialRatioFamily.PROFIT,),
        eligible_reporting_period_ends=_REPORTING_PERIODS,
    )
    calls: list[str] = []

    def tushare(
        _request: FinancialIndicatorProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        calls.append("tushare")
        return _available_response("tushare", candidates)

    def lower_provider(
        _request: FinancialIndicatorProviderGapRequest,
    ) -> FinancialProviderPeriodResponse:
        raise AssertionError("complete Tushare indicators must stop fallback")

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
            routing_plan=_qualified_plan(monkeypatch),
            statement_sources={},
            indicator_sources={
                "tushare": tushare,
                "akshare": lower_provider,
            },
        ),
        qualified_indicator_request_factory=lambda _request: indicator_request,
    )

    result = node(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_fundamentals",
                            "args": {
                                "ticker": identity.symbol,
                                "curr_date": "2026-07-30",
                            },
                            "id": "qualified-indicators-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "evidence_state": EvidenceState(
                instrument_identity=identity
            ).model_dump(mode="json"),
            "asset_configuration": asset.model_dump(mode="json"),
            "trade_date": "2026-07-30",
        }
    )

    message = result["messages"][0]
    assert calls == ["tushare"]
    assert isinstance(message, ToolMessage)
    assert message.status == "success"
    assert '"complete_families":["profit"]' in message.content
    assert '"payload"' not in message.content
    assert len(
        result["financial_dispatch_ledger"]["qualified_indicator_selections"]
    ) == 1
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.1"


def test_compiled_tool_node_keeps_fundamentals_legacy_without_indicator_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    identity = asset.instrument_identity
    legacy_calls: list[tuple[str, str]] = []

    def legacy_fundamentals(ticker: str, current_date: str) -> str:
        legacy_calls.append((ticker, current_date))
        return (
            f"# Company Fundamentals for {ticker}\n"
            f"# Data retrieved on: {current_date}\n\n"
            "Name: legacy fundamentals"
        )

    unused = lambda *_args, **_kwargs: "must not be called"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"legacy-placeholder": legacy_fundamentals},
        "get_balance_sheet": {"legacy-placeholder": unused},
        "get_cashflow": {"legacy-placeholder": unused},
        "get_income_statement": {"legacy-placeholder": unused},
    }
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["tool_vendors"] = dict.fromkeys(
        vendor_methods,
        "legacy-placeholder",
    )
    runtime_config["mainland_capability_routing_mode"] = "qualified_v1"

    def statement_only_factory(
        _request: FinancialToolRequest,
    ) -> FinancialStatementRoutingRequest:
        raise AssertionError("fundamentals must not use a statement request factory")

    node = FinancialDispatchToolNode(
        config=runtime_config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=_qualified_plan(monkeypatch),
            statement_sources={},
        ),
        qualified_statement_request_factory=statement_only_factory,
    )

    result = node(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_fundamentals",
                            "args": {
                                "ticker": identity.symbol,
                                "curr_date": "2026-07-30",
                            },
                            "id": "legacy-fundamentals-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "evidence_state": EvidenceState(
                instrument_identity=identity
            ).model_dump(mode="json"),
            "asset_configuration": asset.model_dump(mode="json"),
            "trade_date": "2026-07-30",
        }
    )

    message = result["messages"][0]
    assert legacy_calls == [(identity.symbol, "2026-07-30")]
    assert isinstance(message, ToolMessage)
    assert message.status == "success"
    assert message.content.endswith("Name: legacy fundamentals")
    assert "qualified_statement_selections" not in result["financial_dispatch_ledger"]
    assert result["financial_dispatch_ledger"]["contract_version"] == "1.0"


def test_trading_graph_composes_qualified_financial_router(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    plan = _qualified_plan(monkeypatch)
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    capability_calls: list[str] = []

    def name_source(
        request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        capability_calls.append("tushare")
        return FinancialCapabilityProviderResponse.available(
            provider_id="tushare",
            capability=request.capability,
            retained_artifact_identities=("provider-artifact:tushare-name",),
            candidate_count=1,
            subrequest_keys=("subrequest:tushare-name",),
            physical_attempt_ids=("attempt:tushare-name",),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.NAME_EVENTS: {"tushare": name_source},
        },
    )
    statement_factory = lambda _request: _statement_request()  # noqa: E731

    def indicator_factory(
        _request: FinancialToolRequest,
    ) -> FinancialIndicatorRoutingRequest:
        raise AssertionError("composition must not execute during graph construction")

    composition = QualifiedFinancialRoutingComposition(
        router=router,
        statement_request_factory=statement_factory,
        indicator_request_factory=indicator_factory,
    )
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "data_cache_dir": str(tmp_path / "cache"),
            "results_dir": str(tmp_path / "results"),
        }
    )
    llm = MagicMock()
    model_calls = 0

    def model_factory(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        return SimpleNamespace(get_llm=lambda: llm)

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        model_factory,
    )

    graph = TradingAgentsGraph(
        selected_analysts=("fundamentals",),
        config=runtime_config,
        asset_configuration=asset,
        capability_routing_plan=plan,
        qualified_financial_routing=composition,
    )

    node = graph.tool_nodes["fundamentals"]
    assert model_calls == 2
    assert isinstance(node, FinancialDispatchToolNode)
    assert node._qualified_statement_router is router
    assert node._qualified_statement_request_factory is statement_factory
    assert node._qualified_indicator_request_factory is indicator_factory
    routed = graph.route_mainland_financial_capability(
        FinancialCapabilityRoutingRequest(
            instrument_identity=asset.instrument_identity,
            capability=MainlandCapability.NAME_EVENTS,
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
    )
    assert capability_calls == ["tushare"]
    assert routed.selected_provider == "tushare"


def test_trading_graph_rejects_mismatched_qualified_composition_before_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    plan = _qualified_plan(monkeypatch)
    mismatched_plan = _qualified_plan(
        monkeypatch,
        account_scope_label="personal-research-secondary",
    )
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    composition = QualifiedFinancialRoutingComposition(
        router=MainlandFinancialCapabilityRouter(
            routing_plan=mismatched_plan,
            statement_sources={},
        ),
        statement_request_factory=lambda _request: _statement_request(),
    )
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "data_cache_dir": str(tmp_path / "cache"),
            "results_dir": str(tmp_path / "results"),
        }
    )
    model_calls = 0

    def model_factory(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("model construction must not run")

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        model_factory,
    )

    with pytest.raises(ValueError, match="Capability Routing Plan is immutable"):
        TradingAgentsGraph(
            selected_analysts=("fundamentals",),
            config=runtime_config,
            asset_configuration=asset,
            capability_routing_plan=plan,
            qualified_financial_routing=composition,
        )

    assert model_calls == 0


def test_qualified_router_rejects_non_mainland_and_identity_currency_mismatch() -> None:
    common = {
        "statement_type": FinancialStatementType.BALANCE_SHEET,
        "as_of_date": date(2026, 7, 30),
        "company_type": FinancialCompanyType.INDUSTRIAL_NON_BANK,
        "consolidation_scope": FinancialConsolidationScope.CONSOLIDATED,
        "eligible_annual_period_ends": (),
        "eligible_reporting_period_ends": (date(2026, 6, 30),),
    }
    with pytest.raises(ValueError, match="CNY XSHG/XSHE equity"):
        FinancialStatementRoutingRequest(
            instrument_identity=_identity().model_copy(
                update={"venue": "XNAS", "currency": "USD"}
            ),
            currency="USD",
            **common,
        )
    with pytest.raises(ValueError, match="CNY XSHG/XSHE equity"):
        FinancialStatementRoutingRequest(
            instrument_identity=_identity(),
            currency="USD",
            **common,
        )

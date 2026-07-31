from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, Lock

import pandas as pd
import pytest

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.baostock_capabilities import (
    BaoStockStrictBundleState,
    assemble_baostock_history_bundle,
)
from tradingagents.dataflows.baostock_ratios import (
    BaoStockRatioAdapterConfigurationError,
    BaoStockRatioAdapterFailureReason,
    BaoStockRatioFamilyAdapter,
    BaoStockRatioFamilyTransport,
    BaoStockRatioOutcomeKind,
    assess_baostock_ratio_family_results,
    offer_baostock_ratio_to_statement,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCapability,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialConsolidationScope,
    FinancialPeriodDisposition,
    FinancialPeriodRejectionReason,
    FinancialRatioFamily,
    FinancialStatementType,
    resolve_financial_company_type,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)
from tradingagents.market_history.config import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
)
from tradingagents.market_history.coordinator import (
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.store import MarketHistoryStore

_OBSERVED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _identity(
    symbol: str = "600895.SS",
    venue: str = "XSHG",
    provenance_artifact_sha256: str = "a" * 64,
) -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol=symbol,
        venue=venue,
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="registry:mainland:v1",
            retrieved_at="2026-07-29T00:00:00Z",
            artifact_sha256=provenance_artifact_sha256,
        ),
    )


def _company_type_resolution(
    company_type: FinancialCompanyType,
) -> FinancialCompanyTypeResolution:
    hint = {
        FinancialCompanyType.BANK: "bank",
        FinancialCompanyType.INDUSTRIAL_NON_BANK: "non_bank",
    }[company_type]
    return resolve_financial_company_type(
        provider_declared_type=None,
        provider_declaration_qualified=False,
        classifier_metadata={"industry": hint},
        present_fields=(),
    )


def _qualified_plan() -> MainlandCapabilityRoutingPlan:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": [],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
        }
    )
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    preflight = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={},
    )
    assert preflight.plan is not None
    return preflight.plan


def _legacy_plan() -> MainlandCapabilityRoutingPlan:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    preflight = preflight_mainland_capability_routing(
        asset,
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )
    assert preflight.plan is not None
    return preflight.plan


def _history_config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode="personal_research",
    )


def _profit_row() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "sh.600895",
                "pubDate": "2026-04-25",
                "statDate": "2026-03-31",
                "gpMargin": "0.4",
                "npMargin": "0.1",
                "roeAvg": "0.16",
                "netProfit": "123456789",
                "epsTTM": "0.75",
                "MBRevenue": "987654321",
                "totalShare": "1000000000",
                "liqaShare": "800000000",
                "providerExtension": "preserved-verbatim",
            }
        ]
    )


def _bank_profit_row() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "sh.601328",
                "pubDate": "2026-04-25",
                "statDate": "2026-03-31",
                "npMargin": "0.28",
                "roeAvg": "0.12",
                "gpMargin": None,
                "netProfit": "223456789",
                "epsTTM": "0.65",
                "MBRevenue": "1987654321",
                "totalShare": "2000000000",
                "liqaShare": "1800000000",
            }
        ]
    )


class _FakeTransport:
    def __init__(
        self,
        *,
        profit: object | None = None,
        operation: object | None = None,
        growth: object | None = None,
        balance: object | None = None,
        cash_flow: object | None = None,
        dupont: object | None = None,
    ) -> None:
        self.results = {
            "profit": _profit_row() if profit is None else profit,
            "operation": operation,
            "growth": growth,
            "balance": balance,
            "cash_flow": cash_flow,
            "dupont": dupont,
        }
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _result(self, endpoint: str, kwargs: dict[str, object]) -> pd.DataFrame:
        self.calls.append((endpoint, kwargs))
        value = self.results[endpoint]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(**kwargs)
        if isinstance(value, pd.DataFrame):
            return value.copy(deep=True)
        return value  # type: ignore[return-value]

    def query_profit_data(self, **kwargs: object) -> pd.DataFrame:
        return self._result("profit", kwargs)

    def query_operation_data(self, **kwargs: object) -> pd.DataFrame:
        return self._result("operation", kwargs)

    def query_growth_data(self, **kwargs: object) -> pd.DataFrame:
        return self._result("growth", kwargs)

    def query_balance_data(self, **kwargs: object) -> pd.DataFrame:
        return self._result("balance", kwargs)

    def query_cash_flow_data(self, **kwargs: object) -> pd.DataFrame:
        return self._result("cash_flow", kwargs)

    def query_dupont_data(self, **kwargs: object) -> pd.DataFrame:
        return self._result("dupont", kwargs)


@dataclass(frozen=True)
class _Harness:
    adapter: BaoStockRatioFamilyAdapter
    cache: ProviderSubrequestCache
    coordinator: ProviderRequestCoordinator


def _build_harness(
    *,
    tmp_path,
    store: MarketHistoryStore,
    transport: BaoStockRatioFamilyTransport,
    run_scope_id: str,
    checkpoint: dict[str, object] | None = None,
    artifact_store: ProviderSubrequestArtifactStore | None = None,
    coordinator: ProviderRequestCoordinator | None = None,
    normalizer_version: str | None = None,
    data_usage_mode: DataUsageMode = DataUsageMode.PERSONAL_RESEARCH,
) -> _Harness:
    plan = _qualified_plan()
    coordinator = coordinator or ProviderRequestCoordinator(store)
    upstream_id, service_name = upstream_service_identity_for_provider("baostock")
    coordinator.register_upstream_service(upstream_id, service_name)
    artifact_store = artifact_store or ProviderSubrequestArtifactStore(
        tmp_path / "artifacts"
    )
    cache = ProviderSubrequestCache(
        coordinator=coordinator,
        artifact_store=artifact_store,
        run_scope_id=run_scope_id,
        checkpoint=checkpoint,
    )
    adapter_kwargs = (
        {}
        if normalizer_version is None
        else {"normalizer_version": normalizer_version}
    )
    return _Harness(
        adapter=BaoStockRatioFamilyAdapter(
            routing_plan=plan,
            subrequest_cache=cache,
            transport=transport,
            owner_id=run_scope_id,
            now=lambda: _OBSERVED_AT,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            data_usage_mode=data_usage_mode,
            **adapter_kwargs,
        ),
        cache=cache,
        coordinator=coordinator,
    )


@pytest.mark.unit
def test_non_research_data_usage_mode_fails_closed_before_io(tmp_path) -> None:
    transport = _FakeTransport(profit=_profit_row())
    with (
        MarketHistoryStore.open(_history_config(tmp_path)) as store,
        pytest.raises(BaoStockRatioAdapterConfigurationError) as exc_info,
    ):
        _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-production-mode",
            data_usage_mode=DataUsageMode.PRODUCTION,
        )

    assert exc_info.value.reason is (
        BaoStockRatioAdapterFailureReason.DATA_USAGE_MODE_NOT_PERMITTED
    )
    assert transport.calls == []


@pytest.mark.unit
def test_complete_non_bank_profit_request_normalizes_one_exact_period(
    tmp_path,
) -> None:
    transport = _FakeTransport(profit=_profit_row())
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-profit",
        )
        result = harness.adapter.acquire_profit(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    assert transport.calls == [
        ("profit", {"code": "sh.600895", "year": 2026, "quarter": 1})
    ]
    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(result.attempt_events) == 1
    assert result.request.capacity_scope == "ratio_profit"
    assert result.request.requested_range_start == "2026-Q1"
    assert result.request.requested_range_end == "2026-Q1"
    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert result.artifact.dataset.endpoint_id == "query_profit_data"
    assert result.artifact.dataset.dataset_id == "baostock.profit.quarterly.v1"
    assert len(result.candidates) == 1
    assert len(result.period_completeness) == 1

    candidate = result.candidates[0]
    assessment = result.period_completeness[0]
    assert candidate.period_identity.capability is FinancialCapability.RATIO_FAMILY
    assert candidate.period_identity.ratio_family is FinancialRatioFamily.PROFIT
    assert candidate.period_identity.statement_type is None
    assert candidate.period_identity.period_end == date(2026, 3, 31)
    assert candidate.period_identity.company_type is (
        FinancialCompanyType.INDUSTRIAL_NON_BANK
    )
    assert assessment.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert assessment.core_coverage == Decimal("1")

    gross_margin = next(
        field
        for field in candidate.fields
        if field.normalized_field == "gross_margin"
    )
    assert gross_margin.provider_field == "gpMargin"
    assert gross_margin.original_value == "0.4"
    assert gross_margin.original_unit == "RATIO"
    assert gross_margin.normalized_value == "0.4"
    assert gross_margin.normalized_unit == "RATIO"


@pytest.mark.unit
def test_bank_profit_uses_only_the_bank_qualified_declaration(tmp_path) -> None:
    transport = _FakeTransport(profit=_bank_profit_row())
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-bank-profit",
        )
        result = harness.adapter.acquire_profit(
            instrument_identity=_identity("601328.SS"),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    assessment = result.period_completeness[0]
    assert assessment.company_type is FinancialCompanyType.BANK
    assert assessment.core_coverage == Decimal(2) / Decimal(3)
    assert "net_margin" in assessment.declared_core_fields
    assert "return_on_equity" in assessment.declared_core_fields
    assert "gross_margin" in assessment.declared_core_fields
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        assessment.rejection_reasons
    )
    assert {
        field.normalized_field for field in result.candidates[0].fields
    } == set(assessment.present_core_fields)


def _family_row(family: FinancialRatioFamily) -> pd.DataFrame:
    family_fields = {
        FinancialRatioFamily.OPERATION: {
            "AssetTurnRatio": "1.2",
            "CATurnRatio": "1.3",
            "INVTurnRatio": "1.5",
            "INVTurnDays": "65",
            "NRTurnRatio": "1.6",
            "NRTurnDays": "72",
        },
        FinancialRatioFamily.GROWTH: {
            "YOYAsset": "0.08",
            "YOYEquity": "0.06",
            "YOYNI": "0.04",
            "YOYEPSBasic": "0.03",
            "YOYPNI": "0.02",
        },
        FinancialRatioFamily.BALANCE: {
            "cashRatio": "0.8",
            "currentRatio": "1.5",
            "liabilityToAsset": "0.45",
            "assetToEquity": "1.9",
            "quickRatio": "1.2",
            "YOYLiability": "0.07",
        },
        FinancialRatioFamily.CASH_FLOW: {
            "CAToAsset": "0.4",
            "NCAToAsset": "0.6",
            "tangibleAssetToAsset": "0.7",
            "ebitToInterest": "6",
            "CFOToOR": "0.2",
            "CFOToNP": "0.9",
            "CFOToGr": "0.15",
        },
        FinancialRatioFamily.DUPONT: {
            "dupontAssetTurn": "1.2",
            "dupontAssetStoEquity": "1.9",
            "dupontIntburden": "0.9",
            "dupontPnitoni": "0.95",
            "dupontNitogr": "0.10",
            "dupontEbittogr": "0.15",
            "dupontROE": "0.16",
            "dupontTaxBurden": "0.75",
        },
    }[family]
    return pd.DataFrame(
        [
            {
                "code": "sh.600895",
                "pubDate": "2026-04-25",
                "statDate": "2026-03-31",
                **family_fields,
            }
        ]
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method_name", "family", "transport_slot", "endpoint_id"),
    [
        (
            "acquire_operation",
            FinancialRatioFamily.OPERATION,
            "operation",
            "query_operation_data",
        ),
        (
            "acquire_growth",
            FinancialRatioFamily.GROWTH,
            "growth",
            "query_growth_data",
        ),
        (
            "acquire_balance_ratios",
            FinancialRatioFamily.BALANCE,
            "balance",
            "query_balance_data",
        ),
        (
            "acquire_cash_flow_ratios",
            FinancialRatioFamily.CASH_FLOW,
            "cash_flow",
            "query_cash_flow_data",
        ),
        (
            "acquire_dupont",
            FinancialRatioFamily.DUPONT,
            "dupont",
            "query_dupont_data",
        ),
    ],
)
def test_each_explicit_ratio_family_normalizes_through_its_own_adapter_seam(
    tmp_path,
    method_name: str,
    family: FinancialRatioFamily,
    transport_slot: str,
    endpoint_id: str,
) -> None:
    frame = _family_row(family)
    transport = _FakeTransport(**{transport_slot: frame})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id=f"ticket-09-{family.value}",
            artifact_store=artifact_store,
        )
        result = getattr(harness.adapter, method_name)(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    assert transport.calls == [
        (
            transport_slot,
            {"code": "sh.600895", "year": 2026, "quarter": 1},
        )
    ]
    assert result.ratio_family is family
    assert result.request.capacity_scope == f"ratio_{family.value}"
    assert result.artifact is not None
    assert result.artifact.dataset.endpoint_id == endpoint_id
    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert persisted["columns"] == list(frame.columns)
    assert persisted["rows"][0] == frame.iloc[0].to_dict()
    assert len(result.candidates) == 1
    assert result.candidates[0].period_identity.ratio_family is family
    assert result.period_completeness[0].core_coverage == Decimal("1")
    assert result.period_completeness[0].disposition is (
        FinancialPeriodDisposition.CURRENT_ONLY
    )
    provider_fields = {
        field.provider_field for field in result.candidates[0].fields
    }
    if family is FinancialRatioFamily.OPERATION:
        assert provider_fields == {
            "AssetTurnRatio",
            "CATurnRatio",
            "INVTurnRatio",
            "NRTurnRatio",
        }
        assert "NRTurnDays" in result.request.requested_fields
        assert "INVTurnDays" in result.request.requested_fields
        assert "APTurnRatio" not in result.request.requested_fields
        assert "FATurnRatio" not in result.request.requested_fields


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


def _quarter(period_end: date) -> int:
    return {3: 1, 6: 2, 9: 3, 12: 4}[period_end.month]


def _period_result(
    family: FinancialRatioFamily,
    *,
    bank: bool,
    sparse: bool = False,
):
    def result(**kwargs: object) -> pd.DataFrame:
        year = int(kwargs["year"])
        quarter = int(kwargs["quarter"])
        month, day = {
            1: (3, 31),
            2: (6, 30),
            3: (9, 30),
            4: (12, 31),
        }[quarter]
        period_end = date(year, month, day)
        if family is FinancialRatioFamily.PROFIT:
            frame = _bank_profit_row() if bank else _profit_row()
        else:
            frame = _family_row(family)
        frame = frame.copy(deep=True)
        if bank and family is FinancialRatioFamily.PROFIT and not sparse:
            frame.loc[0, "gpMargin"] = "0.35"
        frame.loc[0, "code"] = "sh.601328" if bank else "sh.600895"
        frame.loc[0, "statDate"] = period_end.isoformat()
        frame.loc[0, "pubDate"] = (period_end + timedelta(days=25)).isoformat()
        if sparse:
            removable = [
                column
                for column in frame.columns
                if column not in {"code", "pubDate", "statDate"}
            ]
            frame = frame.drop(columns=removable[:2])
        return frame

    return result


@pytest.mark.unit
def test_bank_sparse_families_are_rejected_without_erasing_complete_siblings(
    tmp_path,
) -> None:
    complete_families = (
        FinancialRatioFamily.PROFIT,
        FinancialRatioFamily.GROWTH,
        FinancialRatioFamily.BALANCE,
        FinancialRatioFamily.DUPONT,
    )
    sparse_families = (
        FinancialRatioFamily.OPERATION,
        FinancialRatioFamily.CASH_FLOW,
    )
    slot_by_family = {
        FinancialRatioFamily.PROFIT: "profit",
        FinancialRatioFamily.OPERATION: "operation",
        FinancialRatioFamily.GROWTH: "growth",
        FinancialRatioFamily.BALANCE: "balance",
        FinancialRatioFamily.CASH_FLOW: "cash_flow",
        FinancialRatioFamily.DUPONT: "dupont",
    }
    method_by_family = {
        FinancialRatioFamily.PROFIT: "acquire_profit",
        FinancialRatioFamily.OPERATION: "acquire_operation",
        FinancialRatioFamily.GROWTH: "acquire_growth",
        FinancialRatioFamily.BALANCE: "acquire_balance_ratios",
        FinancialRatioFamily.CASH_FLOW: "acquire_cash_flow_ratios",
        FinancialRatioFamily.DUPONT: "acquire_dupont",
    }
    transport = _FakeTransport(
        **{
            slot_by_family[family]: _period_result(
                family,
                bank=True,
                sparse=family in sparse_families,
            )
            for family in FinancialRatioFamily
        }
    )
    identity = _identity("601328.SS")
    resolution = _company_type_resolution(FinancialCompanyType.BANK)
    results = []
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-bank-independent-salvage",
        )
        for family in FinancialRatioFamily:
            for period_end in _PERIODS:
                results.append(
                    getattr(harness.adapter, method_by_family[family])(
                        instrument_identity=identity,
                        company_type_resolution=resolution,
                        year=period_end.year,
                        quarter=_quarter(period_end),
                        as_of_date=date(2026, 7, 30),
                    )
                )

    aggregate = assess_baostock_ratio_family_results(
        instrument_identity=identity,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.UNKNOWN,
        currency="CNY",
        results=results,
        eligible_reporting_period_ends=_PERIODS,
    )
    completeness = {
        item.ratio_family: item for item in aggregate.family_completeness
    }
    assert all(completeness[family].complete for family in complete_families)
    assert all(not completeness[family].complete for family in sparse_families)
    assert set(aggregate.qualified_families) == set(complete_families)
    assert set(aggregate.rejected_families) == set(sparse_families)
    assert {
        item.period_identity.ratio_family
        for item in aggregate.usable_candidates
    } == set(complete_families)
    assert len(aggregate.usable_candidates) == 8 * len(complete_families)
    assert len(aggregate.operational_results) == 8 * len(FinancialRatioFamily)
    assert all(
        result.provider_artifact is not None
        for result in aggregate.operational_results
    )
    assert all(
        assessment.core_coverage < Decimal("0.9")
        for result in aggregate.operational_results
        if result.ratio_family in sparse_families
        for assessment in result.period_completeness
    )
    assert all(
        result.capability_outcome.kind
        is BaoStockRatioOutcomeKind.INSUFFICIENT_COMPLETENESS
        for result in aggregate.operational_results
        if result.ratio_family in sparse_families
    )

    missing_one_profit = assess_baostock_ratio_family_results(
        instrument_identity=identity,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.UNKNOWN,
        currency="CNY",
        results=[
            result
            for result in results
            if not (
                result.ratio_family is FinancialRatioFamily.PROFIT
                and result.year == 2024
                and result.quarter == 2
            )
        ],
        eligible_reporting_period_ends=_PERIODS,
    )
    profit = next(
        item
        for item in missing_one_profit.family_completeness
        if item.ratio_family is FinancialRatioFamily.PROFIT
    )
    assert not profit.complete
    assert profit.missing_reporting_period_ends == (date(2024, 6, 30),)


@pytest.mark.unit
def test_mature_non_bank_family_qualifies_across_eight_reporting_periods(
    tmp_path,
) -> None:
    identity = _identity()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(
                growth=_period_result(
                    FinancialRatioFamily.GROWTH,
                    bank=False,
                )
            ),
            run_scope_id="ticket-09-non-bank-eight-periods",
        )
        results = tuple(
            harness.adapter.acquire_growth(
                instrument_identity=identity,
                company_type_resolution=_company_type_resolution(
                    FinancialCompanyType.INDUSTRIAL_NON_BANK
                ),
                year=period_end.year,
                quarter=_quarter(period_end),
                as_of_date=date(2026, 7, 30),
            )
            for period_end in _PERIODS
        )

    aggregate = assess_baostock_ratio_family_results(
        instrument_identity=identity,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.UNKNOWN,
        currency="CNY",
        results=results,
        eligible_reporting_period_ends=_PERIODS,
    )
    assert aggregate.qualified_families == (FinancialRatioFamily.GROWTH,)
    assert aggregate.rejected_families == ()
    assert len(aggregate.usable_candidates) == 8


@pytest.mark.unit
def test_exact_request_artifact_is_immutable_and_checkpoint_resume_is_zero_io(
    tmp_path,
) -> None:
    transport = _FakeTransport(profit=_profit_row())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-checkpoint",
            artifact_store=artifact_store,
        )
        kwargs = {
            "instrument_identity": _identity(),
            "company_type_resolution": _company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            "year": 2026,
            "quarter": 1,
            "as_of_date": date(2026, 7, 30),
        }
        first = harness.adapter.acquire_profit(**kwargs)
        completed = harness.adapter.acquire_profit(**kwargs)
        checkpoint = harness.cache.checkpoint()

        resumed_transport = _FakeTransport(
            profit=AssertionError("checkpoint restore performed provider I/O")
        )
        resumed_harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=resumed_transport,
            run_scope_id="ticket-09-checkpoint",
            artifact_store=artifact_store,
            checkpoint=checkpoint,
        )
        resumed = resumed_harness.adapter.acquire_profit(**kwargs)

    assert first == completed == resumed
    assert len(transport.calls) == 1
    assert resumed_transport.calls == []
    assert len(first.attempt_events) == 1
    assert len(checkpoint["entries"]) == 1
    assert first.provider_artifact is not None
    persisted = json.loads(artifact_store.read(first.provider_artifact))
    assert persisted["ratio_family"] == "profit"
    assert persisted["year"] == 2026
    assert persisted["quarter"] == 1
    assert persisted["canonical_symbol"] == "600895.SS"
    assert persisted["instrument_identity"] == (
        first.request.instrument_identity.model_dump(mode="json")
    )
    assert persisted["retrieved_at"] == "2026-07-30T12:00:00Z"
    assert persisted["observed_at"] == "2026-07-30T12:00:00Z"
    assert persisted["qualification_profile"] == "cn-a-2000-20260729-v1"
    assert persisted["normalizer_version"] == (
        "baostock-ratio-family-normalizer-v1"
    )
    assert persisted["exact_request_identity"] == first.request.subrequest_key
    assert persisted["columns"] == list(_profit_row().columns)
    assert persisted["rows"][0]["providerExtension"] == "preserved-verbatim"
    assert persisted["rows"][0]["gpMargin"] == "0.4"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("identity_field", "contradictory_value"),
    [
        ("dataset_id", "baostock.growth.quarterly.v1"),
        ("schema_identity", "baostock_growth_quarterly_v1"),
    ],
)
def test_checkpoint_artifact_with_contradictory_family_identity_fails_closed(
    tmp_path,
    identity_field: str,
    contradictory_value: str,
) -> None:
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    kwargs = {
        "instrument_identity": _identity(),
        "company_type_resolution": _company_type_resolution(
            FinancialCompanyType.INDUSTRIAL_NON_BANK
        ),
        "year": 2026,
        "quarter": 1,
        "as_of_date": date(2026, 7, 30),
    }
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        first_harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(profit=_profit_row()),
            run_scope_id=f"ticket-09-identity-{identity_field}",
            artifact_store=artifact_store,
        )
        first = first_harness.adapter.acquire_profit(**kwargs)
        assert first.provider_artifact is not None
        checkpoint = first_harness.cache.checkpoint()
        forged_payload = json.loads(artifact_store.read(first.provider_artifact))
        forged_payload[identity_field] = contradictory_value
        forged_artifact = artifact_store.install(
            json.dumps(
                forged_payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
            media_type="application/json",
        )
        checkpoint["entries"][0]["artifact"] = forged_artifact.model_dump(mode="json")

        resumed_transport = _FakeTransport(
            profit=AssertionError("contradictory checkpoint performed provider I/O")
        )
        resumed_harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=resumed_transport,
            run_scope_id=f"ticket-09-identity-{identity_field}",
            artifact_store=artifact_store,
            checkpoint=checkpoint,
        )
        with pytest.raises(ValueError, match="BaoStock ratio artifact is malformed"):
            resumed_harness.adapter.acquire_profit(**kwargs)

    assert resumed_transport.calls == []


class _BlockingProfitTransport(_FakeTransport):
    def __init__(self) -> None:
        super().__init__(profit=_profit_row())
        self.started = Event()
        self.release = Event()
        self._lock = Lock()

    def query_profit_data(self, **kwargs: object) -> pd.DataFrame:
        with self._lock:
            self.calls.append(("profit", dict(kwargs)))
        self.started.set()
        assert self.release.wait(timeout=5)
        return _profit_row()


@pytest.mark.unit
def test_identical_ratio_requests_single_flight_to_one_call_and_attempt(
    tmp_path,
) -> None:
    transport = _BlockingProfitTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-single-flight",
        )
        kwargs = {
            "instrument_identity": _identity(),
            "company_type_resolution": _company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            "year": 2026,
            "quarter": 1,
            "as_of_date": date(2026, 7, 30),
        }

        def follower():
            assert transport.started.wait(timeout=5)
            return harness.adapter.acquire_profit(**kwargs)

        def release_leader() -> None:
            assert transport.started.wait(timeout=5)
            transport.release.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            second_future = executor.submit(follower)
            releaser = executor.submit(release_leader)
            first = harness.adapter.acquire_profit(**kwargs)
            second = second_future.result(timeout=5)
            releaser.result(timeout=5)

    assert first == second
    assert len(transport.calls) == 1
    assert len(first.attempt_events) == 1
    assert first.attempt_events[0].request_key == first.request.subrequest_key


@pytest.mark.unit
def test_family_year_quarter_as_of_and_normalizer_have_distinct_cache_keys(
    tmp_path,
) -> None:
    transport = _FakeTransport(
        profit=_period_result(FinancialRatioFamily.PROFIT, bank=False),
        growth=_period_result(FinancialRatioFamily.GROWTH, bank=False),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-distinct-keys",
        )
        common = {
            "instrument_identity": _identity(),
            "company_type_resolution": _company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
        }
        requests = (
            harness.adapter.acquire_profit(
                **common,
                year=2026,
                quarter=1,
                as_of_date=date(2026, 7, 30),
            ),
            harness.adapter.acquire_profit(
                **common,
                year=2025,
                quarter=4,
                as_of_date=date(2026, 7, 30),
            ),
            harness.adapter.acquire_growth(
                **common,
                year=2026,
                quarter=1,
                as_of_date=date(2026, 7, 30),
            ),
            harness.adapter.acquire_profit(
                **common,
                year=2026,
                quarter=1,
                as_of_date=date(2026, 7, 31),
            ),
        )
        different_normalizer = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-distinct-keys",
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            coordinator=harness.coordinator,
            checkpoint=harness.cache.checkpoint(),
            normalizer_version="baostock-ratio-family-normalizer-v2",
        ).adapter.acquire_profit(
            **common,
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    keys = {result.request.subrequest_key for result in (*requests, different_normalizer)}
    assert len(keys) == 5
    assert len(transport.calls) == 5
    assert len(
        {
            result.provider_artifact.artifact_ref
            for result in (*requests, different_normalizer)
            if result.provider_artifact is not None
        }
    ) == 5


@pytest.mark.unit
def test_symbol_and_authoritative_instrument_revision_have_distinct_cache_keys(
    tmp_path,
) -> None:
    def response(**kwargs: object) -> pd.DataFrame:
        frame = _profit_row()
        frame.loc[0, "code"] = kwargs["code"]
        return frame

    transport = _FakeTransport(profit=response)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-identity-keys",
        )
        identities = (
            _identity(),
            _identity(provenance_artifact_sha256="b" * 64),
            _identity("000001.SZ", "XSHE"),
        )
        results = tuple(
            harness.adapter.acquire_profit(
                instrument_identity=identity,
                company_type_resolution=_company_type_resolution(
                    FinancialCompanyType.INDUSTRIAL_NON_BANK
                ),
                year=2026,
                quarter=1,
                as_of_date=date(2026, 7, 30),
            )
            for identity in identities
        )

    assert len({result.request.subrequest_key for result in results}) == 3
    assert len(
        {result.request.instrument_identity.identity_revision for result in results}
    ) == 3
    assert len(transport.calls) == 3


class _RateLimitError(RuntimeError):
    retry_after_seconds = 17


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider_result", "transport_kind", "capability_kind"),
    [
        (
            RuntimeError(
                "permission denied token=do-not-persist https://provider.invalid"
            ),
            ProviderSubrequestOutcomeKind.PERMISSION_DENIED,
            BaoStockRatioOutcomeKind.PERMISSION_DENIED,
        ),
        (
            RuntimeError("authentication login failed token=do-not-persist"),
            ProviderSubrequestOutcomeKind.AUTHENTICATION,
            BaoStockRatioOutcomeKind.AUTHENTICATION_FAILURE,
        ),
        (
            _RateLimitError("rate limit token=do-not-persist"),
            ProviderSubrequestOutcomeKind.RATE_LIMITED,
            BaoStockRatioOutcomeKind.RATE_LIMITED,
        ),
        (
            TimeoutError("timeout token=do-not-persist"),
            ProviderSubrequestOutcomeKind.TIMEOUT,
            BaoStockRatioOutcomeKind.TIMEOUT,
        ),
        (
            ConnectionError("socket disconnect token=do-not-persist"),
            ProviderSubrequestOutcomeKind.DISCONNECT,
            BaoStockRatioOutcomeKind.DISCONNECT,
        ),
        (
            RuntimeError("unexpected token=do-not-persist https://provider.invalid"),
            ProviderSubrequestOutcomeKind.PROVIDER_ERROR,
            BaoStockRatioOutcomeKind.PROVIDER_ERROR,
        ),
    ],
)
def test_transport_failures_are_typed_and_sanitized(
    tmp_path,
    provider_result: Exception,
    transport_kind: ProviderSubrequestOutcomeKind,
    capability_kind: BaoStockRatioOutcomeKind,
) -> None:
    transport = _FakeTransport(profit=provider_result)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id=f"ticket-09-failure-{transport_kind.value}",
        )
        result = harness.adapter.acquire_profit(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )
        checkpoint = harness.cache.checkpoint()

    assert result.outcome.kind is transport_kind
    assert result.capability_outcome.kind is capability_kind
    assert len(result.attempt_events) == 1
    assert result.provider_artifact is None
    rendered = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "checkpoint": checkpoint,
        },
        sort_keys=True,
    )
    assert "do-not-persist" not in rendered
    assert "provider.invalid" not in rendered
    assert "token=" not in rendered
    assert str(provider_result) not in rendered
    persisted = b"".join(
        path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    )
    assert b"do-not-persist" not in persisted
    assert b"provider.invalid" not in persisted
    assert b"token=" not in persisted


@pytest.mark.unit
def test_empty_malformed_and_incompatible_metadata_outcomes_are_typed(
    tmp_path,
) -> None:
    empty = pd.DataFrame(columns=_profit_row().columns)
    malformed = object()
    wrong_period = _profit_row().assign(statDate="2025-12-31")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        empty_harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(profit=empty),
            run_scope_id="ticket-09-empty",
        )
        empty_result = empty_harness.adapter.acquire_profit(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )
        malformed_harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(profit=malformed),
            run_scope_id="ticket-09-malformed",
        )
        malformed_result = malformed_harness.adapter.acquire_profit(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )
        metadata_harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(profit=wrong_period),
            run_scope_id="ticket-09-incompatible-metadata",
        )
        metadata_result = metadata_harness.adapter.acquire_profit(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    assert empty_result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert empty_result.capability_outcome.kind is (
        BaoStockRatioOutcomeKind.EMPTY_NO_DATA
    )
    assert empty_result.provider_artifact is not None
    assert empty_result.artifact is not None
    assert not empty_result.row_artifacts
    assert not empty_result.candidates

    assert malformed_result.outcome.kind is ProviderSubrequestOutcomeKind.MALFORMED
    assert malformed_result.capability_outcome.kind is (
        BaoStockRatioOutcomeKind.MALFORMED_RESPONSE
    )
    assert malformed_result.provider_artifact is None

    assert metadata_result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert metadata_result.capability_outcome.kind is (
        BaoStockRatioOutcomeKind.INCOMPATIBLE_METADATA
    )
    assert metadata_result.provider_artifact is not None
    assert metadata_result.artifact is not None
    assert len(metadata_result.row_artifacts) == 1
    assert not metadata_result.candidates
    assert len(metadata_result.row_rejections) == 1


@pytest.mark.unit
def test_incompatible_unit_currency_and_ambiguous_company_type_are_independent(
    tmp_path,
) -> None:
    incompatible_unit = _profit_row().assign(gpMarginUnit="BPS")
    incompatible_currency = _profit_row().assign(currency="USD")
    ambiguous_resolution = resolve_financial_company_type(
        provider_declared_type=None,
        provider_declaration_qualified=False,
        classifier_metadata={},
        present_fields=(),
    )
    cases = (
        (
            incompatible_unit,
            _company_type_resolution(FinancialCompanyType.INDUSTRIAL_NON_BANK),
            BaoStockRatioOutcomeKind.INCOMPATIBLE_UNIT,
        ),
        (
            incompatible_currency,
            _company_type_resolution(FinancialCompanyType.INDUSTRIAL_NON_BANK),
            BaoStockRatioOutcomeKind.INCOMPATIBLE_CURRENCY,
        ),
        (
            _profit_row(),
            ambiguous_resolution,
            BaoStockRatioOutcomeKind.AMBIGUOUS_COMPANY_TYPE,
        ),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        results = []
        for index, (frame, resolution, _expected) in enumerate(cases):
            harness = _build_harness(
                tmp_path=tmp_path,
                store=store,
                transport=_FakeTransport(profit=frame),
                run_scope_id=f"ticket-09-incompatible-{index}",
            )
            results.append(
                harness.adapter.acquire_profit(
                    instrument_identity=_identity(),
                    company_type_resolution=resolution,
                    year=2026,
                    quarter=1,
                    as_of_date=date(2026, 7, 30),
                )
            )

    for result, (_frame, _resolution, expected) in zip(results, cases, strict=True):
        assert result.capability_outcome.kind is expected
        assert result.provider_artifact is not None
        assert result.artifact is not None
        assert not any(
            assessment.usable_for_current_analysis
            for assessment in result.period_completeness
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "statement_type",
    [
        FinancialStatementType.BALANCE_SHEET,
        FinancialStatementType.INCOME_STATEMENT,
        FinancialStatementType.CASH_FLOW,
    ],
)
def test_ratio_artifact_offered_as_statement_returns_capability_mismatch(
    tmp_path,
    statement_type: FinancialStatementType,
) -> None:
    identity = _identity()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(profit=_profit_row()),
            run_scope_id=f"ticket-09-mismatch-{statement_type.value}",
        )
        ratio_result = harness.adapter.acquire_profit(
            instrument_identity=identity,
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    boundary = offer_baostock_ratio_to_statement(
        result=ratio_result,
        statement_type=statement_type,
        expected_instrument_identity=identity,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert boundary.capability_outcome.kind is (
        BaoStockRatioOutcomeKind.CAPABILITY_MISMATCH
    )
    assert boundary.retained_artifact_identity == (
        ratio_result.artifact.artifact_identity
    )
    assert not boundary.statement_candidates
    assert boundary.candidate_assessments
    assert all(
        FinancialPeriodRejectionReason.CAPABILITY_MISMATCH
        in assessment.rejection_reasons
        for assessment in boundary.candidate_assessments
    )
    assert not boundary.source_facts_created
    assert not boundary.decision_ready_evidence_created
    assert not boundary.decision_gate_changed


@pytest.mark.unit
@pytest.mark.parametrize(
    "statement_type",
    [
        FinancialStatementType.BALANCE_SHEET,
        FinancialStatementType.INCOME_STATEMENT,
        FinancialStatementType.CASH_FLOW,
    ],
)
def test_empty_ratio_artifact_offered_as_statement_returns_capability_mismatch(
    tmp_path,
    statement_type: FinancialStatementType,
) -> None:
    identity = _identity()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(
                profit=pd.DataFrame(columns=_profit_row().columns)
            ),
            run_scope_id=f"ticket-09-empty-mismatch-{statement_type.value}",
        )
        ratio_result = harness.adapter.acquire_profit(
            instrument_identity=identity,
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    boundary = offer_baostock_ratio_to_statement(
        result=ratio_result,
        statement_type=statement_type,
        expected_instrument_identity=identity,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert boundary.capability_outcome.kind is (
        BaoStockRatioOutcomeKind.CAPABILITY_MISMATCH
    )
    assert boundary.retained_artifact_identity == (
        ratio_result.artifact.artifact_identity
    )
    assert boundary.candidate_assessments == ()
    assert boundary.statement_candidates == ()


class _BaoStockLoginCapacityError(RuntimeError):
    error_code = "10001005"
    retry_after_seconds = 19


@pytest.mark.unit
def test_baostock_login_capacity_failure_applies_upstream_global_cooldown(
    tmp_path,
) -> None:
    transport = _FakeTransport(
        profit=_BaoStockLoginCapacityError("login capacity unavailable"),
        growth=_family_row(FinancialRatioFamily.GROWTH),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=transport,
            run_scope_id="ticket-09-global-cooldown",
        )
        profit = harness.adapter.acquire_profit(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )
        blocked_growth = harness.adapter.acquire_growth(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    assert profit.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
    assert blocked_growth.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
    assert blocked_growth.attempt_events == ()
    assert transport.calls == [
        ("profit", {"code": "sh.600895", "year": 2026, "quarter": 1})
    ]


@pytest.mark.unit
def test_legacy_mode_never_activates_ratios_and_daily_order_is_unchanged(
    tmp_path,
) -> None:
    plan = _legacy_plan()
    transport = _FakeTransport(profit=_profit_row())
    assert plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT) == (
        "akshare",
        "baostock",
        "yfinance",
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_id, service_name = upstream_service_identity_for_provider("baostock")
        coordinator.register_upstream_service(upstream_id, service_name)
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="ticket-09-legacy",
        )
        with pytest.raises(BaoStockRatioAdapterConfigurationError) as exc_info:
            BaoStockRatioFamilyAdapter(
                routing_plan=plan,
                subrequest_cache=cache,
                transport=transport,
                owner_id="ticket-09-legacy",
                now=lambda: _OBSERVED_AT,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
            )

    assert exc_info.value.reason is BaoStockRatioAdapterFailureReason.NOT_ENABLED
    assert transport.calls == []
    assert cache.checkpoint()["entries"] == []


@pytest.mark.unit
def test_ratio_result_cannot_enter_strict_history_or_change_status_evidence(
    tmp_path,
) -> None:
    identity = _identity()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            tmp_path=tmp_path,
            store=store,
            transport=_FakeTransport(profit=_profit_row()),
            run_scope_id="ticket-09-strict-separation",
        )
        result = harness.adapter.acquire_profit(
            instrument_identity=identity,
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            year=2026,
            quarter=1,
            as_of_date=date(2026, 7, 30),
        )

    bundle = assemble_baostock_history_bundle(
        instrument_identity=identity,
        as_of_date=date(2026, 7, 30),
        raw=result,  # type: ignore[arg-type]
        factors=None,
        statuses=None,
    )
    assert bundle.strict_bundle_state is BaoStockStrictBundleState.INCOMPLETE
    assert bundle.provider_history_bundle_identity is None
    assert bundle.publication is None
    assert not result.strict_history_bundle_component
    assert not result.current_tradeability_changed
    assert not result.adjustment_basis_changed
    assert not result.lifecycle_evidence_changed
    assert not result.source_facts_created
    assert not result.decision_ready_evidence_created
    assert not result.decision_gate_changed

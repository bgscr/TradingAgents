from __future__ import annotations

import copy
import importlib.util
import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from threading import Event, Lock

import pandas as pd
import pytest

import tradingagents.dataflows.provider_subrequests as provider_subrequests_module
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_V2,
    FinancialCapability,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialConsolidationScope,
    FinancialPeriodDisposition,
    FinancialPeriodRejectionReason,
    FinancialProviderArtifactIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    assess_financial_period_candidate,
    financial_ratio_field_declaration,
    resolve_financial_company_type,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.dataflows.tushare_indicators import (
    TushareFinancialIndicatorAdapter,
    TushareFinancialIndicatorAdapterConfigurationError,
    TushareFinancialIndicatorAdapterFailureReason,
    TushareFinancialIndicatorAdapterResult,
    TushareFinancialIndicatorSdkClient,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)
from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
from tradingagents.market_history.coordinator import (
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.store import MarketHistoryStore

_OBSERVED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
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


def _identity(symbol: str = "600895.SS") -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol=symbol,
        venue="XSHG" if symbol.endswith(".SS") else "XSHE",
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="registry:mainland:v1",
            retrieved_at="2026-07-29T00:00:00Z",
            artifact_sha256="a" * 64,
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


def _qualified_plan(monkeypatch: pytest.MonkeyPatch) -> MainlandCapabilityRoutingPlan:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": ["financial_indicators"],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
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


def _history_config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode="personal_research",
    )


class _FakeTushareClient:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def fina_indicator(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        if isinstance(self.result, pd.DataFrame):
            return self.result.copy(deep=True)
        return self.result  # type: ignore[return-value]


class _RateLimitError(RuntimeError):
    retry_after_seconds = 30


class _BlockingTushareClient(_FakeTushareClient):
    def __init__(self, frame: pd.DataFrame) -> None:
        super().__init__(frame)
        self.started = Event()
        self.release = Event()
        self._calls_lock = Lock()

    def fina_indicator(self, **kwargs: object) -> pd.DataFrame:
        with self._calls_lock:
            self.calls.append(kwargs)
        self.started.set()
        assert self.release.wait(timeout=5)
        assert isinstance(self.result, pd.DataFrame)
        return self.result.copy(deep=True)


def _complete_non_bank_rows() -> pd.DataFrame:
    rows = []
    for index, period_end in enumerate(_PERIODS):
        rows.append(
            {
                "ts_code": "600895.SH",
                "ann_date": (period_end + timedelta(days=25)).strftime("%Y%m%d"),
                "end_date": period_end.strftime("%Y%m%d"),
                "eps": 1.2 + index / 100,
                "ebitda_margin": 12 + index,
                "grossprofit_margin": 40 + index,
                "netprofit_margin": 10 + index,
                "op_of_gr": 15 + index,
                "profit_to_gr": 13 + index,
                "profit_to_op": 80 + index,
                "roa": 8 + index,
                "roe": 16 + index,
                "roic": 11 + index,
                "cash_ratio": 0.8,
                "current_ratio": 1.5,
                "debt_to_assets": 45 + index,
                "debt_to_eqt": 0.9,
                "assets_to_eqt": 1.9,
                "ebit_to_interest": 6.0,
                "longdeb_to_debt": 30 + index,
                "quick_ratio": 1.2,
                "tbassets_to_totalassets": 70 + index,
                "working_capital_ratio": 0.2,
                "provider_extension": f"preserve-{index}",
            }
        )
    return pd.DataFrame(rows)


def _complete_bank_rows() -> pd.DataFrame:
    rows = []
    for index, period_end in enumerate(_PERIODS):
        rows.append(
            {
                "ts_code": "601328.SH",
                "ann_date": (period_end + timedelta(days=25)).strftime("%Y%m%d"),
                "end_date": period_end.strftime("%Y%m%d"),
                "netprofit_margin": 28 + index,
                "profit_to_gr": 31 + index,
                "roa": 1.1 + index / 100,
                "roe": 12 + index,
                "roic": 10 + index,
                "cost_income_ratio": 30 + index,
                "net_interest_margin": 2.1 + index / 100,
                "net_interest_spread": 1.9 + index / 100,
                "npl_ratio": 1.2 + index / 100,
                "provision_coverage_ratio": 180 + index,
                "assets_yoy": 8 + index,
                "ocf_yoy": 7 + index,
                "eqt_yoy": 6 + index,
                "grossprofit_yoy": 5 + index,
                "netprofit_yoy": 4 + index,
                "op_yoy": 3 + index,
                "tr_yoy": 2 + index,
                "roe_yoy": 1 + index,
                "ebt_yoy": 9 + index,
                "working_capital_yoy": 10 + index,
                "industrial_only_ebitda_margin": None,
            }
        )
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class _Harness:
    adapter: TushareFinancialIndicatorAdapter
    cache: ProviderSubrequestCache
    coordinator: ProviderRequestCoordinator


def _build_harness(
    *,
    plan: MainlandCapabilityRoutingPlan,
    client: TushareFinancialIndicatorSdkClient,
    store: MarketHistoryStore,
    artifact_store: ProviderSubrequestArtifactStore,
    run_scope_id: str,
    owner_id: str | None = None,
    observed_at: datetime = _OBSERVED_AT,
    checkpoint: dict[str, object] | None = None,
    coordinator: ProviderRequestCoordinator | None = None,
) -> _Harness:
    coordinator = coordinator or ProviderRequestCoordinator(store)
    upstream_id, service_name = upstream_service_identity_for_provider(
        "tushare",
        account_scope=plan.account_scope_label,
    )
    coordinator.register_upstream_service(
        upstream_id,
        service_name,
        account_scope=plan.account_scope_label,
    )
    cache = ProviderSubrequestCache(
        coordinator=coordinator,
        artifact_store=artifact_store,
        run_scope_id=run_scope_id,
        checkpoint=checkpoint,
    )
    return _Harness(
        adapter=TushareFinancialIndicatorAdapter(
            routing_plan=plan,
            subrequest_cache=cache,
            sdk_client=client,
            owner_id=owner_id or run_scope_id,
            now=lambda: observed_at,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ),
        cache=cache,
        coordinator=coordinator,
    )


@pytest.mark.unit
def test_complete_non_bank_history_persists_raw_artifact_and_passes_eight_periods(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(_complete_non_bank_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-complete-non-bank",
        )
        result = harness.adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=_PERIODS,
        )

    assert client.calls == [
        {
            "ts_code": "600895.SH",
            "start_date": "20210101",
            "end_date": "20260730",
        }
    ]
    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(result.attempt_events) == 1
    assert result.attempt_events[0].capacity_scope == "fina_indicator"
    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert persisted["columns"] == list(_complete_non_bank_rows().columns)
    assert persisted["adapter_metadata"] == {
        "capability": "financial_ratio_family",
        "qualified_metadata_absences": [
            "comp_type",
            "f_ann_date",
            "report_scope",
            "stable_restatement_identity",
        ],
    }
    assert persisted["rows"][0]["provider_extension"] == "preserve-0"
    assert persisted["rows"][0]["eps"] == 1.2
    assert persisted["rows"][0]["ann_date"] == "20240725"
    assert result.artifact is not None
    assert result.artifact.dataset.endpoint_id == "fina_indicator"
    assert len(result.row_artifacts) == 8

    assert result.candidates
    assert all(
        candidate.period_identity.capability is FinancialCapability.RATIO_FAMILY
        and candidate.period_identity.statement_type is None
        and candidate.period_identity.consolidation_scope
        is FinancialConsolidationScope.UNKNOWN
        for candidate in result.candidates
    )
    first_profit = next(
        candidate
        for candidate in result.candidates
        if candidate.period_identity.ratio_family is FinancialRatioFamily.PROFIT
        and candidate.period_identity.period_end == _PERIODS[0]
    )
    gross_margin = next(
        field
        for field in first_profit.fields
        if field.normalized_field == "gross_margin"
    )
    assert all(
        field.normalized_field != "earnings_per_share"
        for candidate in result.candidates
        for field in candidate.fields
    )
    assert gross_margin.original_value == "40"
    assert gross_margin.original_unit == "PERCENT"
    assert gross_margin.normalized_value == "0.4"
    assert gross_margin.normalized_unit == "RATIO"
    assert first_profit.period_identity.currency == "XXX"
    assert first_profit.filing_metadata.retrieved_at == _OBSERVED_AT
    assert first_profit.filing_metadata.observed_at == _OBSERVED_AT
    profit_history = next(
        item
        for item in result.family_completeness
        if item.ratio_family is FinancialRatioFamily.PROFIT
    )
    assert profit_history.complete is True
    assert {item.ratio_family for item in result.family_completeness} == set(
        FinancialRatioFamily
    )
    assert profit_history.covered_reporting_period_ends == tuple(reversed(_PERIODS))
    profit_periods = [
        item
        for item in result.period_completeness
        if item.ratio_family is FinancialRatioFamily.PROFIT
    ]
    assert len(profit_periods) == 8
    assert all(
        item.disposition is FinancialPeriodDisposition.CURRENT_ONLY
        and item.usable_for_current_analysis
        and not item.strict_pit_eligible
        and item.core_coverage >= Decimal("0.9")
        for item in profit_periods
    )
    assert all(
        candidate.filing_metadata.ann_date is not None
        and candidate.filing_metadata.f_ann_date is None
        and candidate.filing_metadata.report_type is None
        and candidate.filing_metadata.comp_type is None
        and candidate.filing_metadata.update_flag is None
        and not candidate.filing_metadata.provider_restatement_lineage_established
        for candidate in result.candidates
    )


@pytest.mark.unit
def test_bank_history_uses_bank_qualified_indicator_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    bank_identity = _identity("601328.SS")
    client = _FakeTushareClient(_complete_bank_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-bank",
        )
        result = harness.adapter.acquire_financial_indicators(
            instrument_identity=bank_identity,
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=_PERIODS,
        )

    legacy_declaration = financial_ratio_field_declaration(
        FinancialCompanyType.BANK,
        FinancialRatioFamily.PROFIT,
    )
    declaration = financial_ratio_field_declaration(
        FinancialCompanyType.BANK,
        FinancialRatioFamily.PROFIT,
        declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
    )
    assert legacy_declaration.declaration_version == "financial-ratio-declarations-v1"
    assert "earnings_per_share" in legacy_declaration.normalized_fields
    assert "net_interest_margin" not in legacy_declaration.normalized_fields
    assert declaration.declaration_version == FINANCIAL_RATIO_DECLARATION_V2
    assert "net_interest_margin" in declaration.normalized_fields
    assert "gross_margin" not in declaration.normalized_fields
    profit_history = next(
        item
        for item in result.family_completeness
        if item.ratio_family is FinancialRatioFamily.PROFIT
    )
    assert profit_history.company_type is FinancialCompanyType.BANK
    assert profit_history.complete is True
    profit_periods = tuple(
        item
        for item in result.period_completeness
        if item.ratio_family is FinancialRatioFamily.PROFIT
    )
    assert len(profit_periods) == 8
    assert all(item.core_coverage == Decimal("1") for item in profit_periods)
    assert all(
        item.company_type is FinancialCompanyType.BANK
        and "gross_margin" not in item.declared_core_fields
        for item in profit_periods
    )
    growth_history = next(
        item
        for item in result.family_completeness
        if item.ratio_family is FinancialRatioFamily.GROWTH
    )
    assert growth_history.complete is True
    growth_periods = tuple(
        item
        for item in result.period_completeness
        if item.ratio_family is FinancialRatioFamily.GROWTH
    )
    assert len(growth_periods) == 8
    assert all(item.core_coverage == Decimal("1") for item in growth_periods)
    assert all(
        item.contract_version == "financial-period-selection-v2"
        for item in result.period_completeness
    )


@pytest.mark.unit
def test_sparse_family_period_is_rejected_without_discarding_usable_family(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _complete_non_bank_rows()
    affected_period = _PERIODS[0]
    rows.loc[0, ["ebitda_margin", "grossprofit_margin"]] = None
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = _build_harness(
            plan=plan,
            client=_FakeTushareClient(rows),
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-family-isolation",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=_PERIODS,
        )

    family_results = {item.ratio_family: item for item in result.family_completeness}
    assert family_results[FinancialRatioFamily.PROFIT].complete is False
    assert family_results[
        FinancialRatioFamily.PROFIT
    ].missing_reporting_period_ends == (affected_period,)
    assert family_results[FinancialRatioFamily.BALANCE].complete is True
    affected = {
        item.ratio_family: item
        for item in result.period_completeness
        if item.period_end == affected_period
    }
    assert affected[FinancialRatioFamily.PROFIT].disposition is (
        FinancialPeriodDisposition.REJECTED
    )
    assert affected[FinancialRatioFamily.PROFIT].core_coverage == (
        Decimal(7) / Decimal(9)
    )
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        affected[FinancialRatioFamily.PROFIT].rejection_reasons
    )
    assert affected[FinancialRatioFamily.BALANCE].disposition is (
        FinancialPeriodDisposition.CURRENT_ONLY
    )
    assert result.current_analysis_capable is True
    rejection = next(
        item
        for item in result.row_rejections
        if item.ratio_family is FinancialRatioFamily.PROFIT
        and item.candidate_identity == affected[FinancialRatioFamily.PROFIT].candidate_identity
    )
    assert rejection.reasons == (
        FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,
    )


@pytest.mark.unit
def test_missing_required_reporting_period_fails_aggregate_gate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _complete_non_bank_rows().iloc[:-1].copy(deep=True)
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = _build_harness(
            plan=plan,
            client=_FakeTushareClient(rows),
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-missing-period",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=_PERIODS,
        )

    profit = next(
        item
        for item in result.family_completeness
        if item.ratio_family is FinancialRatioFamily.PROFIT
    )
    assert profit.complete is False
    assert profit.missing_reporting_period_ends == (_PERIODS[-1],)
    assert profit.rejection_reasons == (
        FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,
    )


@pytest.mark.unit
def test_indicator_rows_remain_distinct_and_cannot_satisfy_statement_requests(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _complete_non_bank_rows().iloc[0].to_dict()
    revision = dict(first)
    revision.update({"ann_date": "20240728", "roe": 99})
    rows = pd.DataFrame([first, revision])
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = _build_harness(
            plan=plan,
            client=_FakeTushareClient(rows),
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-duplicate-revisions",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=(first_period := _PERIODS[0],),
        )

    profit_candidates = tuple(
        item
        for item in result.candidates
        if item.period_identity.ratio_family is FinancialRatioFamily.PROFIT
    )
    assert len(profit_candidates) == 2
    assert {item.period_identity.period_end for item in profit_candidates} == {
        first_period
    }
    assert len({item.candidate_identity for item in profit_candidates}) == 2
    assert len({item.artifact.artifact_identity for item in profit_candidates}) == 2
    assert {item.filing_metadata.ann_date for item in profit_candidates} == {
        date(2024, 7, 25),
        date(2024, 7, 28),
    }
    assert all(
        item.filing_metadata.f_ann_date is None
        and item.filing_metadata.provider_filing_revision_id is None
        and not item.filing_metadata.provider_restatement_lineage_established
        for item in profit_candidates
    )

    statement_assessment = assess_financial_period_candidate(
        profit_candidates[0],
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_identity(),
        expected_frequency=FinancialReportingFrequency.QUARTERLY,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert FinancialPeriodRejectionReason.CAPABILITY_MISMATCH in (
        statement_assessment.rejection_reasons
    )
    assert statement_assessment.usable_for_current_analysis is False
    assert result.metadata_availability.f_ann_date is False
    assert result.metadata_availability.report_scope is False
    assert result.metadata_availability.comp_type is False
    assert result.metadata_availability.stable_restatement_identity is False
    assert result.strict_no_lookahead_eligible is False
    assert result.strict_replay_eligible is False
    assert result.restatement_lineage_established is False


@pytest.mark.unit
def test_unknown_company_type_and_incompatible_unit_currency_are_typed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    unknown_resolution = resolve_financial_company_type(
        provider_declared_type=None,
        provider_declaration_qualified=False,
        classifier_metadata={},
        present_fields=(),
    )
    incompatible_rows = _complete_non_bank_rows().iloc[[0]].copy(deep=True)
    incompatible_rows["unit"] = "CNY_PER_SHARE"
    incompatible_rows["currency"] = "USD"

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        unknown = _build_harness(
            plan=plan,
            client=_FakeTushareClient(_complete_non_bank_rows().iloc[[0]]),
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-unknown-company",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=unknown_resolution,
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=(_PERIODS[0],),
        )
        incompatible = _build_harness(
            plan=plan,
            client=_FakeTushareClient(incompatible_rows),
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-incompatible-unit-currency",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=(_PERIODS[0],),
        )

    assert unknown.provider_artifact is not None
    assert unknown.family_completeness == ()
    assert unknown.current_analysis_capable is False
    assert all(
        FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE
        in item.rejection_reasons
        for item in unknown.period_completeness
    )
    unknown_reasons = {reason for item in unknown.row_rejections for reason in item.reasons}
    assert FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE in unknown_reasons

    assert incompatible.provider_artifact is not None
    incompatible_reasons = {
        reason for item in incompatible.row_rejections for reason in item.reasons
    }
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT in incompatible_reasons
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY in incompatible_reasons
    assert incompatible.current_analysis_capable is False


@pytest.mark.unit
@pytest.mark.parametrize("response_unit", ["RATIO", "MIXED"])
def test_wrong_or_ambiguous_response_unit_is_rejected_before_conversion(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    response_unit: str,
) -> None:
    rows = _complete_non_bank_rows().iloc[[0]].copy(deep=True)
    rows["unit"] = response_unit
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = _build_harness(
            plan=plan,
            client=_FakeTushareClient(rows),
            store=store,
            artifact_store=artifact_store,
            run_scope_id=f"ticket-05-unit-{response_unit.casefold()}",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=(_PERIODS[0],),
        )

    assert not any(
        candidate.period_identity.ratio_family is FinancialRatioFamily.PROFIT
        for candidate in result.candidates
    )
    assert any(
        rejection.ratio_family is FinancialRatioFamily.PROFIT
        and rejection.reasons == (FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT,)
        for rejection in result.row_rejections
    )


@pytest.mark.unit
def test_result_enforces_bulk_row_lineage_and_every_row_disposition(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = _build_harness(
            plan=plan,
            client=_FakeTushareClient(_complete_non_bank_rows().iloc[[0]]),
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-result-invariants",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=(_PERIODS[0],),
        )

    assert result.artifact is not None
    rogue_bulk = FinancialProviderArtifactIdentity.create(
        dataset=result.artifact.dataset,
        canonical_request={"request": "different"},
        provider_metadata={"provider_id": "tushare"},
        retrieved_at=result.artifact.retrieved_at,
        observed_at=result.artifact.observed_at,
        payload={"row": "different"},
    )
    rogue_payload = result.model_dump(mode="python")
    rogue_payload["artifact"] = rogue_bulk.model_dump(mode="python")
    with pytest.raises(ValueError, match="row artifact contradicts bulk artifact"):
        TushareFinancialIndicatorAdapterResult.model_validate(rogue_payload)

    orphan_identity = result.row_artifacts[0].artifact_identity
    orphan_payload = result.model_dump(mode="python")
    removed_candidates = {
        candidate.candidate_identity
        for candidate in result.candidates
        if candidate.artifact.artifact_identity == orphan_identity
    }
    orphan_payload["candidates"] = [
        candidate
        for candidate in orphan_payload["candidates"]
        if candidate["candidate_identity"] not in removed_candidates
    ]
    orphan_payload["period_completeness"] = [
        assessment
        for assessment in orphan_payload["period_completeness"]
        if assessment["candidate_identity"] not in removed_candidates
    ]
    orphan_payload["row_rejections"] = [
        rejection
        for rejection in orphan_payload["row_rejections"]
        if rejection["artifact_identity"] != orphan_identity
    ]
    with pytest.raises(ValueError, match="row artifact has no disposition"):
        TushareFinancialIndicatorAdapterResult.model_validate(orphan_payload)


@pytest.mark.unit
def test_rejection_identities_and_order_are_content_canonical(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = _build_harness(
            plan=plan,
            client=_FakeTushareClient(_complete_non_bank_rows().iloc[[0]]),
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-canonical-rejections",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=(_PERIODS[0],),
        )

    assert result.row_rejections == tuple(
        sorted(result.row_rejections, key=lambda item: item.row_identity)
    )
    rejection = result.row_rejections[0]
    encoded = json.dumps(
        {
            "artifact_identity": rejection.artifact_identity,
            "candidate_identity": rejection.candidate_identity,
            "ratio_family": (
                rejection.ratio_family.value
                if rejection.ratio_family is not None
                else None
            ),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert rejection.row_identity == (
        f"tushare-indicator-row:v1:{sha256(encoded).hexdigest()}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider_result", "expected_kind"),
    [
        (
            RuntimeError(
                "没有接口访问权限 TUSHARE_TOKEN=do-not-persist "
                "https://provider.invalid/query?credential=raw"
            ),
            ProviderSubrequestOutcomeKind.PERMISSION_DENIED,
        ),
        (
            RuntimeError(
                "invalid token do-not-persist "
                "https://provider.invalid/query?credential=raw"
            ),
            ProviderSubrequestOutcomeKind.AUTHENTICATION,
        ),
        (
            _RateLimitError(
                "rate limit TUSHARE_TOKEN=do-not-persist "
                "https://provider.invalid/query?credential=raw"
            ),
            ProviderSubrequestOutcomeKind.RATE_LIMITED,
        ),
        (
            TimeoutError(
                "timeout TUSHARE_TOKEN=do-not-persist "
                "https://provider.invalid/query?credential=raw"
            ),
            ProviderSubrequestOutcomeKind.TIMEOUT,
        ),
        (pd.DataFrame(), ProviderSubrequestOutcomeKind.EMPTY),
        ({"unexpected": "shape"}, ProviderSubrequestOutcomeKind.MALFORMED),
        (
            RuntimeError(
                "provider failure TUSHARE_TOKEN=do-not-persist "
                "https://provider.invalid/query?credential=raw"
            ),
            ProviderSubrequestOutcomeKind.PROVIDER_ERROR,
        ),
    ],
)
def test_provider_failures_are_typed_single_attempt_and_secret_safe(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    provider_result: object,
    expected_kind: ProviderSubrequestOutcomeKind,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(provider_result)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-typed-failure",
        )
        result = harness.adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=_PERIODS,
        )
        checkpoint = harness.cache.checkpoint()

    assert result.outcome.kind is expected_kind
    assert result.provider_artifact is None
    assert result.candidates == ()
    assert len(result.attempt_events) == 1
    assert len(client.calls) == 1
    rendered = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "checkpoint": checkpoint,
        },
        default=str,
        sort_keys=True,
    ).encode("utf-8").lower()
    persisted = b"".join(
        path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()
    ).lower()
    surfaces = rendered + persisted
    assert b"do-not-persist" not in surfaces
    assert b"provider.invalid" not in surfaces
    assert b"credential=raw" not in surfaces
    assert b"tushare_token" not in surfaces


@pytest.mark.unit
def test_concurrent_and_completed_duplicates_share_one_physical_event(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _BlockingTushareClient(_complete_non_bank_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    follower_waiting = Event()

    class _ObservedFuture(Future):
        def result(self, timeout: float | None = None):
            if not self.done():
                follower_waiting.set()
            return super().result(timeout)

    monkeypatch.setattr(provider_subrequests_module, "Future", _ObservedFuture)

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-single-flight",
        )

        def acquire():
            return harness.adapter.acquire_financial_indicators(
                instrument_identity=_identity(),
                company_type_resolution=_company_type_resolution(
                    FinancialCompanyType.INDUSTRIAL_NON_BANK
                ),
                range_start=date(2021, 1, 1),
                as_of_date=date(2026, 7, 30),
                eligible_reporting_period_ends=_PERIODS,
            )

        def acquire_follower():
            assert client.started.wait(timeout=5)
            return acquire()

        def release_after_follower_waits() -> None:
            try:
                assert follower_waiting.wait(timeout=5)
            finally:
                client.release.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            follower = executor.submit(acquire_follower)
            releaser = executor.submit(release_after_follower_waits)
            first = acquire()
            second = follower.result(timeout=5)
            releaser.result(timeout=5)
        completed_duplicate = acquire()
        persisted_events = harness.coordinator.physical_attempt_events(
            first.sequence_id
        )

    assert len(client.calls) == 1
    assert first.provider_artifact == second.provider_artifact
    assert first.artifact == second.artifact == completed_duplicate.artifact
    assert first.candidates == second.candidates == completed_duplicate.candidates
    assert len(first.attempt_events) == 1
    assert second.attempt_events == first.attempt_events
    assert completed_duplicate.attempt_events == first.attempt_events
    assert persisted_events == tuple(
        event
        for event in persisted_events
        if event.sequence_id == first.sequence_id
    )
    assert len(persisted_events) == 1
    assert persisted_events[0].final_physical_attempt_count == 1


@pytest.mark.unit
def test_checkpoint_restore_reprojects_without_provider_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    original_client = _FakeTushareClient(_complete_non_bank_rows())
    resumed_client = _FakeTushareClient(
        RuntimeError("resume must not call provider TUSHARE_TOKEN=do-not-persist")
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        original_harness = _build_harness(
            plan=plan,
            client=original_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-checkpoint",
            owner_id="ticket-05-original",
        )
        original = original_harness.adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=_PERIODS,
        )
        checkpoint = original_harness.cache.checkpoint()
        resumed_harness = _build_harness(
            plan=plan,
            client=resumed_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-checkpoint",
            owner_id="ticket-05-resumed",
            observed_at=_OBSERVED_AT + timedelta(days=1),
            checkpoint=checkpoint,
            coordinator=original_harness.coordinator,
        )
        resumed = resumed_harness.adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=_PERIODS,
        )
        persisted_events = original_harness.coordinator.physical_attempt_events(
            original.sequence_id
        )

    assert len(original_client.calls) == 1
    assert resumed_client.calls == []
    assert resumed.provider_artifact == original.provider_artifact
    assert resumed.artifact == original.artifact
    assert resumed.candidates == original.candidates
    assert resumed.period_completeness == original.period_completeness
    assert resumed.family_completeness == original.family_completeness
    assert resumed.attempt_events == original.attempt_events
    assert len(persisted_events) == 1
    checkpoint_text = json.dumps(checkpoint, sort_keys=True).lower()
    assert "do-not-persist" not in checkpoint_text
    assert "provider-subrequest-artifact=sha256:" in checkpoint_text


@pytest.mark.unit
def test_legacy_mode_cannot_construct_adapter_and_keeps_routes_unchanged() -> None:
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
    assert preflight.plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT) == (
        "akshare",
        "baostock",
        "yfinance",
    )
    assert "tushare" not in preflight.plan.route_for(
        MainlandCapability.FINANCIAL_INDICATORS
    )

    with pytest.raises(
        TushareFinancialIndicatorAdapterConfigurationError
    ) as exc_info:
        TushareFinancialIndicatorAdapter(
            routing_plan=preflight.plan,
            subrequest_cache=object(),  # type: ignore[arg-type]
            sdk_client=object(),  # type: ignore[arg-type]
            owner_id="must-not-run",
            now=lambda: _OBSERVED_AT,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

    assert exc_info.value.reason is (
        TushareFinancialIndicatorAdapterFailureReason.NOT_ENABLED
    )


@pytest.mark.unit
def test_malformed_family_value_preserves_artifact_and_other_families(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _complete_non_bank_rows()
    rows["roe"] = rows["roe"].astype(object)
    rows.loc[0, "roe"] = "not-a-ratio"
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = _build_harness(
            plan=plan,
            client=_FakeTushareClient(rows),
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-05-malformed-family",
        ).adapter.acquire_financial_indicators(
            instrument_identity=_identity(),
            company_type_resolution=_company_type_resolution(
                FinancialCompanyType.INDUSTRIAL_NON_BANK
            ),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
            eligible_reporting_period_ends=_PERIODS,
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert persisted["rows"][0]["roe"] == "not-a-ratio"
    family_results = {item.ratio_family: item for item in result.family_completeness}
    assert family_results[FinancialRatioFamily.PROFIT].complete is False
    assert family_results[FinancialRatioFamily.BALANCE].complete is True
    assert any(
        item.ratio_family is FinancialRatioFamily.PROFIT
        and item.candidate_identity is None
        and item.reasons == (FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,)
        for item in result.row_rejections
    )


@pytest.mark.unit
def test_changed_response_creates_new_artifact_without_overwrite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    first_rows = _complete_non_bank_rows().iloc[[0]].copy(deep=True)
    second_rows = first_rows.copy(deep=True)
    second_rows.loc[0, "roe"] = 99
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)

        def acquire(
            run_scope_id: str,
            rows: pd.DataFrame,
            observed_at: datetime,
        ):
            harness = _build_harness(
                plan=plan,
                client=_FakeTushareClient(rows),
                store=store,
                artifact_store=artifact_store,
                run_scope_id=run_scope_id,
                observed_at=observed_at,
                coordinator=coordinator,
            )
            return harness.adapter.acquire_financial_indicators(
                instrument_identity=_identity(),
                company_type_resolution=_company_type_resolution(
                    FinancialCompanyType.INDUSTRIAL_NON_BANK
                ),
                range_start=date(2021, 1, 1),
                as_of_date=date(2026, 7, 30),
                eligible_reporting_period_ends=(_PERIODS[0],),
            )

        first = acquire("ticket-05-revision-one", first_rows, _OBSERVED_AT)
        second = acquire(
            "ticket-05-revision-two",
            second_rows,
            _OBSERVED_AT + timedelta(minutes=1),
        )

    assert first.provider_artifact is not None
    assert second.provider_artifact is not None
    assert first.provider_artifact != second.provider_artifact
    assert first.artifact != second.artifact
    first_payload = json.loads(artifact_store.read(first.provider_artifact))
    second_payload = json.loads(artifact_store.read(second.provider_artifact))
    assert first_payload["rows"][0]["roe"] == 16
    assert second_payload["rows"][0]["roe"] == 99

from __future__ import annotations

import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import tradingagents.qualified_routing_composition as composition_module
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingDisposition,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityRoutingRequest,
    FinancialCompanyTypeEvidenceRoutingRequest,
    FinancialIndicatorProviderGapRequest,
    FinancialProviderGapRequest,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialRatioFamily,
    FinancialStatementType,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialEligibilityMetadataBinding,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestCacheCheckpoint,
)
from tradingagents.dataflows.qualified_production_sources import (
    AKSHARE_FACTOR_NORMALIZER_VERSION,
    AKSHARE_INDICATOR_NORMALIZER_VERSION,
    AKSHARE_INDICATOR_NORMALIZER_VERSION_V1,
    AKSHARE_NAME_NORMALIZER_VERSION,
    YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION,
    YFINANCE_PROFILE_NORMALIZER_VERSION,
    YFINANCE_STATEMENT_NORMALIZER_VERSION,
    sanitized_provider_failure,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
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
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.qualified_routing_composition import (
    PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    ProductionQualifiedRoutingRunDependencies,
    assemble_production_qualified_provider_sources,
    build_production_qualified_routing_composition_blueprint,
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
            retrieved_at="2026-07-30T00:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )


def _blueprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": [
                "statements",
                "financial_indicators",
                "adjustment_factors",
                "name_events",
            ],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
            "data_cache_dir": str(tmp_path / "cache"),
            "market_history_database_path": str(tmp_path / "history.sqlite3"),
            "market_history_payload_root": str(tmp_path / "payloads"),
            "market_history_backup_root": str(tmp_path / "backups"),
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
    return build_production_qualified_routing_composition_blueprint(
        plan=plan,
        asset_configuration=asset,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        config=config,
        validation_workload=True,
        disposition=MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE,
        environment={"TUSHARE_TOKEN": "fixture-token"},
        dependency_availability=dict.fromkeys(
            ("akshare", "baostock", "tushare", "yfinance"), True
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
        factory_contract_version=PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    )


class _UnusedClient:
    pass


class _YahooTicker:
    def __init__(self, facade: _YahooFacade) -> None:
        self._facade = facade

    @property
    def balance_sheet(self) -> pd.DataFrame:
        return self._statement("balance_sheet")

    @property
    def quarterly_balance_sheet(self) -> pd.DataFrame:
        return self._statement("quarterly_balance_sheet")

    @property
    def income_stmt(self) -> pd.DataFrame:
        return self._statement("income_stmt")

    @property
    def quarterly_income_stmt(self) -> pd.DataFrame:
        return self._statement("quarterly_income_stmt")

    @property
    def cashflow(self) -> pd.DataFrame:
        return self._statement("cashflow")

    @property
    def quarterly_cashflow(self) -> pd.DataFrame:
        return self._statement("quarterly_cashflow")

    def _statement(self, endpoint_id: str) -> pd.DataFrame:
        self._facade.calls.append(endpoint_id)
        return self._facade.statement_frames[endpoint_id].copy()

    def get_info(self) -> dict[str, str]:
        self._facade.calls.append("get_info")
        return {
            "industry": "Banks - Diversified",
            "sector": "Financial Services",
            "raw_token": "must-not-survive",
        }

    def history(
        self,
        *,
        auto_adjust: bool,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        assert auto_adjust is False
        self._facade.calls.append("history")
        self._facade.history_requests.append(
            {"auto_adjust": auto_adjust, "start": start, "end": end}
        )
        return self._facade.history_frame.copy()


class _YahooFacade:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.symbols: list[str] = []
        self.statement_frames = {
            "balance_sheet": pd.DataFrame(
                {
                    pd.Timestamp("2024-12-31"): [1400, 1200, 200],
                    pd.Timestamp("2025-12-31"): [1500, 1300, 200],
                },
                index=[
                    "Total Assets",
                    "Total Liabilities Net Minority Interest",
                    "Stockholders Equity",
                ],
            )
        }
        self.history_requests: list[dict[str, object]] = []
        self.history_frame = pd.DataFrame(
            {"Close": [8.0, 10.0, 20.0], "Adj Close": [4.0, 5.0, 15.0]},
            index=pd.to_datetime(["2020-01-02", "2026-07-29", "2026-07-30"]),
        )

    def Ticker(self, symbol: str) -> _YahooTicker:
        self.symbols.append(symbol)
        return _YahooTicker(self)


class _AkshareFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def stock_financial_analysis_indicator(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("stock_financial_analysis_indicator", kwargs))
        return pd.DataFrame(
            [
                {
                    "日期": "2024-12-31",
                    "净资产收益率(%)": 10,
                },
                {
                    "日期": "2025-12-31",
                    "净资产收益率(%)": 12.5,
                    "净资产收益率": 999,
                    "销售净利率(%)": 8.5,
                }
            ]
        )

    def stock_zh_a_daily(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("stock_zh_a_daily", kwargs))
        return pd.DataFrame(
            [
                {"date": "2020-01-02", "qfq_factor": "0.25"},
                {"date": "2026-07-29", "qfq_factor": "0.5"},
                {"date": "2026-07-30", "qfq_factor": "0.75"},
            ]
        )

    def stock_info_change_name(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("stock_info_change_name", kwargs))
        return pd.DataFrame(
            [
                {
                    "变更日期": "2010-01-02",
                    "证券简称": "交通银行",
                    "变更原因": "简称变更",
                },
                {
                    "变更日期": "2020-01-02",
                    "证券简称": "*ST交通",
                    "变更原因": "简称变更",
                }
            ]
        )

    def stock_financial_report_sina(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("stock_financial_report_sina", kwargs))
        return pd.DataFrame()


class _ScriptedTicker:
    def __init__(self, facade: _ScriptedProviderFacade) -> None:
        self._facade = facade

    @property
    def balance_sheet(self) -> object:
        return self._facade.invoke("balance_sheet")

    def get_info(self) -> object:
        return self._facade.invoke("get_info")

    def history(self, **_kwargs: object) -> object:
        return self._facade.invoke("history")


class _ScriptedProviderFacade:
    def __init__(self, endpoint_id: str, result: object) -> None:
        self.endpoint_id = endpoint_id
        self.result = result
        self.calls: list[str] = []

    def invoke(self, endpoint_id: str) -> object:
        if endpoint_id != self.endpoint_id:
            raise AssertionError("production source performed hidden provider fan-out")
        self.calls.append(endpoint_id)
        if isinstance(self.result, BaseException):
            raise self.result
        if isinstance(self.result, pd.DataFrame):
            return self.result.copy()
        if isinstance(self.result, dict):
            return dict(self.result)
        return self.result

    def Ticker(self, _symbol: str) -> _ScriptedTicker:
        return _ScriptedTicker(self)

    def stock_financial_analysis_indicator(self, **_kwargs: object) -> object:
        return self.invoke("stock_financial_analysis_indicator")

    def stock_zh_a_daily(self, **_kwargs: object) -> object:
        return self.invoke("stock_zh_a_daily")

    def stock_info_change_name(self, **_kwargs: object) -> object:
        return self.invoke("stock_info_change_name")


class _YahooInfoOnlyTicker:
    def __init__(self, facade: _YahooInfoOnlyFacade) -> None:
        self._facade = facade

    @property
    def info(self) -> dict[str, str]:
        self._facade.calls.append("info")
        return {"industry": "Banks - Diversified", "raw_token": "discard-me"}


class _YahooInfoOnlyFacade:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def Ticker(self, _symbol: str) -> _YahooInfoOnlyTicker:
        return _YahooInfoOnlyTicker(self)


class _EmptyTushare:
    def __init__(self) -> None:
        self.calls = 0

    def balancesheet(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        return pd.DataFrame()


class _TushareStatement(_EmptyTushare):
    def balancesheet(self, **_kwargs: object) -> pd.DataFrame:
        self.calls += 1
        return pd.DataFrame(
            [
                {
                    "ts_code": "601328.SH",
                    "ann_date": "20260429",
                    "f_ann_date": "20260430",
                    "end_date": "20251231",
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
            ]
        )


class _AkshareStatement(_AkshareFacade):
    def stock_financial_report_sina(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("stock_financial_report_sina", kwargs))
        return pd.DataFrame(
            [
                {
                    "报告日": "2025-12-31",
                    "公告日期": "2026-04-29",
                    "币种": "CNY",
                    "类型": "合并期末",
                    "更新日期": "2026-04-30T08:00:00+08:00",
                    "现金及存放中央银行款项": 100,
                    "吸收存款": 800,
                    "金融投资": 300,
                    "存放同业款项": 120,
                    "同业及其他金融机构存放款项": 90,
                    "贷款损失准备": 20,
                    "发放贷款和垫款": 700,
                    "资产总计": 1500,
                    "所有者权益合计": 200,
                    "负债合计": 1300,
                }
            ]
        )


def _company_type_resolution():
    return resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        classifier_metadata={},
        present_fields=(),
    )


def _six_binding_requests() -> dict[str, object]:
    indicator_request = FinancialIndicatorProviderGapRequest(
        provider_id="akshare",
        instrument_identity=_identity(),
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.BANK,
        company_type_resolution=_company_type_resolution(),
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        missing_periods_by_family=(
            (FinancialRatioFamily.PROFIT, (date(2025, 12, 31),)),
        ),
    )
    factor_request = FinancialCapabilityRoutingRequest(
        instrument_identity=_identity(),
        capability="adjustment_factors",
        range_start=date(2026, 7, 29),
        as_of_date=date(2026, 7, 30),
    )
    return {
        "financial_statements": FinancialProviderGapRequest(
            provider_id="yfinance",
            instrument_identity=_identity(),
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=date(2026, 7, 30),
            missing_annual_period_ends=(date(2025, 12, 31),),
            missing_reporting_period_ends=(),
        ),
        "financial_indicators": indicator_request,
        "current_financial_profile": indicator_request.model_copy(
            update={"provider_id": "yfinance"}
        ),
        "adjustment_factors": factor_request,
        "derived_adjustment_factors": factor_request,
        "issuer_name_events": FinancialCapabilityRoutingRequest(
            instrument_identity=_identity(),
            capability="name_events",
            range_start=date(2019, 1, 1),
            as_of_date=date(2026, 7, 30),
        ),
    }


@contextmanager
def _production_sources(
    *,
    blueprint,
    tmp_path: Path,
    yahoo: object,
    akshare: object | None = None,
    tushare: object | None = None,
):
    history = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=tmp_path / "coordinator" / "history.sqlite3",
        payload_root=tmp_path / "coordinator" / "payloads",
        backup_root=tmp_path / "coordinator" / "backups",
        data_usage_mode="personal_research",
    )
    with MarketHistoryStore.open(history) as store:
        coordinator = ProviderRequestCoordinator(store)
        for provider_id in ("akshare_sina", "tushare", "yfinance"):
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
        artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=artifact_store,
            run_scope_id="ticket-02d-production-sources",
        )
        dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=coordinator,
            subrequest_cache=cache,
            artifact_store=artifact_store,
            provider_clients={
                "akshare": akshare or _UnusedClient(),
                "baostock": _UnusedClient(),
                "tushare": tushare or _UnusedClient(),
                "yfinance": yahoo,
            },
            now=lambda: datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            sleep=lambda _seconds: None,
            owner_id="ticket-02d-production-sources",
            lease_duration=timedelta(seconds=30),
        )
        yield assemble_production_qualified_provider_sources(
            blueprint=blueprint,
            dependencies=dependencies,
            source_registrations=(
                composition_module._production_executable_source_registrations(blueprint)
            ),
        )


def test_yahoo_statement_production_binding_is_executable_and_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    request = FinancialProviderGapRequest(
        provider_id="yfinance",
        instrument_identity=_identity(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=date(2026, 7, 30),
        missing_annual_period_ends=(date(2025, 12, 31),),
        missing_reporting_period_ends=(),
    )

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
    ) as sources:
        binding = next(
            binding
            for binding in sources.bindings
            if binding.endpoint_id == "financial_statements"
        )
        first = binding.source(request)
        repeated = binding.source(request)

    assert first.kind.value == "available"
    assert first.reason is None
    assert first.candidates
    assert all(
        candidate.filing_metadata.ann_date is None
        and candidate.filing_metadata.f_ann_date is None
        and not candidate.filing_metadata.is_strict_pit_eligible(
            as_of_date=request.as_of_date
        )
        for candidate in first.candidates
    )
    assert len(first.physical_attempt_ids) == 1
    assert repeated.physical_attempt_ids == first.physical_attempt_ids
    assert yahoo.calls == ["balance_sheet"]
    assert yahoo.symbols == ["601328.SS"]


@pytest.mark.parametrize(
    ("endpoint_id", "statement_type", "reporting_period", "frame", "expected_fields"),
    [
        (
            "income_stmt",
            FinancialStatementType.INCOME_STATEMENT,
            False,
            pd.DataFrame(
                {pd.Timestamp("2025-12-31"): [90, 100, 1000]},
                index=[
                    "Net Income Common Stockholders",
                    "Net Income",
                    "Total Revenue",
                ],
            ),
            {"net_income": "100", "operating_revenue": "1000"},
        ),
        (
            "cashflow",
            FinancialStatementType.CASH_FLOW,
            False,
            pd.DataFrame(
                {pd.Timestamp("2025-12-31"): [500, 200]},
                index=["Operating Cash Flow", "Free Cash Flow"],
            ),
            {"cash_from_operations": "500", "free_cash_flow": "200"},
        ),
        (
            "quarterly_balance_sheet",
            FinancialStatementType.BALANCE_SHEET,
            True,
            pd.DataFrame(
                {pd.Timestamp("2025-12-31"): [1500]},
                index=["Total Assets"],
            ),
            {"total_assets": "1500"},
        ),
    ],
)
def test_yahoo_statement_surfaces_deduplicate_aliases_without_fanout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_id: str,
    statement_type: FinancialStatementType,
    reporting_period: bool,
    frame: pd.DataFrame,
    expected_fields: dict[str, str],
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    yahoo.statement_frames = {endpoint_id: frame}
    period = date(2025, 12, 31)
    request = FinancialProviderGapRequest(
        provider_id="yfinance",
        instrument_identity=_identity(),
        statement_type=statement_type,
        as_of_date=date(2026, 7, 30),
        missing_annual_period_ends=() if reporting_period else (period,),
        missing_reporting_period_ends=(period,) if reporting_period else (),
    )

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
    ) as sources:
        binding = next(
            item
            for item in sources.bindings
            if item.endpoint_id == "financial_statements"
        )
        response = binding.source(request)

    assert response.kind.value == "available"
    assert len(response.candidates) == 1
    fields = response.candidates[0].fields
    assert len(fields) == len({field.normalized_field for field in fields})
    assert {
        field.normalized_field: field.normalized_value for field in fields
    } == expected_fields
    assert yahoo.calls == [endpoint_id]


def test_production_catalog_replaces_exactly_six_terminal_sources_without_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    akshare = _AkshareFacade()
    registrations = composition_module._production_executable_source_registrations(
        blueprint
    )
    replaced = {
        ("yfinance", "financial_statements"),
        ("akshare", "financial_indicators"),
        ("yfinance", "current_financial_profile"),
        ("akshare", "adjustment_factors"),
        ("yfinance_derived", "derived_adjustment_factors"),
        ("akshare", "issuer_name_events"),
    }

    assert {
        (registration.provider_id, registration.endpoint_id)
        for registration in registrations
        if registration.endpoint_id != "current_profile_company_type_evidence"
    } == replaced
    assert len(replaced) == 6
    assert len(registrations) == 7
    assert len({registration.registration_id for registration in registrations}) == 7

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
        akshare=akshare,
        tushare=_TushareStatement(),
    ) as sources:
        physical_by_endpoint = {
            registration.endpoint_id: registration.source_factory(
                sources.dependencies
            ).physical_provider_id
            for registration in registrations
        }
        plan_pairs = {
            (route.capability, provider_id)
            for route in blueprint.routing_plan.routes
            if route.capability.value != "daily_market_snapshot"
            for provider_id in route.providers
        }
        bound_pairs = {
            (capability, binding.provider_id)
            for binding in sources.bindings
            for capability in binding.capabilities
        }

    assert physical_by_endpoint == {
        "financial_statements": "yfinance",
        "financial_indicators": "akshare_sina",
        "current_financial_profile": "yfinance",
        "current_profile_company_type_evidence": "yfinance",
        "adjustment_factors": "akshare_sina",
        "derived_adjustment_factors": "yfinance",
        "issuer_name_events": "akshare_sina",
    }
    assert bound_pairs == plan_pairs
    assert yahoo.calls == []
    assert akshare.calls == []


def test_remaining_five_production_bindings_are_executable_typed_and_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    akshare = _AkshareFacade()
    indicator_request = FinancialIndicatorProviderGapRequest(
        provider_id="akshare",
        instrument_identity=_identity(),
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.BANK,
        company_type_resolution=_company_type_resolution(),
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        missing_periods_by_family=(
            (FinancialRatioFamily.PROFIT, (date(2025, 12, 31),)),
        ),
    )
    profile_request = indicator_request.model_copy(update={"provider_id": "yfinance"})
    factor_request = FinancialCapabilityRoutingRequest(
        instrument_identity=_identity(),
        capability="adjustment_factors",
        range_start=date(2026, 7, 29),
        as_of_date=date(2026, 7, 30),
    )
    name_request = FinancialCapabilityRoutingRequest(
        instrument_identity=_identity(),
        capability="name_events",
        range_start=date(2019, 1, 1),
        as_of_date=date(2026, 7, 30),
    )

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
        akshare=akshare,
    ) as sources:
        by_endpoint = {binding.endpoint_id: binding for binding in sources.bindings}
        cases = (
            ("financial_indicators", indicator_request),
            ("current_financial_profile", profile_request),
            ("adjustment_factors", factor_request),
            ("derived_adjustment_factors", factor_request),
            ("issuer_name_events", name_request),
        )
        results = {}
        for endpoint_id, request in cases:
            first = by_endpoint[endpoint_id].source(request)
            repeated = by_endpoint[endpoint_id].source(request)
            assert repeated == first
            assert len(first.physical_attempt_ids) == 1
            results[endpoint_id] = first

    indicator = results["financial_indicators"]
    assert indicator.kind.value == "available"
    assert indicator.candidates
    assert indicator.indicator_metadata_compatibility
    assert all(
        not assessment.compatible
        for assessment in indicator.indicator_metadata_compatibility
    )
    assert all(
        candidate.period_identity.capability.value == "financial_ratio_family"
        for candidate in indicator.candidates
    )
    return_on_equity = tuple(
        field
        for candidate in indicator.candidates
        for field in candidate.fields
        if field.normalized_field == "return_on_equity"
    )
    assert len(return_on_equity) == 1
    assert return_on_equity[0].normalized_value == "0.125"
    profile = results["current_financial_profile"]
    assert profile.kind.value == "available"
    assert profile.degraded_current_profile is True
    assert profile.candidates == ()
    assert profile.indicator_metadata_compatibility == ()
    native_factor = results["adjustment_factors"]
    assert native_factor.kind.value == "available"
    assert native_factor.derived is False
    assert native_factor.provider_history_bundle_identity is None
    assert native_factor.current_tradeability == "unknown"
    derived_factor = results["derived_adjustment_factors"]
    assert derived_factor.kind.value == "available"
    assert derived_factor.derived is True
    assert derived_factor.degraded is True
    assert derived_factor.provider_history_bundle_identity is None
    names = results["issuer_name_events"]
    assert names.kind.value == "available"
    assert names.current_tradeability == "unknown"
    assert names.listing_metadata is None
    assert akshare.calls == [
        ("stock_financial_analysis_indicator", {"symbol": "601328"}),
        (
            "stock_zh_a_daily",
            {
                "symbol": "sh601328",
                "start_date": "20260729",
                "end_date": "20260730",
                "adjust": "qfq-factor",
            },
        ),
        ("stock_info_change_name", {"symbol": "601328"}),
    ]
    assert yahoo.calls == ["get_info", "history"]
    assert yahoo.history_requests == [
        {
            "auto_adjust": False,
            "start": "2026-07-29",
            "end": "2026-07-31",
        }
    ]


def test_all_six_production_sources_restore_as_zero_attempt_cache_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    akshare = _AkshareFacade()
    requests = _six_binding_requests()

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
        akshare=akshare,
    ) as sources:
        bindings = {binding.endpoint_id: binding for binding in sources.bindings}
        first = {
            endpoint_id: bindings[endpoint_id].source(request)
            for endpoint_id, request in requests.items()
        }
        checkpoint = sources.dependencies.subrequest_cache.checkpoint()
        entries = checkpoint["entries"]
        assert len(entries) == 6
        assert {
            entry["key"]["normalizer_version"] for entry in entries
        } == {
            YFINANCE_STATEMENT_NORMALIZER_VERSION,
            AKSHARE_INDICATOR_NORMALIZER_VERSION,
            YFINANCE_PROFILE_NORMALIZER_VERSION,
            AKSHARE_FACTOR_NORMALIZER_VERSION,
            YFINANCE_DERIVED_FACTOR_NORMALIZER_VERSION,
            AKSHARE_NAME_NORMALIZER_VERSION,
        }
        checkpoint_model = ProviderSubrequestCacheCheckpoint.model_validate(checkpoint)
        assert all(
            composition_module._cache_normalizer_is_compatible(
                entry.key,
                blueprint.routing_plan,
            )
            for entry in checkpoint_model.entries
        )
        akshare_indicator_entry = next(
            entry
            for entry in checkpoint_model.entries
            if entry.key.capacity_scope == "financial_indicators"
        )
        assert composition_module._cache_normalizer_is_compatible(
            akshare_indicator_entry.key.model_copy(
                update={
                    "normalizer_version": AKSHARE_INDICATOR_NORMALIZER_VERSION_V1
                }
            ),
            blueprint.routing_plan,
        )
        for provider_id in ("akshare_sina", "yfinance"):
            provider_entries = tuple(
                entry
                for entry in checkpoint_model.entries
                if entry.key.provider_id == provider_id
            )
            for entry in provider_entries:
                incompatible_version = next(
                    sibling.key.normalizer_version
                    for sibling in provider_entries
                    if sibling.key.normalizer_version
                    != entry.key.normalizer_version
                )
                assert not composition_module._cache_normalizer_is_compatible(
                    entry.key.model_copy(
                        update={"normalizer_version": incompatible_version}
                    ),
                    blueprint.routing_plan,
                )
        calls_before_restore = (tuple(yahoo.calls), tuple(akshare.calls))
        restored_cache = ProviderSubrequestCache(
            coordinator=sources.dependencies.coordinator,
            artifact_store=sources.dependencies.artifact_store,
            run_scope_id="ticket-02d-production-sources",
            checkpoint=checkpoint,
        )
        restored_dependencies = ProductionQualifiedRoutingRunDependencies(
            coordinator=sources.dependencies.coordinator,
            subrequest_cache=restored_cache,
            artifact_store=sources.dependencies.artifact_store,
            provider_clients=sources.dependencies.provider_clients,
            now=sources.dependencies.now,
            sleep=sources.dependencies.sleep,
            owner_id=sources.dependencies.owner_id,
            lease_duration=sources.dependencies.lease_duration,
        )
        restored_sources = assemble_production_qualified_provider_sources(
            blueprint=blueprint,
            dependencies=restored_dependencies,
            source_registrations=(
                composition_module._production_executable_source_registrations(
                    blueprint
                )
            ),
        )
        restored_bindings = {
            binding.endpoint_id: binding for binding in restored_sources.bindings
        }
        repeated = {
            endpoint_id: restored_bindings[endpoint_id].source(request)
            for endpoint_id, request in requests.items()
        }

    assert repeated == first
    assert (tuple(yahoo.calls), tuple(akshare.calls)) == calls_before_restore


_PHYSICAL_SURFACE_BY_BINDING = {
    "financial_statements": "balance_sheet",
    "financial_indicators": "stock_financial_analysis_indicator",
    "current_financial_profile": "get_info",
    "adjustment_factors": "stock_zh_a_daily",
    "derived_adjustment_factors": "history",
    "issuer_name_events": "stock_info_change_name",
}


@pytest.mark.parametrize("endpoint_id", tuple(_PHYSICAL_SURFACE_BY_BINDING))
@pytest.mark.parametrize(
    ("scenario", "expected_reason"),
    [
        ("empty", "empty_frame"),
        ("malformed", "malformed_response"),
        ("retryable", "timeout"),
        ("terminal", "provider_error"),
    ],
)
def test_each_production_source_maps_failures_once_without_retry_or_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_id: str,
    scenario: str,
    expected_reason: str,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    surface = _PHYSICAL_SURFACE_BY_BINDING[endpoint_id]
    if scenario == "empty":
        result: object = {} if surface == "get_info" else pd.DataFrame()
    elif scenario == "malformed":
        result = object()
    elif scenario == "retryable":
        result = TimeoutError("private timeout payload")
    else:
        result = RuntimeError("private terminal provider payload")
    facade = _ScriptedProviderFacade(surface, result)

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=facade,
        akshare=facade,
    ) as sources:
        binding = next(
            item for item in sources.bindings if item.endpoint_id == endpoint_id
        )
        request = _six_binding_requests()[endpoint_id]
        first = binding.source(request)
        repeated = binding.source(request)

    assert first.kind.value == "unavailable"
    assert first.reason.value == expected_reason
    assert repeated == first
    assert len(first.physical_attempt_ids) == 1
    assert facade.calls == [surface]


def test_all_six_production_sources_honor_capacity_cooldown_without_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    akshare = _AkshareFacade()
    requests = _six_binding_requests()

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
        akshare=akshare,
    ) as sources:
        bindings = {binding.endpoint_id: binding for binding in sources.bindings}
        for endpoint_id in requests:
            physical_provider_id = (
                "akshare_sina"
                if bindings[endpoint_id].provider_id == "akshare"
                else "yfinance"
            )
            upstream_id, _service_name = upstream_service_identity_for_provider(
                physical_provider_id
            )
            sources.dependencies.coordinator.record_rate_limit(
                upstream_service_id=upstream_id,
                cooldown_scope=endpoint_id,
                observed_at=sources.dependencies.now(),
                retry_after=timedelta(seconds=60),
                provider_code="fixture_capacity",
            )
        responses = {
            endpoint_id: bindings[endpoint_id].source(request)
            for endpoint_id, request in requests.items()
        }

    assert all(
        response.reason.value == "rate_limited"
        and response.physical_attempt_ids == ()
        for response in responses.values()
    )
    assert yahoo.calls == []
    assert akshare.calls == []


def test_all_six_production_sources_singleflight_concurrent_identical_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    akshare = _AkshareFacade()
    requests = _six_binding_requests()

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
        akshare=akshare,
    ) as sources:
        bindings = {binding.endpoint_id: binding for binding in sources.bindings}
        for endpoint_id, request in requests.items():
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = tuple(
                    pool.map(bindings[endpoint_id].source, (request,) * 4)
                )
            assert all(response == responses[0] for response in responses)
            assert len(responses[0].physical_attempt_ids) == 1

    assert yahoo.calls == ["balance_sheet", "get_info", "history"]
    assert [call[0] for call in akshare.calls] == [
        "stock_financial_analysis_indicator",
        "stock_zh_a_daily",
        "stock_info_change_name",
    ]


def test_all_six_production_sources_preserve_partial_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    akshare = _AkshareFacade()
    requests = _six_binding_requests()
    statement_request = requests["financial_statements"]
    assert isinstance(statement_request, FinancialProviderGapRequest)
    requests["financial_statements"] = statement_request.model_copy(
        update={
            "missing_annual_period_ends": (
                date(2025, 12, 31),
                date(2023, 12, 31),
            )
        }
    )
    indicator_request = requests["financial_indicators"]
    assert isinstance(indicator_request, FinancialIndicatorProviderGapRequest)
    requests["financial_indicators"] = indicator_request.model_copy(
        update={
            "missing_periods_by_family": (
                (
                    FinancialRatioFamily.PROFIT,
                    (date(2025, 12, 31), date(2023, 12, 31)),
                ),
            )
        }
    )
    profile_request = requests["current_financial_profile"]
    assert isinstance(profile_request, FinancialIndicatorProviderGapRequest)
    requests["current_financial_profile"] = profile_request.model_copy(
        update={
            "missing_periods_by_family": (
                (
                    FinancialRatioFamily.PROFIT,
                    (date(2025, 12, 31), date(2023, 12, 31)),
                ),
            )
        }
    )
    for endpoint_id in ("adjustment_factors", "derived_adjustment_factors"):
        factor_request = requests[endpoint_id]
        assert isinstance(factor_request, FinancialCapabilityRoutingRequest)
        requests[endpoint_id] = factor_request.model_copy(
            update={"range_start": date(2026, 7, 28)}
        )

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
        akshare=akshare,
    ) as sources:
        bindings = {binding.endpoint_id: binding for binding in sources.bindings}
        responses = {
            endpoint_id: bindings[endpoint_id].source(request)
            for endpoint_id, request in requests.items()
        }
        checkpoint = ProviderSubrequestCacheCheckpoint.model_validate(
            sources.dependencies.subrequest_cache.checkpoint()
        )
        entry_by_scope = {entry.key.capacity_scope: entry for entry in checkpoint.entries}

        def cached_payload(capacity_scope: str) -> dict[str, object]:
            artifact = entry_by_scope[capacity_scope].artifact
            assert artifact is not None
            return json.loads(sources.dependencies.artifact_store.read(artifact))

        native_factor_payload = cached_payload("adjustment_factors")
        derived_factor_payload = cached_payload("derived_adjustment_factors")
        name_payload = cached_payload("issuer_name_events")

    statement = responses["financial_statements"]
    assert {
        candidate.period_identity.period_end for candidate in statement.candidates
    } == {date(2025, 12, 31)}
    indicator = responses["financial_indicators"]
    assert {
        candidate.period_identity.period_end for candidate in indicator.candidates
    } == {date(2025, 12, 31)}
    profile = responses["current_financial_profile"]
    assert profile.degraded_current_profile is True
    assert profile.candidates == ()
    assert responses["adjustment_factors"].candidate_count == 2
    assert responses["derived_adjustment_factors"].candidate_count == 2
    assert responses["issuer_name_events"].candidate_count == 1
    assert [row["trade_date"] for row in native_factor_payload["rows"]] == [
        "2026-07-29",
        "2026-07-30",
    ]
    assert [row["trade_date"] for row in derived_factor_payload["rows"]] == [
        "2026-07-29",
        "2026-07-30",
    ]
    assert [row["start_date"] for row in name_payload["rows"]] == ["2020-01-02"]
    assert yahoo.calls == ["balance_sheet", "get_info", "history"]
    assert [call[0] for call in akshare.calls] == [
        "stock_financial_analysis_indicator",
        "stock_zh_a_daily",
        "stock_info_change_name",
    ]


def test_yahoo_info_fallback_preserves_actual_artifact_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooInfoOnlyFacade()
    profile_request = _six_binding_requests()["current_financial_profile"]
    evidence_request = FinancialCompanyTypeEvidenceRoutingRequest(
        metadata_binding=FinancialEligibilityMetadataBinding(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            availability_scope="current_only",
            financial_eligibility_binding=(
                blueprint.routing_plan.financial_eligibility_binding
            ),
            routing_plan_signature=blueprint.routing_plan.plan_signature,
        )
    )

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
    ) as sources:
        bindings = {binding.endpoint_id: binding for binding in sources.bindings}
        profile = bindings["current_financial_profile"].source(profile_request)
        evidence = bindings["current_profile_company_type_evidence"].source(
            evidence_request
        )

    assert profile.kind.value == "available"
    assert profile.response_artifact is not None
    assert profile.response_artifact.dataset.endpoint_id == "info"
    assert evidence.kind.value == "available"
    assert evidence.evidence is not None
    assert evidence.evidence.artifact_identity == (
        profile.response_artifact.artifact_identity
    )
    assert yahoo.calls == ["info"]


def test_yahoo_derived_factor_rejects_zero_close_without_fill_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    yahoo.history_frame = pd.DataFrame(
        {"Close": [0.0], "Adj Close": [5.0]},
        index=pd.to_datetime(["2026-07-30"]),
    )
    request = FinancialCapabilityRoutingRequest(
        instrument_identity=_identity(),
        capability="adjustment_factors",
        range_start=date(2026, 7, 30),
        as_of_date=date(2026, 7, 30),
    )

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
    ) as sources:
        binding = next(
            item
            for item in sources.bindings
            if item.endpoint_id == "derived_adjustment_factors"
        )
        response = binding.source(request)

    assert response.kind.value == "unavailable"
    assert response.reason.value == "malformed_response"
    assert len(response.physical_attempt_ids) == 1
    assert yahoo.calls == ["history"]


class _StatusFailure(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("error", "outcome", "retryable"),
    [
        (_StatusFailure("token=do-not-leak", 401), PhysicalAttemptOutcome.AUTHENTICATION, False),
        (_StatusFailure("C:\\private\\secret denied", 403), PhysicalAttemptOutcome.PERMISSION_DENIED, False),
        (_StatusFailure("https://provider.invalid/?token=x", 429), PhysicalAttemptOutcome.RATE_LIMITED, True),
        (TimeoutError("private timeout payload"), PhysicalAttemptOutcome.TIMEOUT, True),
        (ConnectionError("C:\\private\\disconnect"), PhysicalAttemptOutcome.DISCONNECT, True),
        (RuntimeError("raw payload token=private"), PhysicalAttemptOutcome.PROVIDER_ERROR, False),
    ],
)
def test_provider_failures_are_typed_without_raw_error_material(
    error: Exception,
    outcome: PhysicalAttemptOutcome,
    retryable: bool,
) -> None:
    failure = sanitized_provider_failure("yfinance", error)

    assert failure.outcome is outcome
    assert failure.retryable is retryable
    assert failure.error_code is not None
    safe = repr(failure.__dict__)
    assert "do-not-leak" not in safe
    assert "token=x" not in safe
    assert "C:\\private" not in safe
    assert "raw payload" not in safe


def test_company_type_evidence_route_has_safe_artifact_bound_yahoo_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    yahoo = _YahooFacade()
    akshare = _AkshareFacade()
    tushare = _EmptyTushare()
    request = FinancialCompanyTypeEvidenceRoutingRequest(
        metadata_binding=FinancialEligibilityMetadataBinding(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            availability_scope="current_only",
            financial_eligibility_binding=(
                blueprint.routing_plan.financial_eligibility_binding
            ),
            routing_plan_signature=blueprint.routing_plan.plan_signature,
        )
    )

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=yahoo,
        akshare=akshare,
        tushare=tushare,
    ) as sources:
        evidence_bindings = tuple(
            binding
            for binding in sources.bindings
            if "financial_company_type_evidence" in {
                capability.value for capability in binding.capabilities
            }
        )
        routed = sources.router.route_company_type_evidence(request)
        profile_binding = next(
            binding
            for binding in sources.bindings
            if binding.endpoint_id == "current_financial_profile"
        )
        profile = profile_binding.source(
            FinancialIndicatorProviderGapRequest(
                provider_id="yfinance",
                instrument_identity=_identity(),
                as_of_date=date(2026, 7, 30),
                company_type=FinancialCompanyType.BANK,
                company_type_resolution=_company_type_resolution(),
                consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
                currency="CNY",
                missing_periods_by_family=(
                    (FinancialRatioFamily.PROFIT, (date(2025, 12, 31),)),
                ),
            )
        )

    assert tuple(binding.provider_id for binding in evidence_bindings) == (
        "tushare",
        "akshare_sina",
        "yfinance",
    )
    assert routed.route == ("tushare", "akshare_sina", "yfinance")
    assert routed.selected_provider == "yfinance"
    assert tuple(call.reason.value if call.reason else None for call in routed.provider_calls) == (
        "empty_frame",
        "empty_frame",
        None,
    )
    evidence = routed.selected_evidence
    assert evidence is not None
    assert evidence.provider_id == "yfinance"
    assert evidence.provider_declared_type is None
    assert evidence.provider_declaration_qualified is False
    assert evidence.artifact_identity.startswith("financial-provider-artifact:v1:")
    assert evidence.source_ref == evidence.artifact_identity
    assert {item.key for item in evidence.normalized_resolver_inputs} <= {
        "metadata:industry",
        "metadata:sector",
    }
    safe = repr(routed)
    assert "must-not-survive" not in safe
    assert "fixture-token" not in safe
    assert len(routed.physical_attempt_ids) == 3
    assert profile.physical_attempt_ids == routed.provider_calls[-1].physical_attempt_ids
    assert profile.degraded_current_profile is True
    assert tushare.calls == 1
    assert yahoo.calls == ["get_info"]


@pytest.mark.parametrize(
    ("tushare_factory", "akshare_factory", "selected_provider", "attempts"),
    [
        (_TushareStatement, _AkshareFacade, "tushare", 1),
        (_EmptyTushare, _AkshareStatement, "akshare_sina", 2),
    ],
)
def test_statement_company_type_evidence_projections_are_artifact_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tushare_factory: type,
    akshare_factory: type,
    selected_provider: str,
    attempts: int,
) -> None:
    blueprint = _blueprint(tmp_path, monkeypatch)
    request = FinancialCompanyTypeEvidenceRoutingRequest(
        metadata_binding=FinancialEligibilityMetadataBinding(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            availability_scope="current_only",
            financial_eligibility_binding=(
                blueprint.routing_plan.financial_eligibility_binding
            ),
            routing_plan_signature=blueprint.routing_plan.plan_signature,
        )
    )

    with _production_sources(
        blueprint=blueprint,
        tmp_path=tmp_path,
        yahoo=_YahooFacade(),
        akshare=akshare_factory(),
        tushare=tushare_factory(),
    ) as sources:
        routed = sources.router.route_company_type_evidence(request)

    assert routed.selected_provider == selected_provider
    evidence = routed.selected_evidence
    assert evidence is not None
    assert evidence.provider_id == selected_provider
    assert evidence.artifact_identity.startswith("financial-provider-artifact:v1:")
    assert evidence.source_ref == evidence.artifact_identity
    assert evidence.resolution_input_identity.startswith(
        "financial-company-type-evidence:v1:"
    )
    assert evidence.instrument_identity == request.metadata_binding.instrument_identity
    assert evidence.as_of_date == request.metadata_binding.as_of_date
    assert len(routed.physical_attempt_ids) == attempts
    assert "DataFrame" not in repr(routed)

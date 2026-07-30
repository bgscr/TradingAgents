from __future__ import annotations

import copy
import importlib.util
import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialListingProvenance,
    FinancialPeriodRejectionReason,
    FinancialReportingFrequency,
    FinancialStatementType,
    assess_financial_history_coverage,
    assess_financial_period_candidate,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.dataflows.tushare_statements import (
    TushareStatementAdapter,
    TushareStatementAdapterConfigurationError,
    TushareStatementAdapterFailureReason,
    TushareStatementSdkClient,
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
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)

_OBSERVED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


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


def _shenzhen_identity() -> InstrumentIdentityEvidence:
    return _identity().model_copy(
        update={
            "symbol": "000333.SZ",
            "venue": "XSHE",
            "display_name": "Recent Listing Fixture",
        }
    )


def _qualified_plan(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": ["statements"],
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


@dataclass(frozen=True)
class _AdapterHarness:
    adapter: TushareStatementAdapter
    cache: ProviderSubrequestCache
    coordinator: ProviderRequestCoordinator


def _build_adapter_harness(
    *,
    plan: MainlandCapabilityRoutingPlan,
    client: TushareStatementSdkClient,
    store: MarketHistoryStore,
    artifact_store: ProviderSubrequestArtifactStore,
    run_scope_id: str,
    owner_id: str = "ticket-04-test",
    observed_at: datetime = _OBSERVED_AT,
    checkpoint: dict[str, object] | None = None,
    coordinator: ProviderRequestCoordinator | None = None,
) -> _AdapterHarness:
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
    adapter = TushareStatementAdapter(
        routing_plan=plan,
        subrequest_cache=cache,
        sdk_client=client,
        owner_id=owner_id,
        now=lambda: observed_at,
        sleep=lambda _seconds: None,
        lease_duration=timedelta(seconds=30),
    )
    return _AdapterHarness(
        adapter=adapter,
        cache=cache,
        coordinator=coordinator,
    )


class _FakeTushareClient:
    def __init__(
        self,
        *,
        balancesheet: object = None,
        income: object = None,
        cashflow: object = None,
    ) -> None:
        self._balancesheet = balancesheet
        self._income = income
        self._cashflow = cashflow
        self.calls: list[tuple[str, dict[str, object]]] = []

    def balancesheet(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("balancesheet", kwargs))
        return self._return(self._balancesheet)

    def income(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("income", kwargs))
        return self._return(self._income)

    def cashflow(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(("cashflow", kwargs))
        return self._return(self._cashflow)

    @staticmethod
    def _return(value: object):
        if isinstance(value, Exception):
            raise value
        if isinstance(value, pd.DataFrame):
            return value.copy(deep=True)
        return value


class _RateLimitError(RuntimeError):
    retry_after_seconds = 30


class _BlockingTushareClient(_FakeTushareClient):
    def __init__(self, frame: pd.DataFrame) -> None:
        super().__init__(balancesheet=frame)
        self.started = Event()
        self.release = Event()
        self._calls_lock = Lock()

    def balancesheet(self, **kwargs: object) -> pd.DataFrame:
        with self._calls_lock:
            self.calls.append(("balancesheet", kwargs))
        self.started.set()
        assert self.release.wait(timeout=5)
        assert isinstance(self._balancesheet, pd.DataFrame)
        return self._balancesheet.copy(deep=True)


def _non_bank_balance_rows() -> pd.DataFrame:
    common = {
        "ts_code": "600895.SH",
        "ann_date": "20260420",
        "f_ann_date": "20260422",
        "report_type": "1",
        "comp_type": "1",
        "update_flag": "0",
        "total_assets": 1000,
        "total_liab": 400,
        "total_hldr_eqy_inc_min_int": 600,
        "money_cap": 100,
        "accounts_receiv": 80,
        "acct_payable": 70,
        "inventories": 60,
        "st_borr": 50,
        "lt_borr": 40,
        "fix_assets": 300,
        "provider_extension": "preserve-me",
    }
    return pd.DataFrame(
        [
            {**common, "end_date": "20251231"},
            {**common, "end_date": "20260331", "update_flag": "1"},
        ]
    )


def _non_bank_income_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "600895.SH",
                "ann_date": "20260420",
                "f_ann_date": "20260422",
                "end_date": "20251231",
                "report_type": "1",
                "comp_type": "1",
                "update_flag": "0",
                "total_revenue": 900,
                "operate_profit": 180,
                "n_income": 120,
                "oper_cost": 500,
                "fin_exp": 20,
                "gross_profit": 400,
                "income_tax": 30,
                "total_cogs": 650,
                "oth_income": 10,
                "rd_exp": 25,
            }
        ]
    )


def _non_bank_cashflow_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "600895.SH",
                "ann_date": "20260420",
                "f_ann_date": "20260422",
                "end_date": "20251231",
                "report_type": "1",
                "comp_type": "1",
                "update_flag": "0",
                "n_cash_flows_fnc_act": 50,
                "n_cashflow_inv_act": -80,
                "n_cashflow_act": 160,
                "c_pay_interest": 10,
                "c_paid_for_taxes": 30,
                "c_fr_sale_sg": 920,
                "c_cash_equ_end_period": 300,
                "eff_fx_flu_cash": 1,
                "n_incr_cash_cash_equ": 131,
                "c_cash_equ_beg_period": 169,
            }
        ]
    )


def _bank_balance_rows() -> pd.DataFrame:
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
                "fin_assets": 300,
                "depos_in_oth_bfi": 120,
                "depos_oth_bfi": 90,
                "loan_loss_reser": 20,
                "loans": 700,
                "total_assets": 1500,
                "total_hldr_eqy_inc_min_int": 200,
                "total_liab": 1300,
                "accounts_receiv": None,
                "inventories": None,
                "fix_assets": None,
            }
        ]
    )


def _bank_income_rows() -> pd.DataFrame:
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
                "int_exp": 40,
                "int_income": 180,
                "n_income": 90,
                "n_int_income": 140,
                "credit_impa_loss": 15,
                "comm_income": 30,
                "income_tax": 20,
                "invest_income": 12,
                "oper_cost": 70,
                "operate_profit": 110,
                "total_revenue": None,
                "gross_profit": None,
                "rd_exp": None,
            }
        ]
    )


@pytest.mark.unit
def test_complete_non_bank_balance_sheet_persists_before_normalizing_and_projects_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=_non_bank_balance_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-balance-sheet",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert client.calls == [
        (
            "balancesheet",
            {
                "ts_code": "600895.SH",
                "start_date": "20210101",
                "end_date": "20260730",
            },
        )
    ]
    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(result.attempt_events) == 1
    assert result.attempt_events[0].capacity_scope == "balancesheet"
    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert persisted["columns"] == list(_non_bank_balance_rows().columns)
    assert persisted["rows"][0]["provider_extension"] == "preserve-me"
    assert persisted["rows"][0]["ann_date"] == "20260420"
    assert result.artifact is not None
    assert result.artifact.dataset.endpoint_id == "balancesheet"
    assert result.artifact.observed_at == _OBSERVED_AT

    assert [
        candidate.period_identity.period_end
        for candidate in result.annual_candidates
    ] == [date(2025, 12, 31)]
    assert [
        candidate.period_identity.period_end
        for candidate in result.reporting_period_candidates
    ] == [date(2025, 12, 31), date(2026, 3, 31)]
    assert len(result.row_artifacts) == 2
    assert all(
        candidate.artifact in result.row_artifacts
        for candidate in (
            *result.annual_candidates,
            *result.reporting_period_candidates,
        )
    )

    annual = result.annual_candidates[0]
    assert annual.period_identity.statement_type is FinancialStatementType.BALANCE_SHEET
    assert annual.period_identity.company_type is FinancialCompanyType.INDUSTRIAL_NON_BANK
    assert annual.period_identity.consolidation_scope is FinancialConsolidationScope.CONSOLIDATED
    assert annual.filing_metadata.ann_date == date(2026, 4, 20)
    assert annual.filing_metadata.f_ann_date == date(2026, 4, 22)
    assert annual.filing_metadata.report_type == "1"
    assert annual.filing_metadata.comp_type == "1"
    assert annual.filing_metadata.update_flag == "0"
    assessment = assess_financial_period_candidate(
        annual,
        requested_capability=annual.period_identity.capability,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_identity(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert assessment.usable_for_current_analysis is True
    assert assessment.strict_pit_eligible is True


@pytest.mark.unit
def test_complete_non_bank_income_statement_uses_declared_core(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(income=_non_bank_income_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-income",
        )
        result = harness.adapter.acquire_income_statement(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert [name for name, _kwargs in client.calls] == ["income"]
    assert result.artifact is not None
    assert result.artifact.dataset.endpoint_id == "income"
    candidate = result.annual_candidates[0]
    assessment = assess_financial_period_candidate(
        candidate,
        requested_capability=candidate.period_identity.capability,
        requested_statement_type=FinancialStatementType.INCOME_STATEMENT,
        requested_ratio_family=None,
        expected_instrument_identity=_identity(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert assessment.critical_coverage == 1
    assert assessment.core_coverage == 1
    assert assessment.strict_pit_eligible is True


@pytest.mark.unit
def test_complete_non_bank_cash_flow_statement_uses_declared_core(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(cashflow=_non_bank_cashflow_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-cash-flow",
        )
        result = harness.adapter.acquire_cash_flow(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert [name for name, _kwargs in client.calls] == ["cashflow"]
    assert result.artifact is not None
    assert result.artifact.dataset.endpoint_id == "cashflow"
    candidate = result.annual_candidates[0]
    assessment = assess_financial_period_candidate(
        candidate,
        requested_capability=candidate.period_identity.capability,
        requested_statement_type=FinancialStatementType.CASH_FLOW,
        requested_ratio_family=None,
        expected_instrument_identity=_identity(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert assessment.critical_coverage == 1
    assert assessment.core_coverage == 1
    assert assessment.strict_pit_eligible is True


@pytest.mark.unit
def test_bank_comp_type_uses_bank_contract_without_industrial_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=_bank_balance_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    bank_identity = _identity().model_copy(
        update={
            "symbol": "601328.SS",
        }
    )

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-bank",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=bank_identity,
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    candidate = result.annual_candidates[0]
    assert candidate.period_identity.company_type is FinancialCompanyType.BANK
    assessment = assess_financial_period_candidate(
        candidate,
        requested_capability=candidate.period_identity.capability,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=bank_identity,
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert assessment.critical_coverage == 1
    assert assessment.core_coverage == 1
    assert "inventory" not in assessment.declared_core_fields


@pytest.mark.unit
def test_bank_income_uses_bank_fields_without_non_bank_revenue_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(income=_bank_income_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    bank_identity = _identity().model_copy(update={"symbol": "601328.SS"})

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-bank-income",
        )
        result = harness.adapter.acquire_income_statement(
            instrument_identity=bank_identity,
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    candidate = result.annual_candidates[0]
    assessment = assess_financial_period_candidate(
        candidate,
        requested_capability=candidate.period_identity.capability,
        requested_statement_type=FinancialStatementType.INCOME_STATEMENT,
        requested_ratio_family=None,
        expected_instrument_identity=bank_identity,
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert candidate.period_identity.company_type is FinancialCompanyType.BANK
    assert assessment.critical_coverage == 1
    assert assessment.core_coverage == 1
    assert "operating_revenue" not in assessment.declared_core_fields


@pytest.mark.unit
def test_mixed_rows_salvage_valid_period_and_reject_cross_symbol_metadata(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = _non_bank_balance_rows().iloc[0].to_dict()
    malformed = {**valid, "end_date": "not-a-period"}
    cross_symbol = {
        **valid,
        "ts_code": "000333.SZ",
        "end_date": "20241231",
    }
    rows = pd.DataFrame([valid, malformed, cross_symbol])
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=rows)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-mixed-symbol-salvage",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert [row["ts_code"] for row in persisted["rows"]] == [
        "600895.SH",
        "600895.SH",
        "000333.SZ",
    ]
    assert [item.period_identity.period_end for item in result.annual_candidates] == [
        date(2025, 12, 31)
    ]
    assert len(result.row_rejections) == 2
    assert all(
        item.reasons == (FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,)
        for item in result.row_rejections
    )


@pytest.mark.unit
def test_legitimate_unequal_provider_fields_use_declared_source_without_merging(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    balance_row = _non_bank_balance_rows().iloc[0].to_dict()
    balance_row["total_hldr_eqy_exc_min_int"] = 550
    income_row = _non_bank_income_rows().iloc[0].to_dict()
    income_row["n_income_attr_p"] = 110
    plan = _qualified_plan(monkeypatch)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        balance_client = _FakeTushareClient(
            balancesheet=pd.DataFrame([balance_row])
        )
        balance_harness = _build_adapter_harness(
            plan=plan,
            client=balance_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-declared-balance-fields",
            coordinator=coordinator,
        )
        balance = balance_harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        income_client = _FakeTushareClient(income=pd.DataFrame([income_row]))
        income_harness = _build_adapter_harness(
            plan=plan,
            client=income_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-declared-income-fields",
            coordinator=coordinator,
        )
        income = income_harness.adapter.acquire_income_statement(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    balance_fields = {
        field.normalized_field: field for field in balance.annual_candidates[0].fields
    }
    income_fields = {
        field.normalized_field: field for field in income.annual_candidates[0].fields
    }
    assert balance_fields["total_equity"].provider_field == (
        "total_hldr_eqy_inc_min_int"
    )
    assert balance_fields["total_equity"].original_value == "600"
    assert income_fields["net_income"].provider_field == "n_income_attr_p"
    assert income_fields["net_income"].original_value == "110"


@pytest.mark.unit
def test_payload_and_row_identities_are_canonical_across_row_order(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    first_rows = _non_bank_balance_rows()
    second_rows = first_rows.iloc[::-1].reset_index(drop=True)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)

        def acquire(
            run_scope_id: str,
            rows: pd.DataFrame,
            observed_at: datetime,
        ):
            client = _FakeTushareClient(balancesheet=rows)
            harness = _build_adapter_harness(
                plan=plan,
                client=client,
                store=store,
                artifact_store=artifact_store,
                run_scope_id=run_scope_id,
                owner_id=run_scope_id,
                observed_at=observed_at,
                coordinator=coordinator,
            )
            return client, harness.adapter.acquire_balance_sheet(
                instrument_identity=_identity(),
                range_start=date(2021, 1, 1),
                as_of_date=date(2026, 7, 30),
            )

        first_client, first = acquire(
            "ticket-04-canonical-payload-one",
            first_rows,
            _OBSERVED_AT,
        )
        second_client, second = acquire(
            "ticket-04-canonical-payload-two",
            second_rows,
            _OBSERVED_AT,
        )

    assert len(first_client.calls) == len(second_client.calls) == 1
    assert first.provider_artifact != second.provider_artifact
    assert first.artifact is not None
    assert second.artifact is not None
    assert first.artifact.payload_sha256 == second.artifact.payload_sha256
    assert first.artifact.artifact_identity == second.artifact.artifact_identity
    assert {item.artifact_identity for item in first.row_artifacts} == {
        item.artifact_identity for item in second.row_artifacts
    }
    assert {item.candidate_identity for item in first.annual_candidates} == {
        item.candidate_identity for item in second.annual_candidates
    }
    assert {
        item.candidate_identity for item in first.reporting_period_candidates
    } == {
        item.candidate_identity for item in second.reporting_period_candidates
    }


@pytest.mark.unit
def test_declared_company_type_with_competing_signature_is_typed_ambiguous(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contradictory = _non_bank_balance_rows().iloc[0].to_dict()
    contradictory.update({"depos": 800, "loans": 700})
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=pd.DataFrame([contradictory]))
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-contradictory-company-signature",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    candidate = result.annual_candidates[0]
    assert candidate.company_type_resolution.ambiguous is True
    assert candidate.company_type_resolution.contradictory is True
    assert result.row_rejections[0].reasons == (
        FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
    )


@pytest.mark.unit
def test_shenzhen_recent_listing_candidates_feed_ticket02_coverage_contract(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _shenzhen_identity()
    rows = _non_bank_balance_rows().copy(deep=True)
    rows["ts_code"] = "000333.SZ"
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=rows)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-shenzhen-recent-listing",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=identity,
            range_start=date(2025, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assessments = tuple(
        assess_financial_period_candidate(
            candidate,
            requested_capability=candidate.period_identity.capability,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=identity,
            expected_frequency=candidate.period_identity.frequency,
            expected_currency="CNY",
            expected_consolidation_scope=(
                FinancialConsolidationScope.CONSOLIDATED
            ),
            pit_as_of_date=date(2026, 7, 30),
        )
        for candidate in (
            *result.annual_candidates,
            *result.reporting_period_candidates,
        )
    )
    coverage = assess_financial_history_coverage(
        instrument_identity=identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=assessments,
        eligible_annual_period_ends=(date(2025, 12, 31),),
        eligible_reporting_period_ends=(
            date(2025, 12, 31),
            date(2026, 3, 31),
        ),
        listing_date=date(2025, 1, 1),
        listing_provenance=FinancialListingProvenance(
            provider_id="fixture_registry",
            source_ref="acq.v1:listing:" + "3" * 64,
            observed_at=_OBSERVED_AT,
        ),
    )

    assert client.calls[0][1]["ts_code"] == "000333.SZ"
    assert coverage.complete is True
    assert coverage.since_listing_exception is not None
    assert coverage.since_listing_exception.supported is True


@pytest.mark.unit
def test_duplicate_period_revisions_and_missing_first_publication_remain_distinct(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _non_bank_balance_rows().iloc[[0]].copy(deep=True)
    revised = rows.iloc[0].to_dict()
    revised.update(
        {
            "f_ann_date": None,
            "update_flag": "1",
            "total_assets": 1001,
        }
    )
    rows = pd.concat([rows, pd.DataFrame([revised])], ignore_index=True)
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=rows)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-duplicate-revisions",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert len(result.annual_candidates) == 2
    assert len({item.candidate_identity for item in result.annual_candidates}) == 2
    assert len({item.artifact.artifact_identity for item in result.annual_candidates}) == 2
    annual_revision_by_update = {
        item.filing_metadata.update_flag: item.artifact.artifact_identity
        for item in result.annual_candidates
    }
    reporting_revision_by_update = {
        item.filing_metadata.update_flag: item.artifact.artifact_identity
        for item in result.reporting_period_candidates
    }
    assert annual_revision_by_update == reporting_revision_by_update
    assert result.artifact is not None
    assert result.artifact.artifact_identity not in set(
        annual_revision_by_update.values()
    )
    assert {item.filing_metadata.update_flag for item in result.annual_candidates} == {
        "0",
        "1",
    }
    assert all(
        item.filing_metadata.provider_filing_revision_id is None
        and not item.filing_metadata.provider_restatement_lineage_established
        for item in result.annual_candidates
    )
    assessments = [
        assess_financial_period_candidate(
            item,
            requested_capability=item.period_identity.capability,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_identity(),
            expected_frequency=FinancialReportingFrequency.ANNUAL,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 30),
        )
        for item in result.annual_candidates
    ]
    assert {item.strict_pit_eligible for item in assessments} == {False, True}
    assert {item.usable_for_current_analysis for item in assessments} == {True}


@pytest.mark.unit
def test_unknown_scope_and_company_type_reject_candidates_without_losing_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown_scope = _non_bank_balance_rows().iloc[0].to_dict()
    unknown_scope["report_type"] = "99"
    unknown_company = _non_bank_balance_rows().iloc[0].to_dict()
    unknown_company.update({"end_date": "20241231", "comp_type": "99"})
    rows = pd.DataFrame([unknown_scope, unknown_company])
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=rows)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-incompatible-metadata",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.provider_artifact is not None
    assert artifact_store.read(result.provider_artifact)
    assessments = {
        item.filing_metadata.comp_type: assess_financial_period_candidate(
            item,
            requested_capability=item.period_identity.capability,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_identity(),
            expected_frequency=FinancialReportingFrequency.ANNUAL,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 30),
        )
        for item in result.annual_candidates
    }
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE in (
        assessments["1"].rejection_reasons
    )
    assert FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE in (
        assessments["99"].rejection_reasons
    )
    assert assessments["99"].company_type is FinancialCompanyType.UNKNOWN
    typed_reasons = {reason for item in result.row_rejections for reason in item.reasons}
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE in (
        typed_reasons
    )
    assert FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE in typed_reasons


@pytest.mark.unit
def test_ambiguous_unit_and_non_cny_currency_are_not_silently_converted(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambiguous_unit = _non_bank_balance_rows().iloc[0].to_dict()
    ambiguous_unit["unit"] = "mystery_scale"
    non_cny = _non_bank_balance_rows().iloc[0].to_dict()
    non_cny.update({"end_date": "20241231", "currency": "USD"})
    rows = pd.DataFrame([ambiguous_unit, non_cny])
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=rows)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-unit-currency",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    by_period = {
        candidate.period_identity.period_end: candidate
        for candidate in result.annual_candidates
    }
    unit_candidate = by_period[date(2025, 12, 31)]
    currency_candidate = by_period[date(2024, 12, 31)]
    assert {field.original_unit for field in unit_candidate.fields} == {
        "mystery_scale"
    }
    assert {field.normalized_unit for field in unit_candidate.fields} == {
        "mystery_scale"
    }
    assert currency_candidate.period_identity.currency == "USD"
    assert {field.normalized_unit for field in currency_candidate.fields} == {
        "USD_UNKNOWN_SCALE"
    }

    def assess(candidate):
        return assess_financial_period_candidate(
            candidate,
            requested_capability=candidate.period_identity.capability,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_identity(),
            expected_frequency=FinancialReportingFrequency.ANNUAL,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 30),
        )

    assert FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT in (
        assess(unit_candidate).rejection_reasons
    )
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY in (
        assess(currency_candidate).rejection_reasons
    )
    typed_reasons = {reason for item in result.row_rejections for reason in item.reasons}
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT in typed_reasons
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY in typed_reasons


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
    client = _FakeTushareClient(balancesheet=provider_result)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    history_config = _history_config(tmp_path)

    with MarketHistoryStore.open(history_config) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-typed-failure",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        checkpoint = harness.cache.checkpoint()

    assert result.outcome.kind is expected_kind
    assert result.provider_artifact is None
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
def test_concurrent_and_completed_identical_calls_reuse_one_physical_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _BlockingTushareClient(_non_bank_balance_rows())
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    follower_waiting = Event()

    class _ObservedFuture(Future):
        def result(self, timeout: float | None = None):
            if not self.done():
                follower_waiting.set()
            return super().result(timeout)

    monkeypatch.setattr(provider_subrequests_module, "Future", _ObservedFuture)

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-single-flight",
        )

        def acquire():
            return harness.adapter.acquire_balance_sheet(
                instrument_identity=_identity(),
                range_start=date(2021, 1, 1),
                as_of_date=date(2026, 7, 30),
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

    assert len(client.calls) == 1
    assert first.provider_artifact == second.provider_artifact
    assert first.artifact == second.artifact == completed_duplicate.artifact
    assert first.annual_candidates == second.annual_candidates
    assert first.annual_candidates == completed_duplicate.annual_candidates
    assert len(first.attempt_events) == 1
    assert second.attempt_events == first.attempt_events


@pytest.mark.unit
def test_checkpoint_resume_reprojects_annual_and_reporting_without_provider_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    original_client = _FakeTushareClient(balancesheet=_non_bank_balance_rows())
    resumed_client = _FakeTushareClient(
        balancesheet=RuntimeError("resume must not call the provider")
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        original_harness = _build_adapter_harness(
            plan=plan,
            client=original_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-checkpoint",
            owner_id="ticket-04-original",
        )
        original = original_harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        checkpoint = original_harness.cache.checkpoint()
        resumed_harness = _build_adapter_harness(
            plan=plan,
            client=resumed_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-checkpoint",
            owner_id="ticket-04-resumed",
            observed_at=_OBSERVED_AT + timedelta(days=1),
            checkpoint=checkpoint,
            coordinator=original_harness.coordinator,
        )
        resumed = resumed_harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert len(original_client.calls) == 1
    assert resumed_client.calls == []
    assert resumed.provider_artifact == original.provider_artifact
    assert resumed.artifact == original.artifact
    assert resumed.annual_candidates == original.annual_candidates
    assert resumed.reporting_period_candidates == original.reporting_period_candidates
    checkpoint_text = json.dumps(checkpoint, sort_keys=True)
    assert "preserve-me" not in checkpoint_text
    assert "provider-subrequest-artifact=sha256:" in checkpoint_text


@pytest.mark.unit
def test_legacy_plan_cannot_construct_adapter_and_keeps_daily_route_unchanged() -> None:
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

    with pytest.raises(TushareStatementAdapterConfigurationError) as exc_info:
        TushareStatementAdapter(
            routing_plan=preflight.plan,
            subrequest_cache=object(),  # type: ignore[arg-type]
            sdk_client=object(),  # type: ignore[arg-type]
            owner_id="must-not-run",
            now=lambda: _OBSERVED_AT,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

    assert (
        exc_info.value.reason
        is TushareStatementAdapterFailureReason.NOT_ENABLED
    )


@pytest.mark.unit
def test_incompatible_row_metadata_is_typed_without_losing_decoded_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed_row = _non_bank_balance_rows().iloc[0].to_dict()
    malformed_row["end_date"] = "not-a-period"
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(balancesheet=pd.DataFrame([malformed_row]))
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-04-row-metadata",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert persisted["rows"][0]["end_date"] == "not-a-period"
    assert result.annual_candidates == ()
    assert result.reporting_period_candidates == ()
    assert len(result.row_rejections) == 1
    assert result.row_rejections[0].reasons == (
        FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,
    )


@pytest.mark.unit
def test_changed_provider_revision_installs_a_new_artifact_without_overwrite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    first_rows = _non_bank_balance_rows().iloc[[0]].copy(deep=True)
    second_rows = first_rows.copy(deep=True)
    second_rows.loc[0, "total_assets"] = 1001
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)

        def acquire(run_scope_id: str, rows: pd.DataFrame, observed_at: datetime):
            client = _FakeTushareClient(balancesheet=rows)
            harness = _build_adapter_harness(
                plan=plan,
                client=client,
                store=store,
                artifact_store=artifact_store,
                run_scope_id=run_scope_id,
                owner_id=run_scope_id,
                observed_at=observed_at,
                coordinator=coordinator,
            )
            return client, harness.adapter.acquire_balance_sheet(
                instrument_identity=_identity(),
                range_start=date(2021, 1, 1),
                as_of_date=date(2026, 7, 30),
            )

        first_client, first = acquire(
            "ticket-04-revision-one",
            first_rows,
            _OBSERVED_AT,
        )
        second_client, second = acquire(
            "ticket-04-revision-two",
            second_rows,
            _OBSERVED_AT + timedelta(minutes=1),
        )

    assert len(first_client.calls) == len(second_client.calls) == 1
    assert first.provider_artifact is not None
    assert second.provider_artifact is not None
    assert first.provider_artifact != second.provider_artifact
    assert first.artifact != second.artifact
    first_payload = json.loads(artifact_store.read(first.provider_artifact))
    second_payload = json.loads(artifact_store.read(second.provider_artifact))
    assert first_payload["rows"][0]["total_assets"] == 1000
    assert second_payload["rows"][0]["total_assets"] == 1001

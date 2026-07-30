from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import Event, Lock

import pandas as pd
import pytest

import tradingagents.dataflows.provider_subrequests as provider_subrequests_module
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingPlan,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows import interface, y_finance
from tradingagents.dataflows.akshare_sina_statements import (
    AkshareSinaStatementAdapter,
    AkshareSinaStatementSdkClient,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialListingProvenance,
    FinancialPeriodDisposition,
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


def _bank_identity() -> InstrumentIdentityEvidence:
    return _identity().model_copy(update={"symbol": "601328.SS"})


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
    adapter: AkshareSinaStatementAdapter
    cache: ProviderSubrequestCache
    coordinator: ProviderRequestCoordinator


def _build_adapter_harness(
    *,
    plan: MainlandCapabilityRoutingPlan,
    client: AkshareSinaStatementSdkClient,
    store: MarketHistoryStore,
    artifact_store: ProviderSubrequestArtifactStore,
    run_scope_id: str,
    checkpoint: dict[str, object] | None = None,
    coordinator: ProviderRequestCoordinator | None = None,
) -> _AdapterHarness:
    coordinator = coordinator or ProviderRequestCoordinator(store)
    upstream_id, service_name = upstream_service_identity_for_provider("akshare_sina")
    coordinator.register_upstream_service(upstream_id, service_name)
    cache = ProviderSubrequestCache(
        coordinator=coordinator,
        artifact_store=artifact_store,
        run_scope_id=run_scope_id,
        checkpoint=checkpoint,
    )
    adapter = AkshareSinaStatementAdapter(
        routing_plan=plan,
        subrequest_cache=cache,
        sdk_client=client,
        owner_id="ticket-07-test",
        now=lambda: _OBSERVED_AT,
        sleep=lambda _seconds: None,
        lease_duration=timedelta(seconds=30),
    )
    return _AdapterHarness(adapter=adapter, cache=cache, coordinator=coordinator)


class _FakeAkshareSinaClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[dict[str, str]] = []

    def stock_financial_report_sina(
        self,
        *,
        stock: str,
        symbol: str,
    ) -> pd.DataFrame:
        self.calls.append({"stock": stock, "symbol": symbol})
        response = self._responses[symbol]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, pd.DataFrame):
            return response.copy(deep=True)
        return response  # type: ignore[return-value]


class _BlockingAkshareSinaClient(_FakeAkshareSinaClient):
    def __init__(self, frame: pd.DataFrame) -> None:
        super().__init__({"资产负债表": frame})
        self.started = Event()
        self.release = Event()
        self._calls_lock = Lock()

    def stock_financial_report_sina(
        self,
        *,
        stock: str,
        symbol: str,
    ) -> pd.DataFrame:
        with self._calls_lock:
            self.calls.append({"stock": stock, "symbol": symbol})
        self.started.set()
        assert self.release.wait(timeout=5)
        response = self._responses[symbol]
        assert isinstance(response, pd.DataFrame)
        return response.copy(deep=True)


class _RateLimitError(RuntimeError):
    retry_after_seconds = 30


def _non_bank_balance_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "报告日": "2025-12-31",
                "公告日期": "2026-04-20",
                "币种": "CNY",
                "类型": "合并期末",
                "更新日期": "2026-04-22T08:30:00+08:00",
                "修订标识": "unqualified-provider-field-1",
                "资产总计": 1_000,
                "负债合计": 400,
                "所有者权益合计": 600,
                "货币资金": 100,
                "应收账款": 80,
                "存货": 60,
                "应付账款": 70,
                "短期借款": 50,
                "长期借款": 40,
                "固定资产": 300,
                "提供方扩展字段": "逐字保留",
            },
            {
                "报告日": "2026-03-31",
                "公告日期": "2026-04-28",
                "币种": "CNY",
                "类型": "合并期末",
                "更新日期": "2026-04-29T09:00:00+08:00",
                "修订标识": "unqualified-provider-field-2",
                "资产总计": 1_050,
                "负债合计": 420,
                "所有者权益合计": 630,
                "货币资金": 110,
                "应收账款": 82,
                "存货": 61,
                "应付账款": 73,
                "短期借款": 49,
                "长期借款": 39,
                "固定资产": 305,
                "提供方扩展字段": "逐字保留-季度",
            },
        ]
    )


def _non_bank_income_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "报告日": "2025-12-31",
                "公告日期": "2026-04-20",
                "币种": "CNY",
                "类型": "合并期末",
                "更新日期": "2026-04-22T08:30:00+08:00",
                "营业总收入": 900,
                "营业利润": 180,
                "净利润": 120,
                "营业成本": 500,
                "财务费用": 20,
                "毛利": 400,
                "所得税费用": 30,
                "营业总成本": 650,
                "其他收益": 10,
                "研发费用": 25,
            }
        ]
    )


def _non_bank_cash_flow_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "报告日": "2025-12-31",
                "公告日期": "2026-04-20",
                "币种": "CNY",
                "类型": "合并期末",
                "更新日期": "2026-04-22T08:30:00+08:00",
                "筹资活动产生的现金流量净额": 50,
                "投资活动产生的现金流量净额": -80,
                "经营活动产生的现金流量净额": 160,
                "支付利息、手续费及佣金的现金": 10,
                "支付的各项税费": 30,
                "销售商品、提供劳务收到的现金": 920,
                "期末现金及现金等价物余额": 300,
                "汇率变动对现金及现金等价物的影响": 1,
                "现金及现金等价物净增加额": 131,
                "期初现金及现金等价物余额": 169,
            }
        ]
    )


def _bank_balance_rows() -> pd.DataFrame:
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
                "资产总计": 1_500,
                "所有者权益合计": 200,
                "负债合计": 1_300,
            }
        ]
    )


def _non_bank_balance_history(period_ends: tuple[date, ...]) -> pd.DataFrame:
    template = _non_bank_balance_rows().iloc[0].to_dict()
    rows: list[dict[str, object]] = []
    for index, period_end in enumerate(period_ends):
        row = dict(template)
        row["报告日"] = period_end.isoformat()
        row["公告日期"] = date(
            period_end.year + (1 if period_end.month == 12 else 0),
            4 if period_end.month == 12 else min(period_end.month + 2, 12),
            20,
        ).isoformat()
        row["资产总计"] = 1_000 + index
        row["提供方扩展字段"] = f"period-{period_end.isoformat()}"
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.mark.unit
def test_complete_non_bank_balance_sheet_uses_sina_endpoint_and_emits_periods(
    tmp_path,
) -> None:
    client = _FakeAkshareSinaClient({"资产负债表": _non_bank_balance_rows()})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-balance-sheet",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert client.calls == [{"stock": "sh600895", "symbol": "资产负债表"}]
    assert result.statement_type is FinancialStatementType.BALANCE_SHEET
    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(result.attempt_events) == 1
    assert result.attempt_events[0].capacity_scope == (
        "stock_financial_report_sina.balance_sheet"
    )
    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert persisted["monetary_unit_contract"] == (
        "akshare-1.18.73-sina-item-value-cny-base-unit-v1"
    )
    assert persisted["columns"] == list(_non_bank_balance_rows().columns)
    assert persisted["rows"][0]["提供方扩展字段"] == "逐字保留"
    assert persisted["rows"][0]["更新日期"] == "2026-04-22T08:30:00+08:00"
    assert persisted["rows"][0]["修订标识"] == "unqualified-provider-field-1"
    assert result.artifact is not None
    assert result.artifact.dataset.provider_id == "akshare_sina"
    assert result.artifact.dataset.endpoint_id == "stock_financial_report_sina"
    assert result.artifact.retrieved_at == _OBSERVED_AT
    assert result.artifact.observed_at == _OBSERVED_AT
    assert len(result.row_artifacts) == 2
    assert [
        item.period_identity.period_end for item in result.annual_candidates
    ] == [date(2025, 12, 31)]
    assert [
        item.period_identity.period_end for item in result.reporting_period_candidates
    ] == [date(2025, 12, 31), date(2026, 3, 31)]

    annual = result.annual_candidates[0]
    assert annual.period_identity.instrument_identity == _identity()
    assert annual.period_identity.currency == "CNY"
    assert annual.period_identity.company_type is FinancialCompanyType.INDUSTRIAL_NON_BANK
    assert (
        annual.period_identity.consolidation_scope
        is FinancialConsolidationScope.CONSOLIDATED
    )
    assert annual.filing_metadata.ann_date == date(2026, 4, 20)
    assert annual.filing_metadata.f_ann_date is None
    assert annual.filing_metadata.report_type == "合并期末"
    assert annual.filing_metadata.comp_type is None
    assert annual.filing_metadata.update_flag == "2026-04-22T08:30:00+08:00"
    assert annual.filing_metadata.provider_filing_revision_id is None
    assert annual.filing_metadata.provider_restatement_lineage_established is False

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
    assert assessment.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert assessment.usable_for_current_analysis is True
    assert assessment.strict_pit_eligible is False
    assert assessment.critical_coverage == 1
    assert assessment.core_coverage == 1


@pytest.mark.unit
def test_complete_non_bank_income_statement_uses_qualified_endpoint_scale(
    tmp_path,
) -> None:
    client = _FakeAkshareSinaClient({"利润表": _non_bank_income_rows()})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-income-statement",
        )
        result = harness.adapter.acquire_income_statement(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert client.calls == [{"stock": "sh600895", "symbol": "利润表"}]
    assert result.statement_type is FinancialStatementType.INCOME_STATEMENT
    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(result.attempt_events) == 1
    assert result.attempt_events[0].capacity_scope == (
        "stock_financial_report_sina.income_statement"
    )
    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert persisted["rows"][0]["营业总收入"] == 900
    candidate = result.annual_candidates[0]
    revenue = next(
        field
        for field in candidate.fields
        if field.normalized_field == "operating_revenue"
    )
    assert revenue.original_value == "900"
    assert revenue.original_unit is None
    assert revenue.normalized_value == "900"
    assert revenue.normalized_unit == "CNY_BASE_UNIT"
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
    assert assessment.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert assessment.critical_coverage == 1
    assert assessment.core_coverage == 1


@pytest.mark.unit
def test_complete_non_bank_cash_flow_emits_statement_candidates(tmp_path) -> None:
    client = _FakeAkshareSinaClient({"现金流量表": _non_bank_cash_flow_rows()})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-cash-flow",
        )
        result = harness.adapter.acquire_cash_flow(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert client.calls == [{"stock": "sh600895", "symbol": "现金流量表"}]
    assert result.statement_type is FinancialStatementType.CASH_FLOW
    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(result.attempt_events) == 1
    assert result.provider_artifact is not None
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
    assert assessment.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert assessment.critical_coverage == 1
    assert assessment.core_coverage == 1


@pytest.mark.unit
def test_bank_balance_passes_bank_contract_without_industrial_fields(tmp_path) -> None:
    client = _FakeAkshareSinaClient({"资产负债表": _bank_balance_rows()})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-bank-balance",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_bank_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert client.calls == [{"stock": "sh601328", "symbol": "资产负债表"}]
    candidate = result.annual_candidates[0]
    assert candidate.period_identity.company_type is FinancialCompanyType.BANK
    assert candidate.filing_metadata.comp_type is None
    assert {field.normalized_field for field in candidate.fields} == {
        "cash_and_central_bank",
        "customer_deposits",
        "financial_investments",
        "interbank_assets",
        "interbank_liabilities",
        "loan_loss_allowance",
        "loans_and_advances",
        "total_assets",
        "total_equity",
        "total_liabilities",
    }
    assessment = assess_financial_period_candidate(
        candidate,
        requested_capability=candidate.period_identity.capability,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_bank_identity(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 30),
    )
    assert assessment.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert assessment.critical_coverage == 1
    assert assessment.core_coverage == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failed_symbol", "failed_statement"),
    [
        ("资产负债表", FinancialStatementType.BALANCE_SHEET),
        ("利润表", FinancialStatementType.INCOME_STATEMENT),
        ("现金流量表", FinancialStatementType.CASH_FLOW),
    ],
)
def test_one_statement_failure_preserves_two_independent_siblings(
    tmp_path,
    failed_symbol: str,
    failed_statement: FinancialStatementType,
) -> None:
    unsafe_failure = ConnectionError(
        "remote disconnected at https://unsafe.invalid/report?token=fixture-secret"
    )
    responses: dict[str, object] = {
        "资产负债表": _non_bank_balance_rows().iloc[[0]],
        "利润表": _non_bank_income_rows(),
        "现金流量表": _non_bank_cash_flow_rows(),
    }
    responses[failed_symbol] = unsafe_failure
    client = _FakeAkshareSinaClient(responses)
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id=f"ticket-07-salvage-{failed_statement.value}",
        )
        batch = harness.adapter.acquire_statements(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        checkpoint = harness.cache.checkpoint()

    assert [call["symbol"] for call in client.calls] == [
        "资产负债表",
        "利润表",
        "现金流量表",
    ]
    failed = batch.result_for(failed_statement)
    assert failed.outcome.kind is ProviderSubrequestOutcomeKind.DISCONNECT
    assert failed.provider_artifact is None
    assert len(failed.attempt_events) == 1
    siblings = [
        result
        for result in batch.results
        if result.statement_type is not failed_statement
    ]
    assert len(siblings) == 2
    assert all(
        result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        and result.provider_artifact is not None
        and result.annual_candidates
        for result in siblings
    )
    assert len({result.subrequest_key for result in batch.results}) == 3
    assert sum(len(result.attempt_events) for result in batch.results) == 3
    rendered = batch.model_dump_json() + json.dumps(checkpoint, sort_keys=True)
    assert "fixture-secret" not in rendered
    assert "unsafe.invalid" not in rendered
    assert "token=" not in rendered


@pytest.mark.unit
def test_malformed_period_preserves_usable_independent_period_and_artifact(
    tmp_path,
) -> None:
    malformed = _non_bank_balance_rows().iloc[[0]].copy(deep=True)
    malformed.loc[:, "报告日"] = "2025-11-30"
    frame = pd.concat(
        [_non_bank_balance_rows().iloc[[1]], malformed],
        ignore_index=True,
    )
    client = _FakeAkshareSinaClient({"资产负债表": frame})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-independent-period-salvage",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(result.row_artifacts) == 2
    assert [
        candidate.period_identity.period_end
        for candidate in result.reporting_period_candidates
    ] == [date(2026, 3, 31)]
    assert len(result.row_rejections) == 1
    assert result.row_rejections[0].reasons == (
        FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA,
    )
    assert result.provider_artifact is not None
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert {row["报告日"] for row in persisted["rows"]} == {
        "2025-11-30",
        "2026-03-31",
    }


@pytest.mark.unit
def test_sparse_period_is_rejected_without_erasing_complete_sibling(tmp_path) -> None:
    complete = _non_bank_balance_rows().iloc[[1]].copy(deep=True)
    sparse = _non_bank_balance_rows().iloc[[0]].copy(deep=True)
    for column in (
            "货币资金",
            "应收账款",
            "应付账款",
        "短期借款",
        "长期借款",
        "固定资产",
    ):
        sparse[column] = pd.Series([None], index=sparse.index, dtype=object)
    client = _FakeAkshareSinaClient(
        {"资产负债表": pd.concat([sparse, complete], ignore_index=True)}
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-sparse-period",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assessments = {
        candidate.period_identity.period_end: assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_identity(),
            expected_frequency=candidate.period_identity.frequency,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 30),
        )
        for candidate in result.reporting_period_candidates
    }
    assert assessments[date(2025, 12, 31)].disposition is FinancialPeriodDisposition.REJECTED
    assert (
        FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS
        in assessments[date(2025, 12, 31)].rejection_reasons
    )
    assert assessments[date(2026, 3, 31)].disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert result.provider_artifact is not None
    assert len(result.row_artifacts) == 2
    sparse_rejection = next(
        rejection
        for rejection in result.row_rejections
        if assessments[date(2025, 12, 31)].candidate_identity
        in rejection.candidate_identities
    )
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        sparse_rejection.reasons
    )


@pytest.mark.unit
def test_indicator_or_ratio_payload_cannot_satisfy_statement_request(tmp_path) -> None:
    indicator = pd.DataFrame(
        [
            {
                "报告日": "2025-12-31",
                "公告日期": "2026-04-20",
                "币种": "CNY",
                "单位": "%",
                "类型": "合并",
                "行业": "工业",
                "capability": "financial_ratio_family",
                "净资产收益率(%)": 12.5,
            }
        ]
    )
    client = _FakeAkshareSinaClient({"资产负债表": indicator})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-capability-mismatch",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert result.provider_artifact is not None
    assert not result.annual_candidates
    assert not result.reporting_period_candidates
    assert result.row_rejections[0].reasons == (
        FinancialPeriodRejectionReason.CAPABILITY_MISMATCH,
    )


@pytest.mark.unit
def test_identical_concurrent_subrequests_single_flight_to_one_attempt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _BlockingAkshareSinaClient(_non_bank_balance_rows().iloc[[0]])
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    follower_waiting = Event()

    class _ObservedFuture(provider_subrequests_module.Future):
        def result(self, timeout: float | None = None):
            if not self.done():
                follower_waiting.set()
            return super().result(timeout)

    monkeypatch.setattr(provider_subrequests_module, "Future", _ObservedFuture)

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-single-flight",
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

    assert len(client.calls) == 1
    assert first.subrequest_key == second.subrequest_key
    assert first.provider_artifact == second.provider_artifact
    assert first.artifact == second.artifact
    assert first.sequence_id == second.sequence_id
    assert first.attempt_events == second.attempt_events
    assert len(first.attempt_events) == 1


@pytest.mark.unit
def test_completed_and_checkpoint_restored_subrequest_performs_zero_new_io(
    tmp_path,
) -> None:
    first_client = _FakeAkshareSinaClient(
        {"资产负债表": _non_bank_balance_rows().iloc[[0]]}
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        first_harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=first_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-checkpoint",
        )
        initial = first_harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        completed = first_harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        checkpoint = first_harness.cache.checkpoint()

        restored_client = _FakeAkshareSinaClient(
            {"资产负债表": AssertionError("checkpoint restore attempted I/O")}
        )
        restored_harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=restored_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-checkpoint",
            checkpoint=checkpoint,
        )
        restored = restored_harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert len(first_client.calls) == 1
    assert restored_client.calls == []
    assert completed == initial
    assert restored == initial
    assert len(restored.attempt_events) == 1
    checkpoint_text = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
    assert "提供方扩展字段" not in checkpoint_text
    assert "逐字保留" not in checkpoint_text


@pytest.mark.unit
def test_each_successful_statement_subrequest_has_its_own_immutable_artifact(
    tmp_path,
) -> None:
    client = _FakeAkshareSinaClient(
        {
            "资产负债表": _non_bank_balance_rows().iloc[[0]],
            "利润表": _non_bank_income_rows(),
            "现金流量表": _non_bank_cash_flow_rows(),
        }
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-independent-artifacts",
        )
        batch = harness.adapter.acquire_statements(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert all(
        result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        for result in batch.results
    )
    assert len({result.subrequest_key for result in batch.results}) == 3
    assert len(
        {
            result.provider_artifact.artifact_ref
            for result in batch.results
            if result.provider_artifact is not None
        }
    ) == 3
    assert len(
        {
            result.artifact.artifact_identity
            for result in batch.results
            if result.artifact is not None
        }
    ) == 3
    assert sum(len(result.attempt_events) for result in batch.results) == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    ("response", "expected_kind"),
    [
        (
            RuntimeError(
                "permission denied https://unsafe.invalid?credential=fixture-secret"
            ),
            ProviderSubrequestOutcomeKind.PERMISSION_DENIED,
        ),
        (
            RuntimeError("authentication failed token=fixture-secret"),
            ProviderSubrequestOutcomeKind.AUTHENTICATION,
        ),
        (
            _RateLimitError("too many requests at https://unsafe.invalid"),
            ProviderSubrequestOutcomeKind.RATE_LIMITED,
        ),
        (TimeoutError("timeout token=fixture-secret"), ProviderSubrequestOutcomeKind.TIMEOUT),
        (
            ConnectionError("remote disconnected https://unsafe.invalid"),
            ProviderSubrequestOutcomeKind.DISCONNECT,
        ),
        (pd.DataFrame(), ProviderSubrequestOutcomeKind.EMPTY),
        ({"unexpected": "shape"}, ProviderSubrequestOutcomeKind.MALFORMED),
        (
            RuntimeError("opaque provider failure token=fixture-secret"),
            ProviderSubrequestOutcomeKind.PROVIDER_ERROR,
        ),
    ],
)
def test_transport_failures_are_typed_single_attempt_and_sanitized(
    tmp_path,
    response: object,
    expected_kind: ProviderSubrequestOutcomeKind,
) -> None:
    client = _FakeAkshareSinaClient({"资产负债表": response})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id=f"ticket-07-failure-{expected_kind.value}",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        checkpoint = harness.cache.checkpoint()

    assert len(client.calls) == 1
    assert result.outcome.kind is expected_kind
    assert result.provider_artifact is None
    assert result.artifact is None
    assert not result.row_artifacts
    assert not result.annual_candidates
    assert not result.reporting_period_candidates
    assert len(result.attempt_events) == 1
    assert result.attempt_events[0].outcome is expected_kind
    persisted_surface = result.model_dump_json() + json.dumps(
        checkpoint,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "fixture-secret" not in persisted_surface
    assert "unsafe.invalid" not in persisted_surface
    assert "token=" not in persisted_surface
    assert "credential=" not in persisted_surface


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("scope", FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE),
        ("unit", FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT),
        ("currency", FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY),
        ("company_type", FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE),
        ("metadata", FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA),
    ],
)
def test_incompatible_metadata_rejects_candidate_without_losing_artifact(
    tmp_path,
    case: str,
    expected_reason: FinancialPeriodRejectionReason,
) -> None:
    frame = _non_bank_balance_rows().iloc[[0]].copy(deep=True)
    if case == "scope":
        frame.loc[:, "类型"] = "无法识别的范围"
    elif case == "unit":
        frame.loc[:, "单位"] = "未声明比例"
    elif case == "currency":
        frame.loc[:, "币种"] = "USD"
        frame.loc[:, "单位"] = "美元"
    elif case == "company_type":
        frame.loc[:, "行业"] = "银行"
    else:
        frame["公告日期"] = pd.Series([None], index=frame.index, dtype=object)
    client = _FakeAkshareSinaClient({"资产负债表": frame})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id=f"ticket-07-incompatible-{case}",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert len(result.row_artifacts) == 1
    assert result.reporting_period_candidates
    assert expected_reason in result.row_rejections[0].reasons
    persisted = json.loads(artifact_store.read(result.provider_artifact))
    assert persisted["rows"]


@pytest.mark.unit
def test_unknown_cash_flow_company_type_is_rejected_but_artifact_is_retained(
    tmp_path,
) -> None:
    frame = _non_bank_cash_flow_rows().drop(
        columns=["销售商品、提供劳务收到的现金"]
    )
    client = _FakeAkshareSinaClient({"现金流量表": frame})
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-unknown-cash-company-type",
        )
        result = harness.adapter.acquire_cash_flow(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert result.provider_artifact is not None
    candidate = result.reporting_period_candidates[0]
    assert candidate.period_identity.company_type is FinancialCompanyType.UNKNOWN
    assert candidate.company_type_resolution.ambiguous is True
    assert FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE in (
        result.row_rejections[0].reasons
    )


@pytest.mark.unit
def test_statement_history_supports_five_annual_and_eight_reporting_targets(
    tmp_path,
) -> None:
    period_ends = tuple(
        date(year, month, day)
        for year in range(2021, 2026)
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))
    )
    client = _FakeAkshareSinaClient(
        {"资产负债表": _non_bank_balance_history(period_ends)}
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-normal-history-targets",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assessments = tuple(
        assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_identity(),
            expected_frequency=candidate.period_identity.frequency,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 30),
        )
        for candidate in (
            *result.annual_candidates,
            *result.reporting_period_candidates,
        )
    )
    history = assess_financial_history_coverage(
        instrument_identity=_identity(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=assessments,
        eligible_annual_period_ends=tuple(
            period for period in period_ends if period.month == 12
        ),
        eligible_reporting_period_ends=period_ends,
    )

    assert len(result.row_artifacts) == 20
    assert len(result.annual_candidates) == 5
    assert len(result.reporting_period_candidates) == 20
    assert history.complete is True
    assert history.target_annual_period_ends == (
        date(2025, 12, 31),
        date(2024, 12, 31),
        date(2023, 12, 31),
        date(2022, 12, 31),
        date(2021, 12, 31),
    )
    assert history.target_reporting_period_ends == tuple(
        sorted(period_ends, reverse=True)[:8]
    )


@pytest.mark.unit
def test_statement_history_supports_typed_since_listing_exception(tmp_path) -> None:
    listing_date = date(2024, 6, 1)
    period_ends = (
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    )
    client = _FakeAkshareSinaClient(
        {"资产负债表": _non_bank_balance_history(period_ends)}
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-since-listing",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=listing_date,
            as_of_date=date(2026, 7, 30),
        )

    assessments = tuple(
        assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_identity(),
            expected_frequency=candidate.period_identity.frequency,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 30),
        )
        for candidate in (
            *result.annual_candidates,
            *result.reporting_period_candidates,
        )
    )
    history = assess_financial_history_coverage(
        instrument_identity=_identity(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=assessments,
        eligible_annual_period_ends=tuple(
            period for period in period_ends if period.month == 12
        ),
        eligible_reporting_period_ends=period_ends,
        listing_date=listing_date,
        listing_provenance=FinancialListingProvenance(
            provider_id="fixture_registry",
            source_ref="registry:listing:v1",
            observed_at=_OBSERVED_AT,
        ),
    )

    assert history.complete is True
    assert history.since_listing_exception is not None
    assert history.since_listing_exception.supported is True
    assert history.since_listing_exception.missing_annual_period_ends == ()
    assert history.since_listing_exception.missing_reporting_period_ends == ()


@pytest.mark.unit
def test_akshare_sina_adapter_never_invokes_yahoo_internally(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yahoo_calls: list[str] = []

    def forbidden_yahoo(*_args, **_kwargs):
        yahoo_calls.append("yahoo")
        raise AssertionError("AKShare-Sina adapter invoked Yahoo")

    monkeypatch.setattr(y_finance, "get_balance_sheet", forbidden_yahoo)
    client = _FakeAkshareSinaClient(
        {"资产负债表": ConnectionError("remote disconnected")}
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_adapter_harness(
            plan=_qualified_plan(),
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-07-no-internal-yahoo",
        )
        result = harness.adapter.acquire_balance_sheet(
            instrument_identity=_identity(),
            range_start=date(2021, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.DISCONNECT
    assert yahoo_calls == []


@pytest.mark.unit
def test_yahoo_fallback_remains_separately_observable_at_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unavailable_akshare(*_args, **_kwargs):
        calls.append("akshare")
        raise RuntimeError("fixture provider unavailable")

    def yahoo_balance(*_args, **_kwargs):
        calls.append("yfinance")
        return "# dispatcher-owned Yahoo balance fixture\nfield,value"

    monkeypatch.setattr(
        interface,
        "get_vendor",
        lambda _category, _method=None, _market=None: "akshare,yfinance",
    )
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_balance_sheet"],
        "akshare",
        unavailable_akshare,
    )
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_balance_sheet"],
        "yfinance",
        yahoo_balance,
    )

    rendered = interface.route_to_vendor(
        "get_balance_sheet",
        "600895.SS",
        "quarterly",
        "2026-07-30",
    )

    assert calls == ["akshare", "yfinance"]
    assert rendered.startswith("# dispatcher-owned Yahoo")


@pytest.mark.unit
def test_ticket_07_does_not_activate_legacy_or_daily_routes() -> None:
    mainland = DEFAULT_CONFIG["market_data_vendors"]["cn_a"]
    assert mainland["core_stock_apis"] == "akshare,baostock,yfinance"
    assert mainland["fundamental_data"] == "akshare,yfinance,baostock"
    assert (
        interface.VENDOR_METHODS["get_balance_sheet"]["akshare"]
        is interface.get_akshare_placeholder
    )
    assert (
        interface.VENDOR_METHODS["get_income_statement"]["akshare"]
        is interface.get_akshare_placeholder
    )
    assert (
        interface.VENDOR_METHODS["get_cashflow"]["akshare"]
        is interface.get_akshare_placeholder
    )

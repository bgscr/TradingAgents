from __future__ import annotations

import copy
import importlib.util
import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, Lock

import pandas as pd
import pytest

import tradingagents.dataflows.provider_subrequests as provider_subrequests_module
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingFailureReason,
    MainlandCapabilityRoutingPlan,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.factor_name_contracts import FactorNameRejectionReason
from tradingagents.dataflows.market_snapshot import (
    AuthoritativeMarketSnapshot,
    AuthoritativeTradingStatusValidationError,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.dataflows.tushare_factors_names import (
    TushareAdjustmentFactorAdapter,
    TushareFactorNameAdapterConfigurationError,
    TushareFactorNameAdapterFailureReason,
    TushareFactorNameSdkClient,
    TushareNameEventAdapter,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    build_evidence_state,
)
from tradingagents.market_history import ProvenanceClass
from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
from tradingagents.market_history.coordinator import (
    LeaseDisposition,
    ProviderRequestCoordinator,
    RateLimitScope,
    RequestPriority,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.store import MarketHistoryStore
from tradingagents.picker.normalize import normalize_partition
from tradingagents.picker.pit_models import Dataset

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


def _qualified_plan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: tuple[str, ...] = ("adjustment_factors", "name_events"),
) -> MainlandCapabilityRoutingPlan:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": list(enabled),
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
    def __init__(self, *, factor_result: object, name_result: object | None = None) -> None:
        self.factor_result = factor_result
        self.name_result = name_result
        self.factor_calls: list[dict[str, object]] = []
        self.name_calls: list[dict[str, object]] = []

    def adj_factor(self, **kwargs: object) -> pd.DataFrame:
        self.factor_calls.append(kwargs)
        if isinstance(self.factor_result, Exception):
            raise self.factor_result
        if isinstance(self.factor_result, pd.DataFrame):
            return self.factor_result.copy(deep=True)
        return self.factor_result  # type: ignore[return-value]

    def namechange(self, **kwargs: object) -> pd.DataFrame:
        self.name_calls.append(kwargs)
        if isinstance(self.name_result, Exception):
            raise self.name_result
        if isinstance(self.name_result, pd.DataFrame):
            return self.name_result.copy(deep=True)
        return self.name_result  # type: ignore[return-value]


class _BlockingFactorClient(_FakeTushareClient):
    def __init__(self, *, factor_result: pd.DataFrame, name_result: pd.DataFrame) -> None:
        super().__init__(factor_result=factor_result, name_result=name_result)
        self.started = Event()
        self.release = Event()
        self._calls_lock = Lock()

    def adj_factor(self, **kwargs: object) -> pd.DataFrame:
        with self._calls_lock:
            self.factor_calls.append(kwargs)
        self.started.set()
        assert self.release.wait(timeout=5)
        assert isinstance(self.factor_result, pd.DataFrame)
        return self.factor_result.copy(deep=True)


class _RateLimitError(RuntimeError):
    retry_after_seconds = 30


class _UpstreamRateLimitError(_RateLimitError):
    rate_limit_scope = RateLimitScope.UPSTREAM


@dataclass(frozen=True)
class _Harness:
    adapter: TushareAdjustmentFactorAdapter
    cache: ProviderSubrequestCache
    coordinator: ProviderRequestCoordinator


@dataclass(frozen=True)
class _NameHarness:
    adapter: TushareNameEventAdapter
    cache: ProviderSubrequestCache
    coordinator: ProviderRequestCoordinator


@dataclass(frozen=True)
class _CombinedHarness:
    factor_adapter: TushareAdjustmentFactorAdapter
    name_adapter: TushareNameEventAdapter
    cache: ProviderSubrequestCache
    coordinator: ProviderRequestCoordinator


def _build_factor_harness(
    *,
    plan: MainlandCapabilityRoutingPlan,
    client: TushareFactorNameSdkClient,
    store: MarketHistoryStore,
    artifact_store: ProviderSubrequestArtifactStore,
    run_scope_id: str,
    checkpoint: dict[str, object] | None = None,
    coordinator: ProviderRequestCoordinator | None = None,
    observed_at: datetime = _OBSERVED_AT,
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
        adapter=TushareAdjustmentFactorAdapter(
            routing_plan=plan,
            subrequest_cache=cache,
            sdk_client=client,
            owner_id=run_scope_id,
            now=lambda: observed_at,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ),
        cache=cache,
        coordinator=coordinator,
    )


def _build_name_harness(
    *,
    plan: MainlandCapabilityRoutingPlan,
    client: TushareFactorNameSdkClient,
    store: MarketHistoryStore,
    artifact_store: ProviderSubrequestArtifactStore,
    run_scope_id: str,
    checkpoint: dict[str, object] | None = None,
    coordinator: ProviderRequestCoordinator | None = None,
    observed_at: datetime = _OBSERVED_AT,
) -> _NameHarness:
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
    return _NameHarness(
        adapter=TushareNameEventAdapter(
            routing_plan=plan,
            subrequest_cache=cache,
            sdk_client=client,
            owner_id=run_scope_id,
            now=lambda: observed_at,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        ),
        cache=cache,
        coordinator=coordinator,
    )


def _build_combined_harness(
    *,
    plan: MainlandCapabilityRoutingPlan,
    client: TushareFactorNameSdkClient,
    store: MarketHistoryStore,
    artifact_store: ProviderSubrequestArtifactStore,
    run_scope_id: str,
    checkpoint: dict[str, object] | None = None,
    coordinator: ProviderRequestCoordinator | None = None,
    observed_at: datetime = _OBSERVED_AT,
) -> _CombinedHarness:
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
    common = {
        "routing_plan": plan,
        "subrequest_cache": cache,
        "sdk_client": client,
        "owner_id": run_scope_id,
        "now": lambda: observed_at,
        "sleep": lambda _seconds: None,
        "lease_duration": timedelta(seconds=30),
    }
    return _CombinedHarness(
        factor_adapter=TushareAdjustmentFactorAdapter(**common),
        name_adapter=TushareNameEventAdapter(**common),
        cache=cache,
        coordinator=coordinator,
    )


@pytest.mark.unit
def test_complete_adjustment_factor_series_is_exact_sorted_and_non_authoritative(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(
        factor_result=pd.DataFrame(
            {
                "ts_code": ["600895.SH", "600895.SH", "600895.SH"],
                "trade_date": ["20260729", "20260725", "20260728"],
                "adj_factor": ["1.300000", "1.200000", "1.250000"],
            }
        )
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_factor_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-factor-complete",
        )
        result = harness.adapter.acquire_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 25),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(client.factor_calls) == 1
    assert client.factor_calls[0] == {
        "ts_code": "600895.SH",
        "start_date": "20260725",
        "end_date": "20260730",
    }
    assert [item.trade_date for item in result.candidates] == [
        date(2026, 7, 25),
        date(2026, 7, 28),
        date(2026, 7, 29),
    ]
    assert [item.adj_factor for item in result.candidates] == [
        Decimal("1.200000"),
        Decimal("1.250000"),
        Decimal("1.300000"),
    ]
    assert [item.original_adj_factor for item in result.candidates] == [
        "1.200000",
        "1.250000",
        "1.300000",
    ]
    assert date(2026, 7, 26) not in {item.trade_date for item in result.candidates}
    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert result.artifact.raw_artifact == result.provider_artifact
    assert result.artifact.dataset.endpoint_id == "adj_factor"
    assert result.artifact.qualification_profile == "cn-a-2000-20260729-v1"
    assert result.artifact.normalizer_version == plan.normalizer_version
    assert result.artifact.retrieved_at == _OBSERVED_AT
    assert result.artifact.observed_at == _OBSERVED_AT
    assert all(item.instrument_identity == _identity() for item in result.candidates)
    assert all(item.canonical_symbol == "600895.SS" for item in result.candidates)
    assert all(item.native_adjustment_factor_revision for item in result.candidates)
    assert all(item.qualified_current_factor_fallback_capable for item in result.candidates)
    assert all(item.raw_artifact == result.provider_artifact for item in result.candidates)
    assert all(
        item.raw_artifact_identity == result.artifact.raw_artifact_identity
        for item in result.candidates
    )
    assert all(
        item.provenance_class is ProvenanceClass.RETROSPECTIVE_BACKFILL
        for item in result.candidates
    )
    assert all(item.first_observed_at == _OBSERVED_AT for item in result.candidates)
    assert all(not item.raw_observation_history_established for item in result.candidates)
    assert all(not item.session_trading_status_established for item in result.candidates)
    assert all(not item.provider_history_bundle_complete for item in result.candidates)
    assert all(not item.strict_replay_eligible for item in result.candidates)


@pytest.mark.unit
def test_duplicate_factor_occurrences_are_retained_and_conflicts_are_typed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(
        factor_result=pd.DataFrame(
            {
                "ts_code": [
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                ],
                "trade_date": ["20260725", "20260725", "20260728", "20260728"],
                "adj_factor": ["1.2000", "1.2000", "1.2500", "1.2600"],
            }
        )
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_factor_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-factor-duplicates",
        )
        result = harness.adapter.acquire_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 25),
            as_of_date=date(2026, 7, 30),
        )

    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert len(result.row_artifacts) == 4
    assert len(result.candidates) == 1
    assert result.candidates[0].trade_date == date(2026, 7, 25)
    assert result.candidates[0].adj_factor == Decimal("1.2000")
    assert len(result.candidates[0].occurrence_artifact_identities) == 2
    assert len(set(result.candidates[0].occurrence_artifact_identities)) == 2
    assert len(result.row_rejections) == 2
    assert {item.reason for item in result.row_rejections} == {
        FactorNameRejectionReason.CONFLICTING_DUPLICATE_FACTOR
    }
    assert {item.trade_date for item in result.row_rejections} == {
        date(2026, 7, 28)
    }


@pytest.mark.unit
def test_malformed_and_incompatible_factor_rows_keep_the_raw_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(
        factor_result=pd.DataFrame(
            {
                "ts_code": ["600895.SH", "000001.SZ", "600895.SH", "600895.SH"],
                "trade_date": ["20260725", "20260726", "20260230", "20260731"],
                "adj_factor": ["1.2000", "1.2100", "not-a-factor", "1.3000"],
            }
        )
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_factor_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-factor-rejections",
        )
        result = harness.adapter.acquire_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 25),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert len(result.row_artifacts) == 4
    assert [item.trade_date for item in result.candidates] == [date(2026, 7, 25)]
    assert [item.reason for item in result.row_rejections] == [
        FactorNameRejectionReason.MALFORMED_RESPONSE,
        FactorNameRejectionReason.INCOMPATIBLE_METADATA,
        FactorNameRejectionReason.INCOMPATIBLE_METADATA,
    ]
    assert {
        *(item.row_artifact_identity for item in result.row_rejections),
        *result.candidates[0].occurrence_artifact_identities,
    } == {item.row_artifact_identity for item in result.row_artifacts}


@pytest.mark.unit
def test_name_history_preserves_overlaps_repeats_and_never_establishes_status(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(
        factor_result=pd.DataFrame(),
        name_result=pd.DataFrame(
            {
                "ts_code": [
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                ],
                "name": ["退市观察", "*ST示例", "普通名称", "G示例", "ST示例", "*ST示例"],
                "start_date": [
                    "20230101",
                    "20220701",
                    "20200101",
                    "20210101",
                    "20211201",
                    "20220701",
                ],
                "end_date": ["", "", "20201231", "20211231", "20220630", ""],
                "change_reason": [
                    "ordinary issuer event",
                    "provider label",
                    "initial name",
                    "share reform",
                    "provider label",
                    "provider label",
                ],
            }
        ),
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_name_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-name-complete",
        )
        result = harness.adapter.acquire_name_events(
            instrument_identity=_identity(),
            range_start=date(2020, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(client.name_calls) == 1
    assert client.name_calls[0] == {
        "ts_code": "600895.SH",
        "start_date": "20200101",
        "end_date": "20260730",
    }
    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert result.artifact.dataset.endpoint_id == "namechange"
    assert len(result.candidates) == 6
    assert [(item.start_date, item.end_date, item.name) for item in result.candidates] == [
        (date(2020, 1, 1), date(2020, 12, 31), "普通名称"),
        (date(2021, 1, 1), date(2021, 12, 31), "G示例"),
        (date(2021, 12, 1), date(2022, 6, 30), "ST示例"),
        (date(2022, 7, 1), None, "*ST示例"),
        (date(2022, 7, 1), None, "*ST示例"),
        (date(2023, 1, 1), None, "退市观察"),
    ]
    repeated = [item for item in result.candidates if item.name == "*ST示例"]
    assert len({item.revision_identity for item in repeated}) == 2
    assert all(item.change_reason is not None for item in result.candidates)
    assert all(not item.st_status_established for item in result.candidates)
    assert all(not item.suspension_status_established for item in result.candidates)
    assert all(not item.current_tradeability_established for item in result.candidates)
    assert all(not item.listing_status_established for item in result.candidates)
    assert all(not item.delisting_status_established for item in result.candidates)
    assert all(not item.source_fact_created for item in result.candidates)
    assert all(not item.decision_ready_evidence_created for item in result.candidates)


@pytest.mark.unit
def test_invalid_reversed_and_contradictory_name_intervals_keep_the_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(
        factor_result=pd.DataFrame(),
        name_result=pd.DataFrame(
            {
                "ts_code": [
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "600895.SH",
                    "000001.SZ",
                ],
                "name": [
                    "Overlap A",
                    "Overlap B",
                    "Reversed",
                    "Impossible",
                    "Contradiction A",
                    "Contradiction B",
                    "",
                    "Wrong Symbol",
                ],
                "start_date": [
                    "20200101",
                    "20210101",
                    "20240101",
                    "20230230",
                    "20250101",
                    "20250101",
                    "20260101",
                    "20260101",
                ],
                "end_date": [
                    "20211231",
                    "20221231",
                    "20230101",
                    "20231231",
                    "20251231",
                    "20251231",
                    "20261231",
                    "20261231",
                ],
                "change_reason": ["fixture"] * 8,
            }
        ),
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_name_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-name-rejections",
        )
        result = harness.adapter.acquire_name_events(
            instrument_identity=_identity(),
            range_start=date(2020, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert result.provider_artifact is not None
    assert result.artifact is not None
    assert [item.name for item in result.candidates] == ["Overlap A", "Overlap B"]
    assert len(result.row_rejections) == 6
    assert [item.reason for item in result.row_rejections].count(
        FactorNameRejectionReason.INVALID_DATE_INTERVAL
    ) == 4
    assert [item.reason for item in result.row_rejections].count(
        FactorNameRejectionReason.MALFORMED_RESPONSE
    ) == 1
    assert [item.reason for item in result.row_rejections].count(
        FactorNameRejectionReason.INCOMPATIBLE_METADATA
    ) == 1
    used_row_ids = {
        *(item.row_artifact_identity for item in result.candidates),
        *(item.row_artifact_identity for item in result.row_rejections),
    }
    assert used_row_ids == {item.row_artifact_identity for item in result.row_artifacts}


@pytest.mark.unit
def test_factor_single_flight_and_name_endpoint_use_distinct_scopes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _BlockingFactorClient(
        factor_result=pd.DataFrame(
            {
                "ts_code": ["600895.SH"],
                "trade_date": ["20260725"],
                "adj_factor": ["1.2000"],
            }
        ),
        name_result=pd.DataFrame(
            {
                "ts_code": ["600895.SH"],
                "name": ["普通名称"],
                "start_date": ["20200101"],
                "end_date": [""],
                "change_reason": ["fixture"],
            }
        ),
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    follower_waiting = Event()

    class _ObservedFuture(Future):
        def result(self, timeout: float | None = None):
            if not self.done():
                follower_waiting.set()
            return super().result(timeout)

    monkeypatch.setattr(provider_subrequests_module, "Future", _ObservedFuture)

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_combined_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-single-flight",
        )

        def acquire_factor():
            return harness.factor_adapter.acquire_adjustment_factors(
                instrument_identity=_identity(),
                range_start=date(2026, 7, 25),
                as_of_date=date(2026, 7, 30),
            )

        def acquire_follower():
            assert client.started.wait(timeout=5)
            return acquire_factor()

        def release_after_follower_waits() -> None:
            try:
                assert follower_waiting.wait(timeout=5)
            finally:
                client.release.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            follower = executor.submit(acquire_follower)
            releaser = executor.submit(release_after_follower_waits)
            first = acquire_factor()
            second = follower.result(timeout=5)
            releaser.result(timeout=5)
        completed_duplicate = acquire_factor()
        name_result = harness.name_adapter.acquire_name_events(
            instrument_identity=_identity(),
            range_start=date(2020, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        factor_events = harness.coordinator.physical_attempt_events(first.sequence_id)
        name_events = harness.coordinator.physical_attempt_events(name_result.sequence_id)

    assert len(client.factor_calls) == 1
    assert len(client.name_calls) == 1
    assert first == second == completed_duplicate
    assert first.subrequest_key != name_result.subrequest_key
    assert len(factor_events) == len(name_events) == 1
    assert factor_events[0].capacity_scope == "adj_factor"
    assert name_events[0].capacity_scope == "namechange"
    assert factor_events[0].upstream_service_id == name_events[0].upstream_service_id


@pytest.mark.unit
def test_checkpoint_restores_both_terminal_endpoints_without_provider_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    original_client = _FakeTushareClient(
        factor_result=pd.DataFrame(
            {
                "ts_code": ["600895.SH"],
                "trade_date": ["20260725"],
                "adj_factor": ["1.2000"],
            }
        ),
        name_result=pd.DataFrame(
            {
                "ts_code": ["600895.SH"],
                "name": ["普通名称"],
                "start_date": ["20200101"],
                "end_date": [""],
                "change_reason": ["fixture"],
            }
        ),
    )
    resumed_client = _FakeTushareClient(
        factor_result=RuntimeError("resume must not call provider token=unsafe"),
        name_result=RuntimeError("resume must not call provider token=unsafe"),
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        original_harness = _build_combined_harness(
            plan=plan,
            client=original_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-checkpoint",
        )
        original_factor = original_harness.factor_adapter.acquire_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 25),
            as_of_date=date(2026, 7, 30),
        )
        original_name = original_harness.name_adapter.acquire_name_events(
            instrument_identity=_identity(),
            range_start=date(2020, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        checkpoint = original_harness.cache.checkpoint()
        resumed_harness = _build_combined_harness(
            plan=plan,
            client=resumed_client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-checkpoint",
            checkpoint=checkpoint,
            coordinator=original_harness.coordinator,
            observed_at=_OBSERVED_AT + timedelta(days=1),
        )
        resumed_factor = resumed_harness.factor_adapter.acquire_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 25),
            as_of_date=date(2026, 7, 30),
        )
        resumed_name = resumed_harness.name_adapter.acquire_name_events(
            instrument_identity=_identity(),
            range_start=date(2020, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert original_client.factor_calls and original_client.name_calls
    assert resumed_client.factor_calls == []
    assert resumed_client.name_calls == []
    assert resumed_factor == original_factor
    assert resumed_name == original_name
    assert len(checkpoint["entries"]) == 2


@pytest.mark.unit
@pytest.mark.parametrize("endpoint", ["factor", "name"])
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
    endpoint: str,
    provider_result: object,
    expected_kind: ProviderSubrequestOutcomeKind,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(
        factor_result=provider_result if endpoint == "factor" else pd.DataFrame(),
        name_result=provider_result if endpoint == "name" else pd.DataFrame(),
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_combined_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id=f"ticket-06-{endpoint}-failure",
        )
        if endpoint == "factor":
            result = harness.factor_adapter.acquire_adjustment_factors(
                instrument_identity=_identity(),
                range_start=date(2026, 7, 25),
                as_of_date=date(2026, 7, 30),
            )
        else:
            result = harness.name_adapter.acquire_name_events(
                instrument_identity=_identity(),
                range_start=date(2020, 1, 1),
                as_of_date=date(2026, 7, 30),
            )
        checkpoint = harness.cache.checkpoint()

    assert result.outcome.kind is expected_kind
    assert result.provider_artifact is None
    assert result.artifact is None
    assert result.candidates == ()
    assert len(result.attempt_events) == 1
    assert len(client.factor_calls if endpoint == "factor" else client.name_calls) == 1
    rendered = json.dumps(
        {"result": result.model_dump(mode="json"), "checkpoint": checkpoint},
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
def test_independent_enablement_legacy_defaults_and_authority_routes_remain_closed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factor_plan = _qualified_plan(monkeypatch, enabled=("adjustment_factors",))
    name_plan = _qualified_plan(monkeypatch, enabled=("name_events",))
    client = _FakeTushareClient(
        factor_result=pd.DataFrame(),
        name_result=pd.DataFrame(),
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    legacy_preflight = preflight_mainland_capability_routing(
        asset,
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )
    assert legacy_preflight.plan is not None
    legacy_plan = legacy_preflight.plan

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        factor_harness = _build_factor_harness(
            plan=factor_plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-factor-enabled-only",
        )
        name_harness = _build_name_harness(
            plan=name_plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-name-enabled-only",
            coordinator=factor_harness.coordinator,
        )
        with pytest.raises(TushareFactorNameAdapterConfigurationError) as name_error:
            TushareNameEventAdapter(
                routing_plan=factor_plan,
                subrequest_cache=factor_harness.cache,
                sdk_client=client,
                owner_id="must-not-call",
                now=lambda: _OBSERVED_AT,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        with pytest.raises(TushareFactorNameAdapterConfigurationError) as factor_error:
            TushareAdjustmentFactorAdapter(
                routing_plan=name_plan,
                subrequest_cache=name_harness.cache,
                sdk_client=client,
                owner_id="must-not-call",
                now=lambda: _OBSERVED_AT,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        with pytest.raises(TushareFactorNameAdapterConfigurationError):
            TushareAdjustmentFactorAdapter(
                routing_plan=legacy_plan,
                subrequest_cache=factor_harness.cache,
                sdk_client=client,
                owner_id="must-not-call",
                now=lambda: _OBSERVED_AT,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )

    assert name_error.value.reason is TushareFactorNameAdapterFailureReason.NAME_EVENTS_NOT_ENABLED
    assert (
        factor_error.value.reason
        is TushareFactorNameAdapterFailureReason.ADJUSTMENT_FACTORS_NOT_ENABLED
    )
    assert client.factor_calls == []
    assert client.name_calls == []
    assert factor_plan.route_for(MainlandCapability.ADJUSTMENT_FACTORS) == (
        "baostock",
        "tushare",
        "akshare",
        "yfinance_derived",
    )
    assert factor_plan.route_for(MainlandCapability.SUSPENSION_STATUS) == ("baostock",)
    assert factor_plan.route_for(MainlandCapability.ISSUER_LIFECYCLE) == ("baostock",)
    assert legacy_plan.route_for(MainlandCapability.DAILY_MARKET_SNAPSHOT) == (
        "akshare",
        "baostock",
        "yfinance",
    )
    assert "tushare" not in legacy_plan.route_for(
        MainlandCapability.DAILY_MARKET_SNAPSHOT
    )


@pytest.mark.unit
def test_invalid_status_enablement_and_missing_secret_fail_before_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    invalid_status_config = copy.deepcopy(DEFAULT_CONFIG)
    invalid_status_config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1",
            "tushare_enabled_capabilities": ["suspension_status"],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    invalid_status = preflight_mainland_capability_routing(
        asset,
        config=invalid_status_config,
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    missing_secret_config = copy.deepcopy(invalid_status_config)
    missing_secret_config["tushare_enabled_capabilities"] = ["name_events"]
    missing_secret = preflight_mainland_capability_routing(
        asset,
        config=missing_secret_config,
        environment={},
    )

    assert not invalid_status.passed
    assert invalid_status.failure is not None
    assert (
        invalid_status.failure.reason
        is MainlandCapabilityRoutingFailureReason.UNSUPPORTED_TUSHARE_CAPABILITY
    )
    assert not missing_secret.passed
    assert missing_secret.failure is not None
    assert (
        missing_secret.failure.reason
        is MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING
    )


@pytest.mark.unit
def test_factor_endpoint_throttle_spares_names_while_global_cooldown_blocks_both(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(
        factor_result=_RateLimitError("rate limit"),
        name_result=pd.DataFrame(
            {
                "ts_code": ["600895.SH"],
                "name": ["普通名称"],
                "start_date": ["20200101"],
                "end_date": [""],
                "change_reason": ["fixture"],
            }
        ),
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_combined_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-cooldowns",
        )
        factor = harness.factor_adapter.acquire_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 25),
            as_of_date=date(2026, 7, 30),
        )
        name = harness.name_adapter.acquire_name_events(
            instrument_identity=_identity(),
            range_start=date(2020, 1, 1),
            as_of_date=date(2026, 7, 30),
        )
        upstream_id, _ = upstream_service_identity_for_provider(
            "tushare",
            account_scope=plan.account_scope_label,
        )
        global_at = _OBSERVED_AT + timedelta(seconds=31)
        harness.coordinator.record_rate_limit(
            upstream_service_id=upstream_id,
            cooldown_scope="all",
            observed_at=global_at,
            retry_after=timedelta(seconds=30),
            provider_code="tushare_global_throttle",
        )
        factor_blocked = harness.coordinator.acquire(
            request_key="ticket-06-global-factor",
            upstream_service_id=upstream_id,
            owner_id="ticket-06-global-factor",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=global_at + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="adj_factor",
        )
        name_blocked = harness.coordinator.acquire(
            request_key="ticket-06-global-name",
            upstream_service_id=upstream_id,
            owner_id="ticket-06-global-name",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=global_at + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="namechange",
        )

    assert factor.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
    assert name.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
    assert len(client.factor_calls) == len(client.name_calls) == 1
    assert factor_blocked.disposition is LeaseDisposition.COOLDOWN
    assert name_blocked.disposition is LeaseDisposition.COOLDOWN


@pytest.mark.unit
def test_adapter_originated_upstream_throttle_blocks_the_other_endpoint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    client = _FakeTushareClient(
        factor_result=_UpstreamRateLimitError("rate limit"),
        name_result=pd.DataFrame(
            {
                "ts_code": ["600895.SH"],
                "name": ["普通名称"],
                "start_date": ["20200101"],
                "end_date": [""],
                "change_reason": ["fixture"],
            }
        ),
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_combined_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id="ticket-06-adapter-global-cooldown",
        )
        factor = harness.factor_adapter.acquire_adjustment_factors(
            instrument_identity=_identity(),
            range_start=date(2026, 7, 25),
            as_of_date=date(2026, 7, 30),
        )
        name = harness.name_adapter.acquire_name_events(
            instrument_identity=_identity(),
            range_start=date(2020, 1, 1),
            as_of_date=date(2026, 7, 30),
        )

    assert factor.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
    assert name.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
    assert len(factor.attempt_events) == 1
    assert name.attempt_events == ()
    assert len(client.factor_calls) == 1
    assert client.name_calls == []


@pytest.mark.unit
def test_empty_tushare_suspension_fixture_creates_no_status_or_tradeability_fact() -> None:
    supplemental_status = normalize_partition(Dataset.SUSPEND_D, pd.DataFrame())
    status_projections = tuple(supplemental_status.itertuples(index=False))
    projected_authoritative_status = (
        status_projections[0] if status_projections else None
    )
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-30"]),
            "Open": [10.0],
            "High": [10.1],
            "Low": [9.9],
            "Close": [10.0],
            "Volume": [100],
        }
    )

    assert status_projections == ()
    for unsupported_claim in ("tradeable", "suspended"):
        with pytest.raises(
            AuthoritativeTradingStatusValidationError,
            match="requires status provenance",
        ):
            AuthoritativeMarketSnapshot(
                symbol="600895.SS",
                frame=frame,
                provider="tushare",
                retrieved_at="2026-07-30T12:00:00Z",
                adjustment_basis="qfq",
                requested_date="2026-07-30",
                effective_trading_date="2026-07-30",
                current_tradeability=unsupported_claim,
                current_status_provenance=projected_authoritative_status,
            )

    snapshot = AuthoritativeMarketSnapshot(
        symbol="600895.SS",
        frame=frame,
        provider="legacy-provider",
        retrieved_at="2026-07-30T12:00:00Z",
        adjustment_basis="qfq",
        requested_date="2026-07-30",
        effective_trading_date="2026-07-30",
    )

    evidence = build_evidence_state(
        symbol="600895.SS",
        identity={},
        snapshot=snapshot,
    )

    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.current_tradeability == "unknown"
    assert evidence.market_snapshot.current_status_provenance is None


@pytest.mark.unit
@pytest.mark.parametrize("endpoint", ["factor", "name"])
def test_normalized_artifact_and_revision_identities_ignore_provider_row_order(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    plan = _qualified_plan(monkeypatch)
    factor_rows = pd.DataFrame(
        {
            "ts_code": ["600895.SH", "600895.SH"],
            "trade_date": ["20260725", "20260728"],
            "adj_factor": ["1.2000", "1.2500"],
        }
    )
    name_rows = pd.DataFrame(
        {
            "ts_code": ["600895.SH", "600895.SH"],
            "name": ["旧名称", "新名称"],
            "start_date": ["20200101", "20210101"],
            "end_date": ["20201231", ""],
            "change_reason": ["initial", "renamed"],
        }
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)

        def acquire(run_scope_id: str, reverse: bool):
            factor_frame = factor_rows.iloc[::-1] if reverse else factor_rows
            name_frame = name_rows.iloc[::-1] if reverse else name_rows
            client = _FakeTushareClient(
                factor_result=factor_frame.reset_index(drop=True),
                name_result=name_frame.reset_index(drop=True),
            )
            harness = _build_combined_harness(
                plan=plan,
                client=client,
                store=store,
                artifact_store=artifact_store,
                run_scope_id=run_scope_id,
                coordinator=coordinator,
            )
            if endpoint == "factor":
                return harness.factor_adapter.acquire_adjustment_factors(
                    instrument_identity=_identity(),
                    range_start=date(2026, 7, 25),
                    as_of_date=date(2026, 7, 30),
                )
            return harness.name_adapter.acquire_name_events(
                instrument_identity=_identity(),
                range_start=date(2020, 1, 1),
                as_of_date=date(2026, 7, 30),
            )

        first = acquire(f"ticket-06-{endpoint}-order-one", False)
        second = acquire(f"ticket-06-{endpoint}-order-two", True)

    assert first.provider_artifact != second.provider_artifact
    assert first.artifact is not None and second.artifact is not None
    assert first.artifact.raw_artifact == first.provider_artifact
    assert second.artifact.raw_artifact == second.provider_artifact
    assert first.artifact.raw_artifact_identity != second.artifact.raw_artifact_identity
    assert first.artifact.artifact_identity == second.artifact.artifact_identity
    assert {item.row_artifact_identity for item in first.row_artifacts} == {
        item.row_artifact_identity for item in second.row_artifacts
    }
    assert {item.revision_identity for item in first.candidates} == {
        item.revision_identity for item in second.candidates
    }
    assert {item.raw_artifact for item in first.candidates} == {
        first.provider_artifact
    }
    assert {item.raw_artifact for item in second.candidates} == {
        second.provider_artifact
    }


@pytest.mark.unit
@pytest.mark.parametrize("endpoint", ["factor", "name"])
def test_credential_like_provider_data_is_rejected_before_artifact_install(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    plan = _qualified_plan(monkeypatch)
    unsafe_value = "https://provider.invalid/query?token=do-not-persist"
    factor_frame = pd.DataFrame(
        {
            "ts_code": ["600895.SH"],
            "trade_date": ["20260725"],
            "adj_factor": ["1.2000"],
            "api_token": [unsafe_value],
        }
    )
    name_frame = pd.DataFrame(
        {
            "ts_code": ["600895.SH"],
            "name": ["普通名称"],
            "start_date": ["20200101"],
            "end_date": [""],
            "change_reason": [unsafe_value],
        }
    )
    client = _FakeTushareClient(
        factor_result=factor_frame,
        name_result=name_frame,
    )
    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        harness = _build_combined_harness(
            plan=plan,
            client=client,
            store=store,
            artifact_store=artifact_store,
            run_scope_id=f"ticket-06-{endpoint}-unsafe-provider-data",
        )
        if endpoint == "factor":
            result = harness.factor_adapter.acquire_adjustment_factors(
                instrument_identity=_identity(),
                range_start=date(2026, 7, 25),
                as_of_date=date(2026, 7, 30),
            )
        else:
            result = harness.name_adapter.acquire_name_events(
                instrument_identity=_identity(),
                range_start=date(2020, 1, 1),
                as_of_date=date(2026, 7, 30),
            )

    assert result.outcome.kind is ProviderSubrequestOutcomeKind.MALFORMED
    assert result.provider_artifact is None
    persisted = b"".join(
        path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()
    ).lower()
    assert b"do-not-persist" not in persisted
    assert b"provider.invalid" not in persisted
    assert b"token=" not in persisted

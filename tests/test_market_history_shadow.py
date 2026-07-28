from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.market_snapshot as market_snapshot
import tradingagents.market_history.shadow as shadow_module
from tradingagents.dataflows.baostock_data import BaoStockSnapshotHistoryCandidate
from tradingagents.dataflows.errors import NoMarketDataError, VendorRateLimitError
from tradingagents.dataflows.market_snapshot import (
    AuthoritativeTradingStatusValidationError,
    SnapshotProvider,
    authoritative_snapshot_run,
    get_active_market_snapshot_acquisition_record,
    get_authoritative_market_snapshot,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import AcquisitionUnavailableReason, build_evidence_state
from tradingagents.evidence_artifacts import (
    SourceArtifactManifest,
    project_evidence_for_audit,
)
from tradingagents.market_history import (
    DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS,
    AdjustmentFactorObservation,
    InstrumentSpec,
    LeaseDisposition,
    MarketHistoryConfig,
    MarketHistoryStore,
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
    ProvenanceClass,
    ProviderDatasetSpec,
    ProviderHistoryBundlePublication,
    ProviderRequestCoordinator,
    RawMarketObservation,
    RequestPriority,
    SnapshotEquivalenceSubject,
    SnapshotPurpose,
    TradingStatus,
    TradingStatusObservation,
    TradingStatusProvenance,
    compare_snapshot_equivalence,
    upstream_service_identity_for_provider,
)


def _suspended_baostock_candidate() -> BaoStockSnapshotHistoryCandidate:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    provider = ProviderDatasetSpec(
        upstream_service_id="upstream:baostock-tcp",
        upstream_service_name="BaoStock TCP service",
        provider_dataset_id="provider:baostock-strict-v1",
        provider_name="baostock",
        dataset_name="mainland-raw-status-factors-v1",
        adjustment_methodology="baostock-fore-factor-v1",
        strict_history_qualified=True,
    )
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-23", "2026-07-24"]),
            "Open": [10.0, 10.25],
            "High": [11.0, 10.25],
            "Low": [9.0, 10.25],
            "Close": [10.25, 10.25],
            "Volume": [100, 0],
        }
    )
    bundle = ProviderHistoryBundlePublication(
        provider=provider,
        instrument=InstrumentSpec(
            instrument_id="instrument:600519.SS",
            canonical_symbol="600519.SS",
            reference_market="mainland-cn",
            instrument_kind="equity",
            currency="CNY",
            identity_revision="registry:mainland-v1",
        ),
        requested_as_of=date(2026, 7, 24),
        retrieval_cutoff=observed_at,
        observed_at=observed_at,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"provider":"baostock","suspended":true}',
        observations=(
            RawMarketObservation(
                date(2026, 7, 23),
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.25"),
                Decimal("100"),
            ),
            RawMarketObservation(
                date(2026, 7, 24),
                Decimal("10.25"),
                Decimal("10.25"),
                Decimal("10.25"),
                Decimal("10.25"),
                Decimal("0"),
            ),
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 23), TradingStatus.TRADED),
            TradingStatusObservation(
                date(2026, 7, 24),
                TradingStatus.SUSPENDED,
                official_carried_close=Decimal("10.25"),
                volume=Decimal("0"),
            ),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 23), Decimal("1")),
        ),
    )
    calendar = MarketSessionCalendarPublication(
        provider=provider,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        observed_at=observed_at,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"calendar":"2026-07-23/24"}',
        sessions=(
            MarketSession(date(2026, 7, 23), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 7, 24), MarketSessionStatus.OPEN),
        ),
    )
    return BaoStockSnapshotHistoryCandidate(
        frame=frame,
        history_bundle=bundle,
        calendar=calendar,
        current_tradeability="suspended",
        current_status_provenance=TradingStatusProvenance(
            provider="baostock",
            provider_dataset_id=provider.provider_dataset_id,
            session_date=date(2026, 7, 24),
            status=TradingStatus.SUSPENDED,
            observed_at=observed_at,
        ),
        latest_traded_close=Decimal("10.25"),
        latest_traded_close_diagnostic=None,
        carried_suspension_close=Decimal("10.25"),
    )


@pytest.mark.unit
def test_mainland_live_snapshot_is_written_to_shadow_without_changing_authority(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-23", "2026-07-24"]),
            "Open": [10.0, 10.5],
            "High": [11.0, 11.5],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.0],
            "Volume": [100, 120],
        }
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(lambda *_args: frame, "qfq")},
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-23",
        "2026-07-24",
    )

    with MarketHistoryStore.open(
        MarketHistoryConfig.from_mapping(config_module.get_config())
    ) as store:
        stored = store.get_provider_frame(snapshot.snapshot_id)
        payload = store.read_payload(stored.payload_digest)
        strict_history_qualified = store._connection.execute(
            "SELECT strict_history_qualified FROM provider_datasets "
            "WHERE provider_dataset_id = ?",
            (stored.provider_dataset_id,),
        ).fetchone()[0]

    assert snapshot.provider == "only"
    assert snapshot.adjustment_basis == "qfq"
    assert snapshot.frame_sha256 == stored.frame_digest
    assert stored.current_only is True
    assert stored.canonical_symbol == "600519.SS"
    assert strict_history_qualified == 0
    assert payload.decode("utf-8") == snapshot.source_artifact.raw_text


@pytest.mark.unit
@pytest.mark.parametrize("history_mode", ("disabled", "shadow"))
def test_non_authoritative_history_modes_skip_optional_strict_history_acquisition(
    monkeypatch,
    tmp_path,
    history_mode,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": history_mode,
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-24"]),
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [100],
        }
    )
    calls = []

    def load_current(*_args):
        calls.append("current")
        return frame

    def load_history(*_args):
        calls.append("history")
        return frame

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(load_current, "qfq", history_load=load_history)},
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    assert snapshot.provider == "only"
    assert calls == ["current"]


@pytest.mark.unit
def test_authoritative_live_fallback_rejects_provider_with_exact_range_gap(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "primary,secondary"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(
        MarketHistoryStore,
        "mainland_cutover_ready",
        lambda _store: True,
    )
    session_dates = tuple(
        item.date() for item in pd.bdate_range("2026-06-25", periods=22)
    )
    calendar_provider = ProviderDatasetSpec(
        upstream_service_id="upstream:calendar",
        upstream_service_name="Calendar",
        provider_dataset_id="provider:calendar",
        provider_name="calendar",
        dataset_name="mainland-session-calendar-v1",
        adjustment_methodology="not-applicable",
        strict_history_qualified=False,
    )
    with MarketHistoryStore.open(
        MarketHistoryConfig.from_mapping(runtime_config)
    ) as store:
        store.publish_session_calendar(
            MarketSessionCalendarPublication(
                provider=calendar_provider,
                reference_market="mainland-cn",
                timezone_name="Asia/Shanghai",
                observed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"calendar":"gap-test"}',
                sessions=tuple(
                    MarketSession(day, MarketSessionStatus.OPEN)
                    for day in session_dates
                ),
            )
        )

    def frame_for(dates):
        rows = len(dates)
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(dates),
                "Open": [10.0] * rows,
                "High": [11.0] * rows,
                "Low": [9.0] * rows,
                "Close": [10.5] * rows,
                "Volume": [100] * rows,
            }
        )

    calls = []

    def primary(*_args):
        calls.append("primary")
        return frame_for(session_dates[:10] + session_dates[11:])

    def secondary(*_args):
        calls.append("secondary")
        return frame_for(session_dates[1:])

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "primary": SnapshotProvider(primary, "qfq"),
            "secondary": SnapshotProvider(secondary, "qfq"),
        },
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        session_dates[0].isoformat(),
        session_dates[-1].isoformat(),
        minimum_history_rows=21,
    )

    assert snapshot.provider == "secondary"
    assert snapshot.history_gap_dates == ()
    assert calls == ["primary", "secondary"]


@pytest.mark.unit
def test_selected_baostock_candidate_publishes_complete_history_workflow(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "baostock,yfinance"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    candidate = _suspended_baostock_candidate()
    persisted = []
    provider_calls = []

    def baostock(*_args):
        provider_calls.append("baostock")
        return candidate

    def yfinance(*_args):
        provider_calls.append("yfinance")
        raise AssertionError("a complete BaoStock candidate must stop fallback")

    def record_candidate(value):
        persisted.append(value)
        return shadow_module.HistoryBundleWriteResult(True, True)

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": SnapshotProvider(baostock, "qfq"),
            "yfinance": SnapshotProvider(yfinance, "auto_adjusted"),
        },
    )
    monkeypatch.setattr(
        shadow_module,
        "persist_mainland_history_candidate",
        record_candidate,
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS", "2026-07-23", "2026-07-24"
    )

    assert snapshot.provider == "baostock"
    assert persisted == [candidate]
    assert provider_calls == ["baostock"]
    assert snapshot.current_tradeability == "suspended"
    assert snapshot.current_status_provenance == candidate.current_status_provenance
    assert snapshot.latest_traded_close == Decimal("10.25")
    assert snapshot.carried_suspension_close == Decimal("10.25")
    assert snapshot.frame.iloc[-1]["Close"] == 10.25
    assert snapshot.frame.iloc[-1]["Volume"] == 0


@pytest.mark.unit
def test_authoritative_traded_baostock_status_remains_tradeable(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "baostock,yfinance"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    suspended = _suspended_baostock_candidate()
    latest_observation = RawMarketObservation(
        date(2026, 7, 24),
        Decimal("10.25"),
        Decimal("11"),
        Decimal("10"),
        Decimal("10.75"),
        Decimal("120"),
    )
    traded = replace(
        suspended,
        frame=pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-23", "2026-07-24"]),
                "Open": [10.0, 10.25],
                "High": [11.0, 11.0],
                "Low": [9.0, 10.0],
                "Close": [10.25, 10.75],
                "Volume": [100, 120],
            }
        ),
        history_bundle=replace(
            suspended.history_bundle,
            observations=(
                suspended.history_bundle.observations[0],
                latest_observation,
            ),
            trading_statuses=(
                suspended.history_bundle.trading_statuses[0],
                TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
            ),
        ),
        current_tradeability="tradeable",
        current_status_provenance=replace(
            suspended.current_status_provenance,
            status=TradingStatus.TRADED,
        ),
        latest_traded_close=Decimal("10.75"),
        carried_suspension_close=None,
    )
    calls = []

    def baostock(*_args):
        calls.append("baostock")
        return traded

    def yfinance(*_args):
        calls.append("yfinance")
        raise AssertionError("a complete traded BaoStock candidate must stop fallback")

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": SnapshotProvider(baostock, "qfq"),
            "yfinance": SnapshotProvider(yfinance, "auto_adjusted"),
        },
    )
    monkeypatch.setattr(
        shadow_module,
        "persist_mainland_history_candidate",
        lambda _candidate: shadow_module.HistoryBundleWriteResult(True, True),
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS", "2026-07-23", "2026-07-24"
    )

    assert calls == ["baostock"]
    assert snapshot.current_tradeability == "tradeable"
    assert snapshot.current_status_provenance is not None
    assert snapshot.current_status_provenance.status is TradingStatus.TRADED
    assert snapshot.latest_traded_close == Decimal("10.75")
    assert snapshot.carried_suspension_close is None
    assert len(snapshot.frame) == 2


@pytest.mark.unit
def test_baostock_status_survives_history_store_degradation(monkeypatch, tmp_path) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "baostock"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    candidate = _suspended_baostock_candidate()
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"baostock": SnapshotProvider(lambda *_args: candidate, "qfq")},
    )
    monkeypatch.setattr(
        shadow_module,
        "persist_mainland_history_candidate",
        lambda _candidate: shadow_module.HistoryBundleWriteResult(
            True,
            False,
            diagnostic="history_bundle_write_failed:disk_full",
        ),
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS", "2026-07-23", "2026-07-24"
    )

    assert snapshot.history_store_status == "degraded"
    assert snapshot.current_tradeability == "suspended"
    assert snapshot.current_status_provenance == candidate.current_status_provenance
    assert snapshot.latest_traded_close == Decimal("10.25")
    evidence = build_evidence_state(
        symbol="600519.SS",
        identity={},
        snapshot=snapshot,
    )
    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.current_status_provenance is not None
    assert audit.market_snapshot is not None
    assert (
        audit.market_snapshot.current_status_provenance.model_dump(mode="python")
        == evidence.market_snapshot.current_status_provenance.model_dump(mode="python")
    )
    assert audit.market_snapshot.current_tradeability == "suspended"
    assert audit.market_snapshot.latest_traded_close == Decimal("10.25")
    assert audit.market_snapshot.carried_suspension_close == Decimal("10.25")


@pytest.mark.unit
def test_selected_baostock_status_must_cover_latest_open_calendar_session(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "baostock,yfinance"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    complete = _suspended_baostock_candidate()
    stale_status = replace(
        complete,
        calendar=replace(
            complete.calendar,
            sessions=(
                *complete.calendar.sessions,
                MarketSession(date(2026, 7, 25), MarketSessionStatus.OPEN),
            ),
        ),
    )
    calls = []

    def baostock(*_args):
        calls.append("baostock")
        return stale_status

    def yfinance(*_args):
        calls.append("yfinance")
        return stale_status.frame

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": SnapshotProvider(baostock, "qfq"),
            "yfinance": SnapshotProvider(yfinance, "auto_adjusted"),
        },
    )

    with pytest.raises(
        AuthoritativeTradingStatusValidationError,
        match="latest applicable mainland session",
    ):
        get_authoritative_market_snapshot(
            "600519.SS", "2026-07-23", "2026-07-25"
        )

    assert calls == ["baostock"]


@pytest.mark.unit
def test_contradictory_selected_baostock_status_fails_closed_without_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "baostock,yfinance"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    contradictory = replace(
        _suspended_baostock_candidate(),
        current_tradeability="tradeable",
    )
    calls = []

    def baostock(*_args):
        calls.append("baostock")
        return contradictory

    def yfinance(*_args):
        calls.append("yfinance")
        return contradictory.frame

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": SnapshotProvider(baostock, "qfq"),
            "yfinance": SnapshotProvider(yfinance, "auto_adjusted"),
        },
    )

    with pytest.raises(
        AuthoritativeTradingStatusValidationError,
        match="tradeability contradicts",
    ):
        get_authoritative_market_snapshot(
            "600519.SS", "2026-07-23", "2026-07-24"
        )

    assert calls == ["baostock"]


@pytest.mark.unit
def test_selected_baostock_candidate_with_dropped_status_fails_closed(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "baostock"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    complete = _suspended_baostock_candidate()
    dropped = SimpleNamespace(
        frame=complete.frame,
        history_bundle=complete.history_bundle,
        calendar=complete.calendar,
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"baostock": SnapshotProvider(lambda *_args: dropped, "qfq")},
    )

    with pytest.raises(
        AuthoritativeTradingStatusValidationError,
        match="dropped authoritative status evidence",
    ):
        get_authoritative_market_snapshot(
            "600519.SS", "2026-07-23", "2026-07-24"
        )


@pytest.mark.unit
def test_history_candidate_seed_and_refresh_are_durable_and_replay_qualified(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    provider = ProviderDatasetSpec(
        upstream_service_id="upstream:baostock-tcp",
        upstream_service_name="BaoStock TCP service",
        provider_dataset_id="provider:baostock-strict-v1",
        provider_name="baostock",
        dataset_name="mainland-raw-status-factors-v1",
        adjustment_methodology="baostock-fore-factor-v1",
        strict_history_qualified=True,
    )
    bundle = ProviderHistoryBundlePublication(
        provider=provider,
        instrument=InstrumentSpec(
            instrument_id="instrument:600519.SS",
            canonical_symbol="600519.SS",
            reference_market="mainland-cn",
            instrument_kind="equity",
            currency="CNY",
            identity_revision="registry:mainland-v1",
        ),
        requested_as_of=date(2026, 7, 24),
        retrieval_cutoff=observed_at,
        observed_at=observed_at,
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=b'{"seed":true}',
        observations=(
            RawMarketObservation(
                date(2026, 7, 24),
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.5"),
                Decimal("100"),
            ),
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 24), Decimal("1")),
        ),
    )
    calendar = MarketSessionCalendarPublication(
        provider=provider,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        observed_at=observed_at,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"calendar":"2026-07-24"}',
        sessions=(MarketSession(date(2026, 7, 24), MarketSessionStatus.OPEN),),
    )
    seed = shadow_module.persist_mainland_history_candidate(
        SimpleNamespace(history_bundle=bundle, calendar=calendar)
    )
    refreshed_at = observed_at.replace(hour=11)
    refresh = shadow_module.persist_mainland_history_candidate(
        SimpleNamespace(
            history_bundle=replace(
                bundle,
                retrieval_cutoff=refreshed_at,
                observed_at=refreshed_at,
                raw_payload=b'{"refresh":true}',
            ),
            calendar=replace(
                calendar,
                observed_at=refreshed_at,
                raw_payload=b'{"calendar":"refreshed"}',
            ),
        )
    )

    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open(config) as store:
        latest = store.latest_qualified_history_bundle("600519.SS", "2026-07-24")
        reconstructed = store.reconstruct_snapshot(
            latest.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=refreshed_at,
        )

    assert seed.persisted is True
    assert refresh.persisted is True
    assert reconstructed.provenance_class is ProvenanceClass.OBSERVED_POINT_IN_TIME


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "seed_provenance",
        "omit_seeded_history",
        "prove_authoritative_availability",
        "include_all_retained_dates",
        "expected_provenance",
    ),
    (
        (
            ProvenanceClass.RETROSPECTIVE_BACKFILL,
            False,
            False,
            False,
            ProvenanceClass.RETROSPECTIVE_BACKFILL,
        ),
        (
            ProvenanceClass.OBSERVED_POINT_IN_TIME,
            False,
            False,
            False,
            ProvenanceClass.OBSERVED_POINT_IN_TIME,
        ),
        (
            ProvenanceClass.OBSERVED_POINT_IN_TIME,
            False,
            False,
            True,
            ProvenanceClass.OBSERVED_POINT_IN_TIME,
        ),
        (
            ProvenanceClass.OBSERVED_POINT_IN_TIME,
            True,
            False,
            False,
            ProvenanceClass.RETROSPECTIVE_BACKFILL,
        ),
        (
            ProvenanceClass.OBSERVED_POINT_IN_TIME,
            True,
            True,
            False,
            ProvenanceClass.OBSERVED_POINT_IN_TIME,
        ),
    ),
)
def test_incremental_history_refresh_preserves_complete_bundle_and_calendar(
    monkeypatch,
    tmp_path,
    seed_provenance,
    omit_seeded_history,
    prove_authoritative_availability,
    include_all_retained_dates,
    expected_provenance,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    session_dates = tuple(
        item.date() for item in pd.bdate_range("2026-06-15", periods=30)
    )
    observed_at = datetime(2026, 7, 24, 10, tzinfo=timezone.utc)
    provider = ProviderDatasetSpec(
        upstream_service_id="upstream:baostock",
        upstream_service_name="BaoStock",
        provider_dataset_id="provider:baostock-history",
        provider_name="baostock",
        dataset_name="mainland-raw-status-factors-v1",
        adjustment_methodology="baostock-fore-factor-v1",
        strict_history_qualified=True,
    )
    instrument = InstrumentSpec(
        instrument_id="instrument:600519.SS",
        canonical_symbol="600519.SS",
        reference_market="mainland-cn",
        instrument_kind="equity",
        currency="CNY",
        identity_revision="registry:mainland-v1",
    )

    def bundle(dates, *, at, payload, authoritative_available_at=None):
        return ProviderHistoryBundlePublication(
            provider=provider,
            instrument=instrument,
            requested_as_of=session_dates[-1],
            retrieval_cutoff=at,
            observed_at=at,
            provenance_class=seed_provenance,
            raw_payload=payload,
            observations=tuple(
                RawMarketObservation(
                    day,
                    Decimal("10"),
                    Decimal("11"),
                    Decimal("9"),
                    Decimal("10.5"),
                    Decimal("100"),
                    provider_available_at=authoritative_available_at,
                )
                for day in dates
            ),
            trading_statuses=tuple(
                TradingStatusObservation(
                    day,
                    TradingStatus.TRADED,
                    provider_available_at=authoritative_available_at,
                )
                for day in dates
            ),
            adjustment_factors=(
                AdjustmentFactorObservation(
                    session_dates[0],
                    Decimal("1"),
                    provider_available_at=authoritative_available_at,
                ),
            ),
        )

    def calendar(dates, *, at, payload):
        return MarketSessionCalendarPublication(
            provider=replace(
                provider,
                provider_dataset_id="provider:calendar",
                dataset_name="mainland-session-calendar-v1",
                adjustment_methodology="not-applicable",
                strict_history_qualified=False,
            ),
            reference_market="mainland-cn",
            timezone_name="Asia/Shanghai",
            observed_at=at,
            provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
            raw_payload=payload,
            sessions=tuple(
                MarketSession(day, MarketSessionStatus.OPEN) for day in dates
            ),
        )

    seed = shadow_module.persist_mainland_history_candidate(
        SimpleNamespace(
            history_bundle=bundle(
                tuple(
                    day
                    for index, day in enumerate(session_dates[:25])
                    if not omit_seeded_history or index != 15
                ),
                at=observed_at,
                payload=b'{"seed":true}',
            ),
            calendar=calendar(
                session_dates,
                at=observed_at,
                payload=b'{"calendar":"seed"}',
            ),
        )
    )
    refreshed_at = observed_at.replace(day=25)
    refresh = shadow_module.persist_mainland_history_candidate(
        SimpleNamespace(
            history_bundle=bundle(
                (
                    session_dates
                    if include_all_retained_dates
                    else session_dates[-21:]
                ),
                at=refreshed_at,
                payload=b'{"refresh":true}',
                authoritative_available_at=(
                    observed_at if prove_authoritative_availability else None
                ),
            ),
            calendar=calendar(
                session_dates[-21:],
                at=refreshed_at,
                payload=b'{"calendar":"refresh"}',
            ),
        )
    )

    with MarketHistoryStore.open(
        MarketHistoryConfig.from_mapping(runtime_config)
    ) as store:
        latest = store.latest_qualified_history_bundle(
            "600519.SS",
            session_dates[-1].isoformat(),
        )
        reconstructed = store.reconstruct_snapshot(
            latest.bundle_revision_id,
            requested_date=session_dates[-1],
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        stored_calendar = store.read_current_session_calendar("mainland-cn")

    assert seed.persisted is True
    assert refresh.persisted is True
    assert tuple(reconstructed.frame["Date"].dt.date) == session_dates
    assert tuple(item.session_date for item in stored_calendar.sessions) == session_dates
    assert reconstructed.provenance_class is expected_provenance


@pytest.mark.unit
def test_authoritative_history_refresh_requests_missing_dates_plus_overlap(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "baostock"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    monkeypatch.setattr(
        MarketHistoryStore,
        "mainland_cutover_ready",
        lambda _store: True,
    )
    session_dates = tuple(
        item.date() for item in pd.bdate_range("2026-06-15", periods=30)
    )
    observed_at = datetime(2026, 7, 24, 10, tzinfo=timezone.utc)
    history_provider = ProviderDatasetSpec(
        upstream_service_id="upstream:baostock",
        upstream_service_name="BaoStock",
        provider_dataset_id="provider:baostock-history",
        provider_name="baostock",
        dataset_name="mainland-raw-status-factors-v1",
        adjustment_methodology="baostock-fore-factor-v1",
        strict_history_qualified=True,
    )
    instrument = InstrumentSpec(
        instrument_id="instrument:600519.SS",
        canonical_symbol="600519.SS",
        reference_market="mainland-cn",
        instrument_kind="equity",
        currency="CNY",
        identity_revision="registry:mainland-v1",
    )
    with MarketHistoryStore.open(
        MarketHistoryConfig.from_mapping(runtime_config)
    ) as store:
        store.publish_session_calendar(
            MarketSessionCalendarPublication(
                provider=replace(
                    history_provider,
                    provider_dataset_id="provider:calendar",
                    dataset_name="mainland-session-calendar-v1",
                    adjustment_methodology="not-applicable",
                    strict_history_qualified=False,
                ),
                reference_market="mainland-cn",
                timezone_name="Asia/Shanghai",
                observed_at=observed_at,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"calendar":"seed"}',
                sessions=tuple(
                    MarketSession(day, MarketSessionStatus.OPEN)
                    for day in session_dates
                ),
            )
        )
        store.publish_history_bundle(
            ProviderHistoryBundlePublication(
                provider=history_provider,
                instrument=instrument,
                requested_as_of=session_dates[-1],
                retrieval_cutoff=observed_at,
                observed_at=observed_at,
                provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
                raw_payload=b'{"seed":true}',
                observations=tuple(
                    RawMarketObservation(
                        day,
                        Decimal("10"),
                        Decimal("11"),
                        Decimal("9"),
                        Decimal("10.5"),
                        Decimal("100"),
                    )
                    for day in session_dates[:25]
                ),
                trading_statuses=tuple(
                    TradingStatusObservation(day, TradingStatus.TRADED)
                    for day in session_dates[:25]
                ),
                adjustment_factors=(
                    AdjustmentFactorObservation(session_dates[0], Decimal("1")),
                ),
            )
        )

    def frame_for(dates):
        rows = len(dates)
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(dates),
                "Open": [10.0] * rows,
                "High": [11.0] * rows,
                "Low": [9.0] * rows,
                "Close": [10.5] * rows,
                "Volume": [100] * rows,
            }
        )

    history_calls = []

    def load_history(_symbol, start_date, end_date):
        history_calls.append((start_date, end_date))
        return frame_for(session_dates[-21:])

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": SnapshotProvider(
                lambda *_args: pytest.fail("current loader should not replace refresh"),
                "qfq",
                history_load=load_history,
            )
        },
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        session_dates[0].isoformat(),
        session_dates[-1].isoformat(),
        minimum_history_rows=21,
    )

    assert snapshot.provider == "baostock"
    assert history_calls == [
        (session_dates[-21].isoformat(), session_dates[-1].isoformat())
    ]


@pytest.mark.unit
def test_shadow_store_failure_is_observable_but_live_snapshot_still_returns(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    unusable_database_path = tmp_path / "database-is-a-directory"
    unusable_database_path.mkdir()
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(unusable_database_path),
            "market_history_payload_root": str(tmp_path / "payloads"),
            "market_history_backup_root": str(tmp_path / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-24"]),
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [100],
        }
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(lambda *_args: frame, "qfq")},
    )

    with caplog.at_level("WARNING"):
        snapshot = get_authoritative_market_snapshot(
            "600519.SS",
            "2026-07-24",
            "2026-07-24",
        )

    assert snapshot.provider == "only"
    assert snapshot.frame["Close"].tolist() == [10.5]
    assert snapshot.history_store_status == "degraded"
    assert snapshot.history_store_diagnostic is not None
    assert snapshot.history_store_diagnostic.startswith("shadow_write_failed:")
    assert "Market history shadow write failed" in caplog.text


@pytest.mark.unit
def test_production_usage_mode_fails_research_providers_closed(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "data_usage_mode": "production",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "akshare,baostock,yfinance"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    calls = 0

    def must_not_call(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("research-only provider was called in production mode")

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            name: SnapshotProvider(must_not_call, "qfq")
            for name in ("akshare", "baostock", "yfinance")
        },
    )

    with authoritative_snapshot_run():
        with pytest.raises(NoMarketDataError):
            get_authoritative_market_snapshot(
                "600519.SS", "2026-07-24", "2026-07-24"
            )
        record = get_active_market_snapshot_acquisition_record(
            "600519.SS", "2026-07-24"
        )

    assert calls == 0
    assert record is not None
    assert {outcome.reason for outcome in record.outcomes} == {
        AcquisitionUnavailableReason.USAGE_NOT_ENTITLED
    }


@pytest.mark.unit
def test_authoritative_mode_never_promotes_current_only_adjusted_frame(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-23", "2026-07-24"]),
            "Open": [10.0, 10.5],
            "High": [11.0, 11.5],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.0],
            "Volume": [100, 120],
        }
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(lambda *_args: frame, "qfq")},
    )
    live = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-23",
        "2026-07-24",
    )
    subject = SnapshotEquivalenceSubject(
        instrument_identity_id="registry:mainland-v1",
        provider_dataset_id="provider:only",
        adjustment_basis="qfq",
        effective_dates=("2026-07-23", "2026-07-24"),
        eligible_observation_ids=("row-1", "row-2"),
        frame_sha256=live.frame_sha256,
        derived_fact_digests=("facts",),
        lineage_digests=("lineage",),
    )
    comparison = compare_snapshot_equivalence(subject, subject)
    config = MarketHistoryConfig.from_mapping(config_module.get_config())
    with MarketHistoryStore.open(config) as store:
        stored_frame = store.get_provider_frame(live.snapshot_id)
        for scenario in DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS:
            store.record_mainland_equivalence(
                scenario,
                comparison,
                frame_revision_id=stored_frame.frame_revision_id,
            )
        assert store.mainland_cutover_ready() is True

    config_module.set_config({"market_history_mode": "authoritative"})

    provider_calls = 0

    def complete_live_frame(*_args):
        nonlocal provider_calls
        provider_calls += 1
        return frame.assign(Close=[10.75, 11.25], High=[11.25, 11.75])

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(complete_live_frame, "qfq")},
    )

    stored = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-23",
        "2026-07-24",
    )

    assert provider_calls == 1
    assert stored.provider == live.provider
    assert stored.frame_sha256 != live.frame_sha256
    assert stored.history_store_status == "degraded"
    assert stored.history_store_diagnostic == "qualified_history_bundle_unavailable"


@pytest.mark.unit
def test_authoritative_mode_reads_complete_strict_bundle_and_surfaces_suspension(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    provider = ProviderDatasetSpec(
        upstream_service_id="upstream:baostock-tcp",
        upstream_service_name="BaoStock TCP service",
        provider_dataset_id="provider:baostock-strict-v1",
        provider_name="baostock",
        dataset_name="mainland-raw-status-factors-v1",
        adjustment_methodology="baostock-fore-factor-v1",
        strict_history_qualified=True,
    )
    with MarketHistoryStore.open(
        MarketHistoryConfig.from_mapping(runtime_config)
    ) as store:
        store.publish_history_bundle(
            ProviderHistoryBundlePublication(
                provider=provider,
                instrument=InstrumentSpec(
                    instrument_id="instrument:600519.SS",
                    canonical_symbol="600519.SS",
                    reference_market="mainland-cn",
                    instrument_kind="equity",
                    currency="CNY",
                    identity_revision="registry:mainland-v1",
                ),
                requested_as_of=date(2026, 7, 24),
                retrieval_cutoff=observed_at,
                observed_at=observed_at,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"provider":"baostock","strict":true}',
                observations=(
                    RawMarketObservation(
                        date(2026, 7, 23),
                        Decimal("10"),
                        Decimal("11"),
                        Decimal("9"),
                        Decimal("10.25"),
                        Decimal("100"),
                    ),
                    RawMarketObservation(
                        date(2026, 7, 24),
                        Decimal("10.25"),
                        Decimal("10.25"),
                        Decimal("10.25"),
                        Decimal("10.25"),
                        Decimal("0"),
                    ),
                ),
                trading_statuses=(
                    TradingStatusObservation(date(2026, 7, 23), TradingStatus.TRADED),
                    TradingStatusObservation(
                        date(2026, 7, 24),
                        TradingStatus.SUSPENDED,
                        official_carried_close=Decimal("10.25"),
                        volume=Decimal("0"),
                    ),
                ),
                adjustment_factors=(
                    AdjustmentFactorObservation(date(2026, 7, 23), Decimal("1")),
                ),
            )
        )
        store.publish_session_calendar(
            MarketSessionCalendarPublication(
                provider=provider,
                reference_market="mainland-cn",
                timezone_name="Asia/Shanghai",
                observed_at=observed_at,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"calendar":"2026-07-23/24"}',
                sessions=(
                    MarketSession(date(2026, 7, 23), MarketSessionStatus.OPEN),
                    MarketSession(date(2026, 7, 24), MarketSessionStatus.OPEN),
                ),
            )
        )
        subject = SnapshotEquivalenceSubject(
            instrument_identity_id="registry:mainland-v1",
            provider_dataset_id=provider.provider_dataset_id,
            adjustment_basis="qfq",
            effective_dates=("2026-07-23", "2026-07-24"),
            eligible_observation_ids=("row-1", "row-2"),
            frame_sha256="a" * 64,
            derived_fact_digests=("facts",),
            lineage_digests=("lineage",),
        )
        comparison = compare_snapshot_equivalence(subject, subject)
        for scenario in DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS:
            store.record_mainland_equivalence(scenario, comparison)

    def must_not_call_provider(*_args):
        raise AssertionError("qualified stored history must not call a live provider")

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(must_not_call_provider, "qfq")},
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    assert snapshot.frame["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-07-23",
        "2026-07-24",
    ]
    assert snapshot.snapshot_manifest_json is not None
    manifest = json.loads(snapshot.snapshot_manifest_json)
    assert manifest["frame"] == {"rows": 2, "sha256": snapshot.frame_sha256}
    assert [row["session_date"] for row in manifest["observations"]] == [
        "2026-07-23",
        "2026-07-24",
    ]
    assert snapshot.provider == "baostock"
    assert snapshot.history_store_status == "stored"
    assert snapshot.current_tradeability == "suspended"
    assert snapshot.latest_traded_close == Decimal("10.25")
    assert snapshot.carried_suspension_close == Decimal("10.25")
    assert snapshot.current_status_provenance is not None
    assert snapshot.current_status_provenance.provider == "baostock"
    assert snapshot.current_status_provenance.session_date == date(2026, 7, 24)
    assert snapshot.current_status_provenance.status is TradingStatus.SUSPENDED
    assert snapshot.current_status_provenance.revision_id is not None


@pytest.mark.unit
def test_authoritative_weekend_reuses_latest_retained_session_without_provider_call(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-23", "2026-07-24"]),
            "Open": [10.0, 10.5],
            "High": [11.0, 11.5],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.0],
            "Volume": [100, 120],
        }
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(lambda *_args: frame, "qfq")},
    )
    live = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-23",
        "2026-07-24",
    )
    subject = SnapshotEquivalenceSubject(
        instrument_identity_id="registry:mainland-v1",
        provider_dataset_id="provider:only",
        adjustment_basis="qfq",
        effective_dates=("2026-07-23", "2026-07-24"),
        eligible_observation_ids=("row-1", "row-2"),
        frame_sha256=live.frame_sha256,
        derived_fact_digests=("facts",),
        lineage_digests=("lineage",),
    )
    comparison = compare_snapshot_equivalence(subject, subject)
    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open(config) as store:
        stored_frame = store.get_provider_frame(live.snapshot_id)
        for scenario in DEFAULT_MAINLAND_EQUIVALENCE_SCENARIOS:
            store.record_mainland_equivalence(
                scenario,
                comparison,
                frame_revision_id=stored_frame.frame_revision_id,
            )
        observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
        strict_provider = ProviderDatasetSpec(
            upstream_service_id="upstream:baostock-tcp",
            upstream_service_name="BaoStock TCP service",
            provider_dataset_id="provider:baostock-strict-v1",
            provider_name="baostock",
            dataset_name="mainland-raw-status-factors-v1",
            adjustment_methodology="baostock-fore-factor-v1",
            strict_history_qualified=True,
        )
        store.publish_history_bundle(
            ProviderHistoryBundlePublication(
                provider=strict_provider,
                instrument=InstrumentSpec(
                    instrument_id="instrument:600519.SS",
                    canonical_symbol="600519.SS",
                    reference_market="mainland-cn",
                    instrument_kind="equity",
                    currency="CNY",
                    identity_revision="registry:mainland-v1",
                ),
                requested_as_of=date(2026, 7, 24),
                retrieval_cutoff=observed_at,
                observed_at=observed_at,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"provider":"baostock","weekend":true}',
                observations=(
                    RawMarketObservation(
                        date(2026, 7, 23),
                        Decimal("10"),
                        Decimal("11"),
                        Decimal("9"),
                        Decimal("10.5"),
                        Decimal("100"),
                    ),
                    RawMarketObservation(
                        date(2026, 7, 24),
                        Decimal("10.5"),
                        Decimal("11.5"),
                        Decimal("10"),
                        Decimal("11"),
                        Decimal("120"),
                    ),
                ),
                trading_statuses=(
                    TradingStatusObservation(date(2026, 7, 23), TradingStatus.TRADED),
                    TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
                ),
                adjustment_factors=(
                    AdjustmentFactorObservation(date(2026, 7, 23), Decimal("1")),
                ),
            )
        )
        store.publish_session_calendar(
            MarketSessionCalendarPublication(
                provider=ProviderDatasetSpec(
                    upstream_service_id="upstream:baostock-calendar",
                    upstream_service_name="BaoStock calendar service",
                    provider_dataset_id="provider:baostock-calendar-v1",
                    provider_name="baostock",
                    dataset_name="mainland-session-calendar-v1",
                    adjustment_methodology="not-applicable",
                    strict_history_qualified=False,
                ),
                reference_market="mainland-cn",
                timezone_name="Asia/Shanghai",
                observed_at=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"calendar":"2026-07-24/26"}',
                sessions=(
                    MarketSession(date(2026, 7, 23), MarketSessionStatus.OPEN),
                    MarketSession(date(2026, 7, 24), MarketSessionStatus.OPEN),
                    MarketSession(date(2026, 7, 25), MarketSessionStatus.CLOSED),
                    MarketSession(date(2026, 7, 26), MarketSessionStatus.CLOSED),
                ),
            )
        )

    runtime_config["market_history_mode"] = "authoritative"
    calls = 0

    def must_not_call(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("retained weekend history must not call a provider")

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(must_not_call, "qfq")},
    )

    weekend = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-23",
        "2026-07-26",
    )

    assert weekend.history_store_status == "stored"
    assert weekend.requested_date == "2026-07-26"
    assert weekend.effective_trading_date == "2026-07-24"
    assert weekend.snapshot_manifest_json is not None
    assert json.loads(weekend.snapshot_manifest_json)["requested_date"] == (
        weekend.requested_date
    )
    assert weekend.frame_sha256 == live.frame_sha256
    assert calls == 0


@pytest.mark.unit
def test_authoritative_mode_degrades_to_live_until_persisted_gate_passes(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-24"]),
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [100],
        }
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(lambda *_args: frame, "qfq")},
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    assert snapshot.provider == "only"
    assert snapshot.history_store_status == "degraded"
    assert snapshot.history_store_diagnostic == "mainland_gate_not_passed"


@pytest.mark.unit
def test_authoritative_mode_degrades_to_live_when_store_is_unavailable(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    unusable_database_path = tmp_path / "database-is-a-directory"
    unusable_database_path.mkdir()
    runtime_config.update(
        {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(unusable_database_path),
            "market_history_payload_root": str(tmp_path / "payloads"),
            "market_history_backup_root": str(tmp_path / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-24"]),
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [100],
        }
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(lambda *_args: frame, "qfq")},
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    assert snapshot.provider == "only"
    assert snapshot.history_store_status == "degraded"
    assert snapshot.history_store_diagnostic is not None
    assert snapshot.history_store_diagnostic.startswith("history_store_read_failed:")


@pytest.mark.unit
def test_authoritative_mode_blocks_live_probe_when_coordinator_state_is_corrupt(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    database_path = history_root / "market_history.sqlite3"
    history_root.mkdir()
    database_path.write_bytes(b"not-a-sqlite-database")
    runtime_config.update(
        {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(database_path),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "only"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    provider_calls = 0

    def complete_live_frame(*_args):
        nonlocal provider_calls
        provider_calls += 1
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [100],
            }
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(complete_live_frame, "qfq")},
    )

    with pytest.raises(NoMarketDataError, match="upstream_busy"):
        get_authoritative_market_snapshot(
            "600519.SS",
            "2026-07-24",
            "2026-07-24",
        )

    assert provider_calls == 0


@pytest.mark.unit
def test_coordinator_cooldown_skips_provider_then_falls_back_sequentially(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "akshare,baostock"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    config = MarketHistoryConfig.from_mapping(runtime_config)
    upstream_id, service_name = upstream_service_identity_for_provider("akshare")
    observed_at = pd.Timestamp("2026-07-24T10:00:00Z").to_pydatetime()
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(upstream_id, service_name)
        coordinator.record_rate_limit(
            upstream_service_id=upstream_id,
            cooldown_scope="market-snapshot",
            observed_at=observed_at,
            retry_after=pd.Timedelta(minutes=5).to_pytimedelta(),
            provider_code="HTTP_429",
        )
    calls = {"akshare": 0, "baostock": 0}

    def must_be_skipped(*_args):
        calls["akshare"] += 1
        raise AssertionError("cooling provider was called")

    def fallback(*_args):
        calls["baostock"] += 1
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [100],
            }
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": SnapshotProvider(must_be_skipped, "qfq"),
            "baostock": SnapshotProvider(fallback, "qfq"),
        },
    )
    monkeypatch.setattr(
        market_snapshot,
        "_coordinator_now",
        lambda: observed_at + pd.Timedelta(minutes=1).to_pytimedelta(),
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    assert snapshot.provider == "baostock"
    assert calls == {"akshare": 0, "baostock": 1}
    assert snapshot.acquisition_outcomes[0].reason is AcquisitionUnavailableReason.RATE_LIMITED


@pytest.mark.unit
def test_operator_pacing_skips_physical_provider_call_with_retry_hint(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "akshare,baostock"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    config = MarketHistoryConfig.from_mapping(runtime_config)
    upstream_id, service_name = upstream_service_identity_for_provider("akshare")
    observed_at = pd.Timestamp("2026-07-24T10:00:00Z").to_pydatetime()
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(upstream_id, service_name)
        coordinator.configure_operator_ceiling(
            upstream_service_id=upstream_id,
            minimum_interval=pd.Timedelta(seconds=10).to_pytimedelta(),
            allow_prewarming=False,
            configured_at=observed_at,
        )
        decision = coordinator.acquire(
            request_key="prior:akshare",
            upstream_service_id=upstream_id,
            owner_id="prior-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=observed_at,
            lease_duration=pd.Timedelta(seconds=30).to_pytimedelta(),
        )
        coordinator.record_physical_attempt(decision.lease, occurred_at=observed_at)
        coordinator.release(decision.lease)
    calls = {"akshare": 0, "baostock": 0}

    def must_be_skipped(*_args):
        calls["akshare"] += 1
        raise AssertionError("paced provider was called")

    def fallback(*_args):
        calls["baostock"] += 1
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [100],
            }
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": SnapshotProvider(must_be_skipped, "qfq"),
            "baostock": SnapshotProvider(fallback, "qfq"),
        },
    )
    monkeypatch.setattr(
        market_snapshot,
        "_coordinator_now",
        lambda: observed_at + pd.Timedelta(seconds=5).to_pytimedelta(),
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    assert snapshot.provider == "baostock"
    assert calls == {"akshare": 0, "baostock": 1}
    paced = snapshot.acquisition_outcomes[0]
    assert paced.reason is AcquisitionUnavailableReason.UPSTREAM_BUSY
    assert paced.retry_after_seconds == 5


@pytest.mark.unit
def test_zero_second_provider_retry_hint_is_preserved(monkeypatch, tmp_path) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "akshare,baostock"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)

    def rate_limited(*_args):
        raise VendorRateLimitError(
            "retry immediately",
            status_code=429,
            retry_after_seconds=0,
        )

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-24"]),
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [100],
        }
    )
    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": SnapshotProvider(rate_limited, "qfq"),
            "baostock": SnapshotProvider(lambda *_args: frame, "qfq"),
        },
    )

    snapshot = get_authoritative_market_snapshot(
        "600519.SS", "2026-07-24", "2026-07-24"
    )

    limited = snapshot.acquisition_outcomes[0]
    assert limited.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert limited.retry_after_seconds == 0


@pytest.mark.unit
def test_provider_capacity_attempts_and_diagnostics_survive_the_snapshot_run(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "shadow",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = (
        "akshare,baostock"
    )
    monkeypatch.setattr(config_module, "_config", runtime_config)
    config = MarketHistoryConfig.from_mapping(runtime_config)
    observed_at = pd.Timestamp("2026-07-24T10:00:00Z").to_pydatetime()

    def rate_limited(*_args):
        raise VendorRateLimitError(
            status_code=429,
            error_code="HTTP_429",
            retry_after_seconds=45,
        )

    def fallback(*_args):
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [100],
            }
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "akshare": SnapshotProvider(rate_limited, "qfq"),
            "baostock": SnapshotProvider(fallback, "qfq"),
        },
    )
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: observed_at)

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    with MarketHistoryStore.open_provider_request_authority(config) as store:
        summary = ProviderRequestCoordinator(store).operational_summary(now=observed_at)

    assert snapshot.provider == "baostock"
    assert snapshot.acquisition_outcomes[0].reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert snapshot.acquisition_outcomes[0].retry_after_seconds == 45
    assert summary.physical_attempts == 2
    assert summary.rate_limit_events == 1
    assert summary.active_cooldowns == 1
    assert summary.active_leases == 0


@pytest.mark.unit
def test_coordinator_records_each_physical_history_request_and_current_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "baostock"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    observed_at = pd.Timestamp("2026-07-24T10:00:00Z").to_pydatetime()

    def incomplete_history(
        *_args,
        before_physical_request,
    ):
        before_physical_request("raw-history")
        before_physical_request("adjustment-factors")
        before_physical_request("session-calendar")
        raise ValueError("history bundle failed validation")

    def current_frame(
        *_args,
        before_physical_request,
    ):
        before_physical_request("adjusted-history")
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-24"]),
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [100],
            }
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": SnapshotProvider(
                current_frame,
                "qfq",
                history_load=incomplete_history,
                reports_physical_requests=True,
            )
        },
    )
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: observed_at)

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    config = MarketHistoryConfig.from_mapping(runtime_config)
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        summary = ProviderRequestCoordinator(store).operational_summary(now=observed_at)

    assert snapshot.provider == "baostock"
    assert summary.physical_attempts == 4
    assert summary.active_leases == 0


@pytest.mark.unit
def test_coordinator_paces_each_physical_request_inside_history_workflow(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    history_root = tmp_path / "history"
    runtime_config.update(
        {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(history_root / "market_history.sqlite3"),
            "market_history_payload_root": str(history_root / "payloads"),
            "market_history_backup_root": str(history_root / "backups"),
        }
    )
    runtime_config["market_data_vendors"]["cn_a"]["core_stock_apis"] = "baostock"
    monkeypatch.setattr(config_module, "_config", runtime_config)
    config = MarketHistoryConfig.from_mapping(runtime_config)
    observed_at = pd.Timestamp("2026-07-24T10:00:00Z").to_pydatetime()
    upstream_id, service_name = upstream_service_identity_for_provider("baostock")
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(upstream_id, service_name)
        coordinator.configure_operator_ceiling(
            upstream_service_id=upstream_id,
            minimum_interval=pd.Timedelta(minutes=3).to_pytimedelta(),
            allow_prewarming=False,
            configured_at=observed_at,
        )

    elapsed_seconds = 0.0
    sleeps = []
    competitor_dispositions = []

    def clock():
        return observed_at + pd.Timedelta(seconds=elapsed_seconds).to_pytimedelta()

    def sleep(seconds):
        nonlocal elapsed_seconds
        sleeps.append(seconds)
        elapsed_seconds += seconds
        if elapsed_seconds >= 180 and not competitor_dispositions:
            with MarketHistoryStore.open_provider_request_authority(
                config
            ) as competing_store:
                competing = ProviderRequestCoordinator(competing_store)
                decision = competing.acquire(
                    request_key="competitor:baostock",
                    upstream_service_id=upstream_id,
                    owner_id="competing-process",
                    priority=RequestPriority.INTERACTIVE_MAINLAND,
                    now=clock(),
                    lease_duration=pd.Timedelta(minutes=2).to_pytimedelta(),
                )
                competitor_dispositions.append(decision.disposition)
                competing.cancel_queued_request(
                    request_key="competitor:baostock",
                    upstream_service_id=upstream_id,
                )

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-07-24"]),
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [100],
        }
    )

    def complete_history(*_args, before_physical_request):
        before_physical_request("raw-history")
        before_physical_request("adjustment-factors")
        before_physical_request("session-calendar")
        return frame

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "baostock": SnapshotProvider(
                lambda *_args, **_kwargs: pytest.fail(
                    "complete history should not use current fallback"
                ),
                "qfq",
                history_load=complete_history,
                reports_physical_requests=True,
            )
        },
    )
    monkeypatch.setattr(market_snapshot, "_coordinator_now", clock)
    monkeypatch.setattr(market_snapshot, "_coordinator_sleep", sleep, raising=False)

    snapshot = get_authoritative_market_snapshot(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    with MarketHistoryStore.open_provider_request_authority(config) as store:
        summary = ProviderRequestCoordinator(store).operational_summary(now=clock())

    assert snapshot.provider == "baostock"
    assert sum(sleeps) == 360.0
    assert max(sleeps) <= 60.0
    assert competitor_dispositions == [LeaseDisposition.UPSTREAM_BUSY]
    assert summary.physical_attempts == 3

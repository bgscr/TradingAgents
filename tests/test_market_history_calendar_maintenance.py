from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import tradingagents.market_history.current as current_history
from tradingagents.market_history import (
    AdjustmentFactorObservation,
    DataUsageMode,
    InstrumentSpec,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
    ProvenanceClass,
    ProviderDatasetSpec,
    ProviderHistoryBundlePublication,
    ProviderRequestCoordinator,
    PublicationState,
    RawMarketObservation,
    RequestPriority,
    SnapshotPurpose,
    TradingStatus,
    TradingStatusObservation,
    assess_history_gaps,
    decide_history_request,
    normalize_mainland_session,
    plan_incremental_refresh,
    reconciliation_due,
)


def _config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _calendar_publication() -> MarketSessionCalendarPublication:
    return MarketSessionCalendarPublication(
        provider=ProviderDatasetSpec(
            upstream_service_id="upstream:baostock-tcp",
            upstream_service_name="BaoStock TCP service",
            provider_dataset_id="provider-dataset:baostock-calendar-v1",
            provider_name="baostock",
            dataset_name="mainland-session-calendar-v1",
            adjustment_methodology="not-applicable",
            strict_history_qualified=False,
        ),
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        observed_at=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"source":"baostock","calendar":"2026-07"}',
        sessions=(
            MarketSession(date(2026, 7, 24), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 7, 25), MarketSessionStatus.CLOSED),
            MarketSession(date(2026, 7, 26), MarketSessionStatus.CLOSED),
            MarketSession(date(2026, 7, 27), MarketSessionStatus.OPEN),
        ),
    )


def _history_publication() -> ProviderHistoryBundlePublication:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    return ProviderHistoryBundlePublication(
        provider=ProviderDatasetSpec(
            upstream_service_id="upstream:baostock-tcp",
            upstream_service_name="BaoStock TCP service",
            provider_dataset_id="provider-dataset:baostock-cn-a-v1",
            provider_name="baostock",
            dataset_name="mainland-raw-status-factors-v1",
            adjustment_methodology="baostock-fore-factor-v1",
            strict_history_qualified=True,
        ),
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
        raw_payload=b'{"provider":"baostock","bundle":"watermark-fixture"}',
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


@pytest.mark.unit
def test_versioned_mainland_session_calendar_round_trips(tmp_path) -> None:
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        published = store.publish_session_calendar(_calendar_publication())
        loaded = store.read_current_session_calendar("mainland-cn")

    assert loaded.calendar_revision_id == published.calendar_revision_id
    assert loaded.timezone_name == "Asia/Shanghai"
    assert tuple(session.session_date for session in loaded.sessions) == (
        date(2026, 7, 24),
        date(2026, 7, 25),
        date(2026, 7, 26),
        date(2026, 7, 27),
    )
    assert loaded.sessions[1].status is MarketSessionStatus.CLOSED


@pytest.mark.unit
def test_calendar_skips_weekend_and_holiday_requests_when_latest_session_is_retained(
    tmp_path,
) -> None:
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        store.publish_session_calendar(_calendar_publication())
        calendar = store.read_current_session_calendar("mainland-cn")

    weekend = decide_history_request(
        calendar,
        as_of=datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc),
        retained_session_dates={date(2026, 7, 24)},
    )
    holiday = decide_history_request(
        calendar,
        as_of=datetime(2026, 7, 26, 4, 0, tzinfo=timezone.utc),
        retained_session_dates={date(2026, 7, 24)},
    )

    assert weekend.should_request is False
    assert weekend.expected_session_date == date(2026, 7, 24)
    assert weekend.reason == "latest_completed_session_retained"
    assert holiday == weekend


@pytest.mark.unit
def test_current_calendar_as_of_uses_real_intraday_time_for_today_and_future(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 27, 2, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(current_history, "_now", lambda: now)

    today = current_history._analysis_as_of(
        date(2026, 7, 27),
        timezone_name="Asia/Shanghai",
    )
    future = current_history._analysis_as_of(
        date(2026, 7, 28),
        timezone_name="Asia/Shanghai",
    )
    historical = current_history._analysis_as_of(
        date(2026, 7, 24),
        timezone_name="Asia/Shanghai",
    )

    assert today.hour == 10 and today.minute == 30
    assert future == today
    assert historical.date() == date(2026, 7, 24)
    assert historical.hour == 23


@pytest.mark.unit
def test_not_yet_published_watermark_persists_and_suppresses_polling_until_cooldown(
    tmp_path,
) -> None:
    observed_at = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    cooldown_until = observed_at + timedelta(minutes=20)
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        store.publish_history_bundle(_history_publication())
        recorded = store.record_publication_watermark(
            instrument_id="instrument:600519.SS",
            provider_dataset_id="provider-dataset:baostock-cn-a-v1",
            expected_session_date=date(2026, 7, 27),
            state=PublicationState.NOT_YET_PUBLISHED,
            observed_at=observed_at,
            cooldown_until=cooldown_until,
        )
        loaded = store.current_publication_watermark(
            "instrument:600519.SS",
            "provider-dataset:baostock-cn-a-v1",
            date(2026, 7, 27),
        )
        store.publish_session_calendar(_calendar_publication())
        calendar = store.read_current_session_calendar("mainland-cn")

    before = decide_history_request(
        calendar,
        as_of=observed_at + timedelta(minutes=5),
        retained_session_dates={date(2026, 7, 24)},
        watermark=loaded,
    )
    after = decide_history_request(
        calendar,
        as_of=cooldown_until,
        retained_session_dates={date(2026, 7, 24)},
        watermark=loaded,
    )

    assert loaded == recorded
    assert before.should_request is False
    assert before.reason == "publication_cooldown"
    assert after.should_request is True


@pytest.mark.unit
def test_only_authoritatively_confirmed_suspension_normalizes_blank_market_values() -> None:
    normalized = normalize_mainland_session(
        session_date=date(2026, 7, 24),
        open_value=None,
        high_value=None,
        low_value=None,
        close_value=None,
        volume=None,
        authoritative_status=TradingStatus.SUSPENDED,
        official_carried_close=Decimal("10.5"),
    )

    assert normalized.observation.open == Decimal("10.5")
    assert normalized.observation.close == Decimal("10.5")
    assert normalized.observation.volume == Decimal("0")
    assert normalized.status.status is TradingStatus.SUSPENDED
    with pytest.raises(ValueError, match="authoritative trading status"):
        normalize_mainland_session(
            session_date=date(2026, 7, 24),
            open_value=None,
            high_value=None,
            low_value=None,
            close_value=None,
            volume=None,
            authoritative_status=None,
            official_carried_close=None,
        )


@pytest.mark.unit
def test_reconstructed_current_suspension_reports_tradeability_and_last_traded_close(
    tmp_path,
) -> None:
    suspension = normalize_mainland_session(
        session_date=date(2026, 7, 24),
        open_value=None,
        high_value=None,
        low_value=None,
        close_value=None,
        volume=None,
        authoritative_status=TradingStatus.SUSPENDED,
        official_carried_close=Decimal("10.5"),
    )
    publication = replace(
        _history_publication(),
        observations=(
            RawMarketObservation(
                date(2026, 7, 23),
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.25"),
                Decimal("100"),
            ),
            suspension.observation,
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 23), TradingStatus.TRADED),
            suspension.status,
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 23), Decimal("1")),
        ),
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        calendar = _calendar_publication()
        store.publish_session_calendar(
            replace(
                calendar,
                sessions=(
                    MarketSession(date(2026, 7, 23), MarketSessionStatus.OPEN),
                    *calendar.sessions,
                ),
            )
        )
        published = store.publish_history_bundle(publication)
        reconstructed = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )

    assert reconstructed.current_tradeability == "suspended"
    assert reconstructed.latest_traded_close == Decimal("10.25")


@pytest.mark.unit
def test_history_gaps_block_only_the_exact_calculation_input_range() -> None:
    calendar = _calendar_publication()

    in_range = assess_history_gaps(
        sessions=calendar.sessions,
        observed_session_dates={date(2026, 7, 27)},
        suspension_session_dates=set(),
        lifecycle_start=date(2026, 7, 24),
        lifecycle_end=None,
        required_start=date(2026, 7, 24),
        required_end=date(2026, 7, 27),
    )
    out_of_range = assess_history_gaps(
        sessions=calendar.sessions,
        observed_session_dates={date(2026, 7, 27)},
        suspension_session_dates=set(),
        lifecycle_start=date(2026, 7, 24),
        lifecycle_end=None,
        required_start=date(2026, 7, 27),
        required_end=date(2026, 7, 27),
    )
    confirmed_suspension = assess_history_gaps(
        sessions=calendar.sessions,
        observed_session_dates={date(2026, 7, 27)},
        suspension_session_dates={date(2026, 7, 24)},
        lifecycle_start=date(2026, 7, 24),
        lifecycle_end=None,
        required_start=date(2026, 7, 24),
        required_end=date(2026, 7, 27),
    )

    assert in_range.all_gaps == (date(2026, 7, 24),)
    assert in_range.blocking_gaps == (date(2026, 7, 24),)
    assert out_of_range.all_gaps == (date(2026, 7, 24),)
    assert out_of_range.blocking_gaps == ()
    assert confirmed_suspension.all_gaps == ()


@pytest.mark.unit
def test_authoritative_current_read_degrades_on_in_range_history_gap(
    tmp_path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    publication = replace(
        _history_publication(),
        observations=(
            RawMarketObservation(
                date(2026, 7, 22),
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.5"),
                Decimal("100"),
            ),
            RawMarketObservation(
                date(2026, 7, 24),
                Decimal("11"),
                Decimal("12"),
                Decimal("10"),
                Decimal("11.5"),
                Decimal("120"),
            ),
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 22), TradingStatus.TRADED),
            TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 22), Decimal("1")),
        ),
    )
    calendar = replace(
        _calendar_publication(),
        sessions=tuple(
            MarketSession(day, MarketSessionStatus.OPEN)
            for day in (date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24))
        ),
    )
    with MarketHistoryStore.open(config) as store:
        store.publish_history_bundle(publication)
        store.publish_session_calendar(calendar)

    monkeypatch.setattr(
        current_history,
        "get_config",
        lambda: {
            "market_history_mode": "authoritative",
            "market_history_database_path": str(config.database_path),
            "market_history_payload_root": str(config.payload_root),
            "market_history_backup_root": str(config.backup_root),
            "data_usage_mode": "personal_research",
        },
    )
    monkeypatch.setattr(
        current_history.MarketHistoryStore,
        "mainland_cutover_ready",
        lambda _store: True,
    )

    result = current_history.try_read_authoritative_mainland_frame(
        "600519.SS",
        "2026-07-22",
        "2026-07-24",
        minimum_history_rows=2,
    )
    exact_latest_only = current_history.try_read_authoritative_mainland_frame(
        "600519.SS",
        "2026-07-22",
        "2026-07-24",
        minimum_history_rows=1,
    )

    assert result.loaded is None
    assert result.history_gap_dates == ("2026-07-23",)
    assert result.diagnostic == "history_gap:2026-07-23"
    assert exact_latest_only.loaded is not None
    assert exact_latest_only.history_gap_dates == ()


@pytest.mark.unit
def test_incremental_refresh_fetches_missing_sessions_plus_latest_twenty_one() -> None:
    sessions = tuple(
        MarketSession(
            date(2026, 6, 1) + timedelta(days=offset),
            MarketSessionStatus.OPEN,
        )
        for offset in range(30)
    )
    old_missing = sessions[2].session_date
    retained = {session.session_date for session in sessions} - {old_missing}

    plan = plan_incremental_refresh(
        sessions=sessions,
        as_of_date=sessions[-1].session_date,
        retained_session_dates=retained,
        seeded=True,
    )

    assert plan.is_seed is False
    assert plan.session_dates[0] == old_missing
    assert set(plan.session_dates[-21:]) == {
        session.session_date for session in sessions[-21:]
    }
    assert len(plan.session_dates) == 22


@pytest.mark.unit
def test_first_seed_is_bounded_to_five_calendar_years() -> None:
    sessions = tuple(
        MarketSession(session_date, MarketSessionStatus.OPEN)
        for session_date in (
            date(2021, 7, 23),
            date(2021, 7, 24),
            date(2026, 7, 24),
        )
    )

    plan = plan_incremental_refresh(
        sessions=sessions,
        as_of_date=date(2026, 7, 24),
        retained_session_dates=set(),
        seeded=False,
    )

    assert plan.is_seed is True
    assert plan.requested_start == date(2021, 7, 24)
    assert plan.requested_end == date(2026, 7, 24)
    assert plan.session_dates == (date(2021, 7, 24), date(2026, 7, 24))


@pytest.mark.unit
def test_monthly_reconciliation_is_due_only_for_active_instruments() -> None:
    assert reconciliation_due(
        lifecycle_state="active",
        last_reconciled_at=None,
        as_of=date(2026, 7, 24),
    )
    assert reconciliation_due(
        lifecycle_state="active",
        last_reconciled_at=date(2026, 6, 24),
        as_of=date(2026, 7, 24),
    )
    assert not reconciliation_due(
        lifecycle_state="active",
        last_reconciled_at=date(2026, 6, 25),
        as_of=date(2026, 7, 24),
    )
    assert not reconciliation_due(
        lifecycle_state="retired",
        last_reconciled_at=None,
        as_of=date(2026, 7, 24),
    )


@pytest.mark.unit
def test_maintenance_selects_only_retained_active_instruments(tmp_path) -> None:
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        store.publish_history_bundle(_history_publication())
        active = store.list_due_reconciliations(date(2026, 7, 24))
        store.set_instrument_lifecycle_state(
            "instrument:600519.SS",
            "provider-dataset:baostock-cn-a-v1",
            "retired",
        )
        retired = store.list_due_reconciliations(date(2026, 7, 24))

    assert tuple(item.instrument_id for item in active) == ("instrument:600519.SS",)
    assert retired == ()


@pytest.mark.unit
def test_reconciliation_completion_persists_next_monthly_due_state(tmp_path) -> None:
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        store.publish_history_bundle(_history_publication())
        store.mark_reconciled(
            "instrument:600519.SS",
            "provider-dataset:baostock-cn-a-v1",
            datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        )
        before_month = store.list_due_reconciliations(date(2026, 8, 23))
        at_month = store.list_due_reconciliations(date(2026, 8, 24))

    assert before_month == ()
    assert tuple(item.instrument_id for item in at_month) == ("instrument:600519.SS",)


@pytest.mark.unit
def test_maintenance_summary_reports_durable_work_and_diagnostics(tmp_path) -> None:
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        store.publish_history_bundle(_history_publication())
        orphan = store.install_payload(b"orphan", media_type="application/octet-stream")
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            "upstream:baostock-tcp",
            "BaoStock TCP service",
        )
        decision = coordinator.acquire(
            request_key="maintenance:600519.SS",
            upstream_service_id="upstream:baostock-tcp",
            owner_id="maintenance-worker",
            priority=RequestPriority.RECONCILIATION,
            now=now,
            lease_duration=timedelta(minutes=2),
            cooldown_scope="history-maintenance",
        )
        coordinator.record_physical_attempt(decision.lease, occurred_at=now)
        coordinator.release(decision.lease)
        coordinator.record_rate_limit(
            upstream_service_id="upstream:baostock-tcp",
            cooldown_scope="history-maintenance",
            observed_at=now,
            retry_after=timedelta(minutes=5),
            provider_code="10001005",
        )

        summary = store.maintenance_summary(as_of=now.date(), now=now)

    assert summary.active_instruments == 1
    assert summary.due_reconciliations == 1
    assert summary.orphan_payloads == 1
    assert summary.active_publication_cooldowns == 0
    assert summary.active_provider_cooldowns == 1
    assert summary.running_ingestion_runs == 0
    assert summary.failed_ingestion_runs == 0
    assert summary.info_diagnostics == 1
    assert summary.warning_diagnostics == 1
    assert summary.error_diagnostics == 0
    assert summary.latest_diagnostic_at == now
    assert orphan.digest in summary.orphan_payload_digests

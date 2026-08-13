from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256

import pytest

from tests.test_market_session_calendar_publication import _acquisition
from tradingagents.market_history import (
    DataUsageMode,
    MainlandSessionCalendarReadinessError,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSession,
    MarketSessionCalendarIntegrityError,
    MarketSessionCalendarReadFailure,
    MarketSessionStatus,
    ProviderRequestCoordinator,
    publication_from_baostock_session_calendar_acquisition,
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


def _changed_publication():
    original = publication_from_baostock_session_calendar_acquisition(_acquisition())
    changed_sessions = (
        original.sessions[0],
        MarketSession(
            session_date=date(2026, 8, 11),
            status=MarketSessionStatus.OPEN,
            opens_at=datetime.fromisoformat("2026-08-11T01:30:00+00:00"),
            closes_at=datetime.fromisoformat("2026-08-11T07:00:00+00:00"),
        ),
    )
    payload = json.loads(original.raw_payload)
    payload["sessions"][1] = {
        "closes_at": "2026-08-11T07:00:00+00:00",
        "opens_at": "2026-08-11T01:30:00+00:00",
        "session_date": "2026-08-11",
        "status": "open",
    }
    raw_payload = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return replace(
        original,
        observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        raw_payload=raw_payload,
        sessions=changed_sessions,
    )


@pytest.mark.unit
def test_exact_reader_returns_requested_revision_not_current(tmp_path) -> None:
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        original_publication = publication_from_baostock_session_calendar_acquisition(
            _acquisition()
        )
        original = store.publish_session_calendar(original_publication)
        changed = store.publish_session_calendar(_changed_publication())

        current = store.read_current_session_calendar("mainland-cn")
        reopened = store.read_session_calendar_revision(original.calendar_revision_id)

    assert original.calendar_revision_id != changed.calendar_revision_id
    assert current.calendar_revision_id == changed.calendar_revision_id
    assert reopened.calendar_revision_id == original.calendar_revision_id
    assert reopened.provider_name == "baostock"
    assert reopened.endpoint == "query_trade_dates"
    assert reopened.normalizer_version == "baostock-session-calendar-normalizer-v1"
    assert reopened.payload_contract == "mainland-session-calendar-payload-v1"
    assert reopened.session_time_contract == "mainland-cn-regular-session-times-v1"
    assert reopened.membership_source == "provider_returned"
    assert reopened.session_time_source == "contract_derived"
    assert reopened.payload_digest == original_publication.payload_digest
    assert reopened.session_count == 2
    assert reopened.sessions == original_publication.sessions


@pytest.mark.unit
def test_exact_reader_has_closed_missing_and_identity_failures(tmp_path) -> None:
    config = _config(tmp_path)
    with MarketHistoryStore.open(config) as store:
        with pytest.raises(MarketSessionCalendarIntegrityError) as missing:
            store.read_session_calendar_revision(
                "market-session-calendar=sha256:" + ("0" * 64)
            )
        assert missing.value.reason is MarketSessionCalendarReadFailure.CALENDAR_MISSING

        published = store.publish_session_calendar(
            publication_from_baostock_session_calendar_acquisition(_acquisition())
        )
        store._connection.execute(
            "UPDATE market_sessions SET closes_at = ? "
            "WHERE calendar_revision_id = ? AND session_date = ?",
            (
                "2026-08-10T08:00:00+00:00",
                published.calendar_revision_id,
                "2026-08-10",
            ),
        )
        with pytest.raises(MarketSessionCalendarIntegrityError) as mismatch:
            store.read_session_calendar_revision(published.calendar_revision_id)

    assert (
        mismatch.value.reason
        is MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
    )
    assert str(mismatch.value) == "revision_identity_mismatch"
    assert repr(mismatch.value) == "MarketSessionCalendarIntegrityError('revision_identity_mismatch')"


@pytest.mark.unit
@pytest.mark.parametrize(
    "tamper",
    (
        "payload_bytes",
        "provider_metadata",
        "upstream_metadata",
        "calendar_metadata",
        "calendar_count_bound",
        "ingestion_metadata",
        "membership",
        "session_times",
    ),
)
def test_exact_reader_rejects_every_tampered_identity_dimension(
    tmp_path,
    tamper: str,
) -> None:
    config = _config(tmp_path)
    publication = publication_from_baostock_session_calendar_acquisition(_acquisition())
    with MarketHistoryStore.open(config) as store:
        published = store.publish_session_calendar(publication)
        revision_id = published.calendar_revision_id
        if tamper == "payload_bytes":
            row = store._connection.execute(
                "SELECT relative_path FROM payload_artifacts WHERE digest = ?",
                (publication.payload_digest,),
            ).fetchone()
            assert row is not None
            (config.payload_root / str(row[0])).write_bytes(b'{"tampered":true}')
        elif tamper == "provider_metadata":
            store._connection.execute(
                "UPDATE provider_datasets SET dataset_name = 'other-calendar' "
                "WHERE provider_dataset_id = ?",
                (publication.provider.provider_dataset_id,),
            )
        elif tamper == "upstream_metadata":
            store._connection.execute(
                "UPDATE upstream_services SET service_name = 'Other service' "
                "WHERE upstream_service_id = ?",
                (publication.provider.upstream_service_id,),
            )
        elif tamper == "calendar_metadata":
            store._connection.execute(
                "UPDATE market_session_calendars SET timezone_name = 'UTC' "
                "WHERE calendar_revision_id = ?",
                (revision_id,),
            )
        elif tamper == "calendar_count_bound":
            store._connection.execute(
                "UPDATE market_session_calendars SET first_observed_at = ? "
                "WHERE calendar_revision_id = ?",
                ("2026-08-11T08:00:00+00:00", revision_id),
            )
        elif tamper == "ingestion_metadata":
            store._connection.execute(
                "UPDATE ingestion_runs SET requested_end = '2026-08-12' "
                "WHERE run_id = ?",
                (published.ingestion_run_id,),
            )
        elif tamper == "membership":
            store._connection.execute(
                "DELETE FROM market_sessions WHERE calendar_revision_id = ? "
                "AND session_date = '2026-08-11'",
                (revision_id,),
            )
        else:
            store._connection.execute(
                "UPDATE market_sessions SET opens_at = ?, closes_at = ? "
                "WHERE calendar_revision_id = ? AND session_date = '2026-08-10'",
                (
                    "2026-08-10T02:00:00+00:00",
                    "2026-08-10T07:00:00+00:00",
                    revision_id,
                ),
            )

        with pytest.raises(MarketSessionCalendarIntegrityError) as captured:
            store.read_session_calendar_revision(revision_id)

    assert (
        captured.value.reason
        is MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
    )
    assert str(captured.value) == "revision_identity_mismatch"
    assert captured.value.__cause__ is None


@pytest.mark.unit
def test_coordinator_exact_reader_is_thread_safe_and_provider_free(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    config = _config(tmp_path)
    with MarketHistoryStore.open(config) as store:
        publication = publication_from_baostock_session_calendar_acquisition(_acquisition())
        published = store.publish_session_calendar(publication)
        attempts_before = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
        coordinator = ProviderRequestCoordinator(store)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                reopened = executor.submit(
                    coordinator.read_session_calendar_revision,
                    published.calendar_revision_id,
                ).result(timeout=5)
            attempts_after = store._connection.execute(
                "SELECT COUNT(*) FROM provider_request_attempts"
            ).fetchone()
            ingestion_count = store._connection.execute(
                "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
            ).fetchone()
        finally:
            coordinator.close()

    assert reopened.calendar_revision_id == published.calendar_revision_id
    assert reopened.payload_digest == sha256(publication.raw_payload).hexdigest()
    assert attempts_before == attempts_after == (0,)
    assert ingestion_count == (1,)


@pytest.mark.unit
def test_exact_and_readiness_failures_discard_unsafe_storage_context(tmp_path) -> None:
    canary_root = tmp_path / "credential=calendar-secret_token=calendar-token"
    config = _config(canary_root)
    with MarketHistoryStore.open(config) as store:
        publication = publication_from_baostock_session_calendar_acquisition(_acquisition())
        published = store.publish_session_calendar(publication)
        row = store._connection.execute(
            "SELECT relative_path FROM payload_artifacts WHERE digest = ?",
            (publication.payload_digest,),
        ).fetchone()
        assert row is not None
        (config.payload_root / str(row[0])).unlink()
        coordinator = ProviderRequestCoordinator(store)
        try:
            failures = []
            with pytest.raises(MarketSessionCalendarIntegrityError) as exact:
                store.read_session_calendar_revision(published.calendar_revision_id)
            failures.append(exact.value)
            with pytest.raises(MarketSessionCalendarIntegrityError) as coordinated:
                coordinator.read_session_calendar_revision(
                    published.calendar_revision_id
                )
            failures.append(coordinated.value)
            with pytest.raises(MainlandSessionCalendarReadinessError) as readiness:
                store.assess_mainland_session_calendar_readiness(
                    published.calendar_revision_id,
                    requested_start_date=date(2026, 8, 10),
                    requested_end_date=date(2026, 8, 11),
                    evaluated_at=datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc),
                )
            failures.append(readiness.value)
        finally:
            coordinator.close()

    canary = str(canary_root.resolve())
    for failure in failures:
        assert failure.__cause__ is None
        assert failure.__context__ is None
        assert canary not in f"{failure!s} {failure!r}"

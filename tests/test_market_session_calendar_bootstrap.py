from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Event, Lock

import pytest
from pydantic import ValidationError

import tradingagents.market_history.bootstrap as bootstrap_module
from tradingagents.dataflows.baostock_calendar import (
    BaoStockSessionCalendarAcquisitionError,
    BaoStockSessionCalendarFailureReason,
    BaoStockSessionCalendarRequest,
    MainlandSessionCalendarMembership,
    acquire_mainland_session_calendar as acquire_with_fake_transport,
)
from tradingagents.market_history import (
    MARKET_SESSION_CALENDAR_BOOTSTRAP_RECEIPT_CONTRACT,
    MARKET_SESSION_CALENDAR_BOOTSTRAP_REQUEST_CONTRACT,
    DataUsageMode,
    MainlandSessionCalendarReadinessError,
    MainlandSessionCalendarReadinessFailure,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSessionCalendarBootstrapReceipt,
    MarketSessionCalendarBootstrapRequest as ExportedBootstrapRequest,
    MarketSessionCalendarIntegrityError,
    MarketSessionCalendarReadFailure,
    ProviderRequestCoordinator,
    bootstrap_mainland_session_calendar as exported_bootstrap,
    publication_from_baostock_session_calendar_acquisition,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.bootstrap import (
    MarketSessionCalendarBootstrapError,
    MarketSessionCalendarBootstrapFailureReason,
    MarketSessionCalendarBootstrapRequest,
    bootstrap_mainland_session_calendar,
)

_NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_programmatic_bootstrap_is_exported_as_one_public_implementation() -> None:
    assert exported_bootstrap is bootstrap_mainland_session_calendar
    assert ExportedBootstrapRequest is MarketSessionCalendarBootstrapRequest
    assert MarketSessionCalendarBootstrapReceipt.__name__ == (
        "MarketSessionCalendarBootstrapReceipt"
    )
    assert MARKET_SESSION_CALENDAR_BOOTSTRAP_REQUEST_CONTRACT == (
        "market-session-calendar-bootstrap-request-v1"
    )
    assert MARKET_SESSION_CALENDAR_BOOTSTRAP_RECEIPT_CONTRACT == (
        "market-session-calendar-bootstrap-receipt-v1"
    )


def _history_config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


class _FixedClock:
    def now(self) -> datetime:
        return _NOW

    def sleep(self, _seconds: float) -> None:
        return None


class _ProviderResult:
    def __init__(self, rows: tuple[tuple[object, ...], ...] = ()) -> None:
        self.error_code = "0"
        self.error_msg = ""
        self.fields = ("calendar_date", "is_trading_day")
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[object]:
        return list(self._rows[self._index])


class _FakeBaoStockTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def login(self) -> object:
        self.calls.append(("login",))
        return _ProviderResult()

    def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
        self.calls.append(("query_trade_dates", start_date, end_date))
        return _ProviderResult(
            (
                ("2026-08-10", "1"),
                ("2026-08-11", "0"),
                ("2026-08-12", "1"),
            )
        )

    def logout(self) -> object:
        self.calls.append(("logout",))
        return _ProviderResult()


def _request(**overrides: object) -> MarketSessionCalendarBootstrapRequest:
    values: dict[str, object] = {
        "start_date": date(2026, 8, 10),
        "end_date": date(2026, 8, 12),
        "evaluated_at": _EVALUATED_AT,
        "data_usage_mode": DataUsageMode.PERSONAL_RESEARCH,
        "authorize_provider_io": True,
    }
    values.update(overrides)
    return MarketSessionCalendarBootstrapRequest(**values)


@pytest.mark.unit
def test_authorized_empty_store_bootstrap_creates_ready_exact_revision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    transport = _FakeBaoStockTransport()
    acquisition_calls = 0

    def acquire(request, **kwargs):
        nonlocal acquisition_calls
        acquisition_calls += 1
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)

    receipt = bootstrap_mainland_session_calendar(
        _request(),
        history_config=config,
        clock=_FixedClock(),
    )

    with MarketHistoryStore.open(config) as store:
        reopened = store.read_session_calendar_revision(receipt.calendar_revision_id)
        ingestion_count = store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone()
        published_at = datetime.fromisoformat(
            str(
                store._connection.execute(
                    "SELECT completed_at FROM ingestion_runs WHERE run_id = ?",
                    (receipt.calendar_refresh_ingestion_id,),
                ).fetchone()[0]
            )
        )
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        attempts = tuple(
            authority._connection.execute(
                "SELECT operation, outcome FROM provider_request_attempts "
                "ORDER BY attempt_index"
            )
        )

    assert acquisition_calls == 1
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    assert receipt.contract_version == "market-session-calendar-bootstrap-receipt-v1"
    assert receipt.disposition == "created"
    assert receipt.provider_io_performed is True
    assert receipt.calendar_revision_id == reopened.calendar_revision_id
    assert receipt.calendar_refresh_ingestion_id == reopened.ingestion_run_id
    assert receipt.retrieved_at == _NOW
    assert receipt.observed_at == reopened.observed_at
    assert receipt.published_at == published_at
    assert receipt.total_session_count == 3
    assert receipt.open_session_count == 2
    assert receipt.closed_session_count == 1
    assert receipt.latest_completed_session == date(2026, 8, 10)
    assert receipt.next_open_session == date(2026, 8, 12)
    assert receipt.physical_attempt_count == 3
    assert ingestion_count == (1,)
    assert attempts == (
        ("baostock_login", "available"),
        ("query_trade_dates", "available"),
        ("baostock_logout", "available"),
    )


@pytest.mark.unit
def test_ready_persisted_revision_reuses_without_authorization_or_new_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    transport = _FakeBaoStockTransport()

    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    created = bootstrap_mainland_session_calendar(
        _request(),
        history_config=config,
        clock=_FixedClock(),
    )

    def forbidden_acquisition(*_args, **_kwargs):
        raise AssertionError("persisted-ready reuse must not acquire")

    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        forbidden_acquisition,
    )
    reused = bootstrap_mainland_session_calendar(
        _request(authorize_provider_io=False),
        history_config=config,
        clock=_FixedClock(),
    )

    with MarketHistoryStore.open(config) as store:
        ingestion_count = store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone()
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        attempt_count = authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
        sequence_count = authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_sequences"
        ).fetchone()

    assert reused.disposition == "reused"
    assert reused.calendar_revision_id == created.calendar_revision_id
    assert reused.calendar_refresh_ingestion_id == created.calendar_refresh_ingestion_id
    assert reused.provider_io_performed is False
    assert reused.retrieved_at is None
    assert reused.published_at == created.published_at
    assert reused.logical_request_count == 0
    assert reused.physical_attempt_count == 0
    assert reused.physical_attempts == ()
    assert ingestion_count == (1,)
    assert attempt_count == (3,)
    assert sequence_count == (1,)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        (
            {"authorize_provider_io": False},
            BaoStockSessionCalendarFailureReason.OPERATOR_AUTHORIZATION_REQUIRED,
        ),
        (
            {"provider": "yahoo"},
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_PROVIDER,
        ),
        (
            {"reference_market": "US"},
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_REFERENCE_MARKET,
        ),
        (
            {"timezone_name": "UTC"},
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_TIMEZONE,
        ),
        (
            {"start_date": None},
            BaoStockSessionCalendarFailureReason.UNBOUNDED_RANGE,
        ),
        (
            {"end_date": None},
            BaoStockSessionCalendarFailureReason.UNBOUNDED_RANGE,
        ),
        (
            {"start_date": date(2026, 8, 13)},
            BaoStockSessionCalendarFailureReason.REVERSED_RANGE,
        ),
        (
            {"end_date": date(2028, 8, 13)},
            BaoStockSessionCalendarFailureReason.RANGE_OUT_OF_POLICY,
        ),
        (
            {"data_usage_mode": DataUsageMode.PRODUCTION},
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_DATA_USAGE_MODE,
        ),
        (
            {"account_scope": "secret-token"},
            BaoStockSessionCalendarFailureReason.UNSAFE_ACCOUNT_SCOPE,
        ),
    ),
)
def test_invalid_or_unauthorized_request_fails_before_provider_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    reason: BaoStockSessionCalendarFailureReason,
) -> None:
    acquisition_called = False

    def forbidden_acquisition(*_args, **_kwargs):
        nonlocal acquisition_called
        acquisition_called = True
        raise AssertionError("invalid request must not acquire")

    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        forbidden_acquisition,
    )

    with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
        bootstrap_mainland_session_calendar(
            _request(**overrides),
            history_config=_history_config(tmp_path),
            clock=_FixedClock(),
        )

    assert captured.value.reason is reason
    assert acquisition_called is False


@pytest.mark.unit
def test_naive_evaluated_at_fails_typed_before_provider_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        lambda *_args, **_kwargs: pytest.fail("naive evaluation must not acquire"),
    )

    with pytest.raises(MarketSessionCalendarBootstrapError) as captured:
        bootstrap_mainland_session_calendar(
            _request(evaluated_at=datetime(2026, 8, 11, 8, 0)),
            history_config=_history_config(tmp_path),
            clock=_FixedClock(),
        )

    assert captured.value.reason is (
        MarketSessionCalendarBootstrapFailureReason.INVALID_EVALUATED_AT
    )


@pytest.mark.unit
def test_force_refresh_requires_authorization_before_acquisition_or_write(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    transport = _FakeBaoStockTransport()

    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    created = bootstrap_mainland_session_calendar(
        _request(), history_config=config, clock=_FixedClock()
    )
    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        lambda *_args, **_kwargs: pytest.fail("unauthorized refresh must not acquire"),
    )

    with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
        bootstrap_mainland_session_calendar(
            _request(force_refresh=True, authorize_provider_io=False),
            history_config=config,
            clock=_FixedClock(),
        )

    with MarketHistoryStore.open(config) as store:
        ingestion_ids = tuple(
            item[0]
            for item in store._connection.execute(
                "SELECT run_id FROM ingestion_runs WHERE operation = 'calendar_refresh'"
            )
        )
    assert captured.value.reason is (
        BaoStockSessionCalendarFailureReason.OPERATOR_AUTHORIZATION_REQUIRED
    )
    assert ingestion_ids == (created.calendar_refresh_ingestion_id,)


@pytest.mark.unit
def test_forced_identical_refresh_reuses_revision_with_later_ingestion_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    transports: list[_FakeBaoStockTransport] = []
    instants = iter((_NOW, _NOW + timedelta(hours=1)))

    class AdvancingClock:
        def __init__(self) -> None:
            self.current = next(instants)

        def now(self) -> datetime:
            return self.current

        def sleep(self, _seconds: float) -> None:
            return None

    def acquire(request, **kwargs):
        transport = _FakeBaoStockTransport()
        transports.append(transport)
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    created = bootstrap_mainland_session_calendar(
        _request(), history_config=config, clock=AdvancingClock()
    )
    refreshed = bootstrap_mainland_session_calendar(
        _request(force_refresh=True),
        history_config=config,
        clock=AdvancingClock(),
    )

    with MarketHistoryStore.open(config) as store:
        revision_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_session_calendars"
        ).fetchone()
        ingestion_count = store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone()
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        attempt_count = authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert refreshed.disposition == "refreshed_same_revision"
    assert refreshed.calendar_revision_id == created.calendar_revision_id
    assert (
        refreshed.calendar_refresh_ingestion_id
        != created.calendar_refresh_ingestion_id
    )
    assert refreshed.published_at > created.published_at
    assert len(transports) == 2
    assert revision_count == (1,)
    assert ingestion_count == (2,)
    assert attempt_count == (6,)


@pytest.mark.unit
def test_reuse_finds_ready_exact_revision_when_newer_current_is_not_capable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    transport = _FakeBaoStockTransport()

    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    ready = bootstrap_mainland_session_calendar(
        _request(), history_config=config, clock=_FixedClock()
    )
    with MarketHistoryStore.open(config) as store:
        old = store.read_session_calendar_revision(ready.calendar_revision_id)
        assert old.calendar_revision_id == ready.calendar_revision_id

    later_transport = _FakeBaoStockTransport()
    later_transport.query_trade_dates = lambda **_kwargs: _ProviderResult(
        (("2026-08-11", "0"), ("2026-08-12", "1"))
    )
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        coordinator = ProviderRequestCoordinator(authority)
        upstream_id, upstream_name = upstream_service_identity_for_provider("baostock")
        coordinator.register_upstream_service(upstream_id, upstream_name)
        later = acquire_with_fake_transport(
            BaoStockSessionCalendarRequest(
                start_date=date(2026, 8, 11),
                end_date=date(2026, 8, 12),
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
                operator_provider_io_authorized=True,
            ),
            coordinator=coordinator,
            transport_factory=lambda: later_transport,
            owner_id="newer-narrow-calendar",
            now=lambda: _NOW + timedelta(hours=1),
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        coordinator.close()
    with MarketHistoryStore.open(config) as store:
        newer = store.publish_session_calendar(
            publication_from_baostock_session_calendar_acquisition(later)
        )
        assert (
            store.read_current_session_calendar("mainland-cn").calendar_revision_id
            == newer.calendar_revision_id
        )

    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        lambda *_args, **_kwargs: pytest.fail("an older ready exact revision must reuse"),
    )
    reused = bootstrap_mainland_session_calendar(
        _request(authorize_provider_io=False),
        history_config=config,
        clock=_FixedClock(),
    )

    assert reused.disposition == "reused"
    assert reused.calendar_revision_id == ready.calendar_revision_id


@pytest.mark.unit
def test_changed_forced_refresh_creates_revision_and_preserves_old_exact_read(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    acquisitions = 0
    current = _NOW

    class AdvancingClock:
        def now(self) -> datetime:
            return current

        def sleep(self, _seconds: float) -> None:
            return None

    def acquire(request, **kwargs):
        nonlocal acquisitions
        acquisitions += 1
        transport = _FakeBaoStockTransport()
        acquired = acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )
        if acquisitions == 2:
            acquired = acquired.model_copy(
                update={
                    "membership": (
                        MainlandSessionCalendarMembership(
                            calendar_date=date(2026, 8, 10),
                            is_trading_day=True,
                        ),
                        MainlandSessionCalendarMembership(
                            calendar_date=date(2026, 8, 11),
                            is_trading_day=True,
                        ),
                        MainlandSessionCalendarMembership(
                            calendar_date=date(2026, 8, 12),
                            is_trading_day=True,
                        ),
                    ),
                    "observed_at": acquired.observed_at + timedelta(hours=1),
                }
            )
        return acquired

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    created = bootstrap_mainland_session_calendar(
        _request(), history_config=config, clock=AdvancingClock()
    )
    current += timedelta(hours=1)
    refreshed = bootstrap_mainland_session_calendar(
        _request(force_refresh=True), history_config=config, clock=AdvancingClock()
    )

    with MarketHistoryStore.open(config) as store:
        old = store.read_session_calendar_revision(created.calendar_revision_id)
        new = store.read_session_calendar_revision(refreshed.calendar_revision_id)

    assert refreshed.disposition == "created"
    assert refreshed.calendar_revision_id != created.calendar_revision_id
    assert tuple(session.status.value for session in old.sessions) == (
        "open",
        "closed",
        "open",
    )
    assert tuple(session.status.value for session in new.sessions) == (
        "open",
        "open",
        "open",
    )


@pytest.mark.unit
def test_created_verification_uses_published_exact_id_when_current_changes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    transport = _FakeBaoStockTransport()
    acquired = None

    def acquire(request, **kwargs):
        nonlocal acquired
        acquired = acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )
        return acquired

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    original_exact_read = MarketHistoryStore.read_session_calendar_revision
    injected_current_id: str | None = None
    exact_ids: list[str] = []

    def exact_read(store, revision_id):
        nonlocal injected_current_id
        exact_ids.append(revision_id)
        if injected_current_id is None:
            assert acquired is not None
            changed = acquired.model_copy(
                update={
                    "membership": tuple(
                        item.model_copy(
                            update={"is_trading_day": True}
                            if item.calendar_date == date(2026, 8, 11)
                            else {}
                        )
                        for item in acquired.membership
                    ),
                    "observed_at": acquired.observed_at + timedelta(hours=1),
                }
            )
            with MarketHistoryStore.open(config) as concurrent_store:
                injected_current_id = concurrent_store.publish_session_calendar(
                    publication_from_baostock_session_calendar_acquisition(changed)
                ).calendar_revision_id
        return original_exact_read(store, revision_id)

    monkeypatch.setattr(
        MarketHistoryStore,
        "read_session_calendar_revision",
        exact_read,
    )

    receipt = bootstrap_mainland_session_calendar(
        _request(), history_config=config, clock=_FixedClock()
    )

    with MarketHistoryStore.open(config) as store:
        current = store.read_current_session_calendar("mainland-cn")
    assert injected_current_id is not None
    assert receipt.calendar_revision_id != injected_current_id
    assert current.calendar_revision_id == injected_current_id
    assert set(exact_ids) == {receipt.calendar_revision_id}


@pytest.mark.unit
def test_exact_reopen_mismatch_blocks_receipt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeBaoStockTransport()

    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    monkeypatch.setattr(
        MarketHistoryStore,
        "read_session_calendar_revision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MarketSessionCalendarIntegrityError(
                MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
            )
        ),
    )

    with pytest.raises(MarketSessionCalendarIntegrityError) as captured:
        bootstrap_mainland_session_calendar(
            _request(),
            history_config=_history_config(tmp_path),
            clock=_FixedClock(),
        )

    assert captured.value.reason is (
        MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
    )


@pytest.mark.unit
def test_readiness_failure_blocks_receipt_without_reacquisition(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeBaoStockTransport()
    acquisition_calls = 0

    def acquire(request, **kwargs):
        nonlocal acquisition_calls
        acquisition_calls += 1
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    monkeypatch.setattr(
        MarketHistoryStore,
        "assess_mainland_session_calendar_readiness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MainlandSessionCalendarReadinessError(
                MainlandSessionCalendarReadinessFailure.MISSING_FORWARD_HORIZON
            )
        ),
    )

    with pytest.raises(MainlandSessionCalendarReadinessError) as captured:
        bootstrap_mainland_session_calendar(
            _request(),
            history_config=_history_config(tmp_path),
            clock=_FixedClock(),
        )

    assert captured.value.reason is (
        MainlandSessionCalendarReadinessFailure.MISSING_FORWARD_HORIZON
    )
    assert acquisition_calls == 1


@pytest.mark.unit
def test_acquisition_failure_creates_no_calendar_publication(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BaoStockSessionCalendarAcquisitionError(
                BaoStockSessionCalendarFailureReason.QUERY_FAILED
            )
        ),
    )

    with pytest.raises(BaoStockSessionCalendarAcquisitionError):
        bootstrap_mainland_session_calendar(
            _request(), history_config=config, clock=_FixedClock()
        )

    with MarketHistoryStore.open(config) as store:
        visible = store._connection.execute(
            "SELECT (SELECT COUNT(*) FROM market_session_calendars), "
            "(SELECT COUNT(*) FROM market_sessions), "
            "(SELECT COUNT(*) FROM ingestion_runs "
            "WHERE operation = 'calendar_refresh')"
        ).fetchone()
    assert visible == (0, 0, 0)


@pytest.mark.unit
def test_publication_failure_creates_no_success_receipt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeBaoStockTransport()

    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    monkeypatch.setattr(
        MarketHistoryStore,
        "publish_session_calendar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MarketSessionCalendarIntegrityError(
                MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
            )
        ),
    )

    with pytest.raises(MarketSessionCalendarIntegrityError):
        bootstrap_mainland_session_calendar(
            _request(),
            history_config=_history_config(tmp_path),
            clock=_FixedClock(),
        )


@pytest.mark.unit
def test_concurrent_empty_store_bootstraps_share_one_workflow_and_publication(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    query_started = Event()
    release_query = Event()
    call_lock = Lock()
    transport_calls: list[tuple[object, ...]] = []

    class BlockingTransport(_FakeBaoStockTransport):
        def login(self) -> object:
            with call_lock:
                transport_calls.append(("login",))
            return _ProviderResult()

        def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
            with call_lock:
                transport_calls.append(("query_trade_dates", start_date, end_date))
            query_started.set()
            assert release_query.wait(timeout=5)
            return _ProviderResult(
                (
                    ("2026-08-10", "1"),
                    ("2026-08-11", "0"),
                    ("2026-08-12", "1"),
                )
            )

        def logout(self) -> object:
            with call_lock:
                transport_calls.append(("logout",))
            return _ProviderResult()

    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=BlockingTransport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(
                bootstrap_mainland_session_calendar,
                _request(),
                history_config=config,
                clock=_FixedClock(),
            )
            for _ in range(2)
        )
        assert query_started.wait(timeout=5)
        time.sleep(0.1)
        release_query.set()
        receipts = tuple(future.result(timeout=10) for future in futures)

    with MarketHistoryStore.open(config) as store:
        counts = store._connection.execute(
            "SELECT (SELECT COUNT(*) FROM market_session_calendars), "
            "(SELECT COUNT(*) FROM market_sessions), "
            "(SELECT COUNT(*) FROM ingestion_runs "
            "WHERE operation = 'calendar_refresh')"
        ).fetchone()
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        attempt_count = authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
        sequence_count = authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_sequences"
        ).fetchone()

    assert transport_calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    assert {receipt.calendar_revision_id for receipt in receipts} == {
        receipts[0].calendar_revision_id
    }
    assert {receipt.calendar_refresh_ingestion_id for receipt in receipts} == {
        receipts[0].calendar_refresh_ingestion_id
    }
    assert counts == (1, 3, 1)
    assert attempt_count == (3,)
    assert sequence_count == (1,)


@pytest.mark.unit
def test_late_follower_during_publication_joins_completed_bootstrap(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    publication_started = Event()
    release_publication = Event()
    transport = _FakeBaoStockTransport()

    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    original_publish = MarketHistoryStore.publish_session_calendar

    def publish(store, publication):
        publication_started.set()
        assert release_publication.wait(timeout=5)
        return original_publish(store, publication)

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    monkeypatch.setattr(MarketHistoryStore, "publish_session_calendar", publish)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(
            bootstrap_mainland_session_calendar,
            _request(),
            history_config=config,
            clock=_FixedClock(),
        )
        assert publication_started.wait(timeout=5)
        follower = executor.submit(
            bootstrap_mainland_session_calendar,
            _request(),
            history_config=config,
            clock=_FixedClock(),
        )
        time.sleep(0.1)
        release_publication.set()
        receipts = (leader.result(timeout=10), follower.result(timeout=10))

    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        attempts = authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
        sequences = authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_sequences"
        ).fetchone()
    with MarketHistoryStore.open(config) as store:
        publications = store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone()

    assert receipts[0] == receipts[1]
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    assert attempts == (3,)
    assert sequences == (1,)
    assert publications == (1,)


@pytest.mark.unit
def test_durable_bootstrap_boundary_prevents_post_acquisition_duplicate_workflow(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model independent processes by bypassing the module-local handoff."""

    config = _history_config(tmp_path)
    transport = _FakeBaoStockTransport()
    acquisition_calls = 0
    acquisition_lock = Lock()

    def acquire(request, **kwargs):
        nonlocal acquisition_calls
        with acquisition_lock:
            acquisition_calls += 1
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)

    def bootstrap_without_process_local_handoff(_index: int):
        return bootstrap_module._bootstrap_mainland_session_calendar(
            _request(),
            history_config=config,
            clock=_FixedClock(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(bootstrap_without_process_local_handoff, range(2)))

    assert acquisition_calls == 1
    assert len({receipt.calendar_revision_id for receipt in receipts}) == 1
    assert {receipt.disposition.value for receipt in receipts} == {"created"}
    assert {receipt.provider_io_performed for receipt in receipts} == {True}
    assert {receipt.physical_attempt_count for receipt in receipts} == {3}
    assert len({receipt.logical_request_id for receipt in receipts}) == 1
    assert len({receipt.workflow_sequence_id for receipt in receipts}) == 1
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        assert authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone() == (3,)
    with MarketHistoryStore.open(config) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone() == (1,)


@pytest.mark.unit
def test_durable_boundary_single_flights_concurrent_forced_refresh(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    initial_transport = _FakeBaoStockTransport()
    refresh_transport = _FakeBaoStockTransport()
    acquisition_calls = 0
    acquisition_started = Event()
    release_acquisition = Event()

    def acquire(request, **kwargs):
        nonlocal acquisition_calls
        acquisition_calls += 1
        if acquisition_calls == 2:
            acquisition_started.set()
            assert release_acquisition.wait(timeout=5)
        transport = initial_transport if acquisition_calls == 1 else refresh_transport
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    created = bootstrap_mainland_session_calendar(
        _request(), history_config=config, clock=_FixedClock()
    )

    def refresh_without_process_local_handoff(_index: int):
        return bootstrap_module._bootstrap_mainland_session_calendar(
            _request(force_refresh=True),
            history_config=config,
            clock=type(
                "RefreshClock",
                (),
                {
                    "now": lambda _self: _NOW + timedelta(hours=1),
                    "sleep": lambda _self, _seconds: None,
                },
            )(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(refresh_without_process_local_handoff, 0)
        assert acquisition_started.wait(timeout=5)
        follower = executor.submit(refresh_without_process_local_handoff, 1)
        time.sleep(0.1)
        release_acquisition.set()
        refreshed = (leader.result(timeout=10), follower.result(timeout=10))

    assert acquisition_calls == 2
    assert {receipt.disposition.value for receipt in refreshed} == {
        "refreshed_same_revision"
    }
    assert {receipt.calendar_revision_id for receipt in refreshed} == {
        created.calendar_revision_id
    }
    assert len({receipt.calendar_refresh_ingestion_id for receipt in refreshed}) == 1
    assert len({receipt.workflow_sequence_id for receipt in refreshed}) == 1
    assert {receipt.physical_attempt_count for receipt in refreshed} == {3}
    assert refresh_transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        assert authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone() == (6,)
    with MarketHistoryStore.open(config) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone() == (2,)


@pytest.mark.unit
def test_late_forced_refresh_follower_after_publication_commit_joins_leader(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    transports: list[_FakeBaoStockTransport] = []
    acquisition_calls = 0

    def acquire(request, **kwargs):
        nonlocal acquisition_calls
        acquisition_calls += 1
        transport = _FakeBaoStockTransport()
        transports.append(transport)
        return acquire_with_fake_transport(
            request,
            transport_factory=lambda: transport,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)
    created = bootstrap_mainland_session_calendar(
        _request(), history_config=config, clock=_FixedClock()
    )

    publication_committed = Event()
    release_publication = Event()
    original_publish = MarketHistoryStore.publish_session_calendar

    def publish(store, publication):
        published = original_publish(store, publication)
        publication_committed.set()
        assert release_publication.wait(timeout=5)
        return published

    monkeypatch.setattr(MarketHistoryStore, "publish_session_calendar", publish)

    def refresh_without_process_local_handoff(_index: int):
        return bootstrap_module._bootstrap_mainland_session_calendar(
            _request(force_refresh=True),
            history_config=config,
            clock=type(
                "RefreshClock",
                (),
                {
                    "now": lambda _self: _NOW + timedelta(hours=1),
                    "sleep": lambda _self, _seconds: None,
                },
            )(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(refresh_without_process_local_handoff, 0)
        assert publication_committed.wait(timeout=5)
        follower = executor.submit(refresh_without_process_local_handoff, 1)
        time.sleep(0.1)
        release_publication.set()
        refreshed = (leader.result(timeout=10), follower.result(timeout=10))

    assert acquisition_calls == 2
    assert {receipt.disposition.value for receipt in refreshed} == {
        "refreshed_same_revision"
    }
    assert {receipt.calendar_revision_id for receipt in refreshed} == {
        created.calendar_revision_id
    }
    assert len({receipt.calendar_refresh_ingestion_id for receipt in refreshed}) == 1
    assert len({receipt.workflow_sequence_id for receipt in refreshed}) == 1
    assert len(transports) == 2
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        assert authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone() == (6,)
    with MarketHistoryStore.open(config) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE operation = 'calendar_refresh'"
        ).fetchone() == (2,)


@pytest.mark.unit
def test_request_and_receipt_serialization_exclude_forbidden_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    request_dump = request.model_dump(mode="json")
    assert "authorize_provider_io" not in request_dump
    assert "authorize_provider_io" not in repr(request)

    forbidden_fields = {
        "symbol",
        "ticker",
        "instrument_identity",
        "requested_session",
        "analysis_run_id",
        "run_id",
        "checkpoint_id",
        "checkpoint_namespace",
        "capability_routing_plan",
        "financial_eligibility_acquisition_context",
        "qualified_v1",
        "credentials",
        "credential_state",
        "token_digest",
        "payload_body",
        "url",
        "query",
        "path",
        "config",
        "raw_text",
    }
    assert forbidden_fields.isdisjoint(request.model_fields)
    assert forbidden_fields.isdisjoint(MarketSessionCalendarBootstrapReceipt.model_fields)
    serialized = json.dumps(request_dump, sort_keys=True)
    for marker in ("credential", "password", "secret", "token", "api_key"):
        assert marker not in serialized.casefold()

    for hostile_value in (
        r"C:\Users\operator\secret-token",
        "C:/Users/operator/calendar",
        "/var/private/calendar",
        "https://provider.invalid/query?credential=raw",
        "x" * 129,
    ):
        with pytest.raises(ValidationError):
            MarketSessionCalendarBootstrapRequest(
                **{
                    **request.model_dump(),
                    "provider": hostile_value,
                    "authorize_provider_io": True,
                }
            )

    transport = _FakeBaoStockTransport()
    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        lambda acquisition_request, **kwargs: acquire_with_fake_transport(
            acquisition_request,
            transport_factory=lambda: transport,
            **kwargs,
        ),
    )
    receipt = bootstrap_mainland_session_calendar(
        request,
        history_config=_history_config(tmp_path),
        clock=_FixedClock(),
    )
    unsafe_receipt = receipt.model_dump()
    unsafe_receipt["upstream_service_name"] = r"C:\private\credential.txt"
    with pytest.raises(ValidationError):
        MarketSessionCalendarBootstrapReceipt.model_validate(unsafe_receipt)

    inconsistent_receipt = receipt.model_dump()
    inconsistent_receipt["physical_attempt_count"] = 2
    with pytest.raises(ValidationError):
        MarketSessionCalendarBootstrapReceipt.model_validate(inconsistent_receipt)

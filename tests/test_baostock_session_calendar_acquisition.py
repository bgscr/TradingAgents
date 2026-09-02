from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from inspect import signature
from threading import Barrier, Event, Lock

import pytest
from pydantic import ValidationError

import tradingagents.market_history.coordinator as coordinator_module
from tradingagents.dataflows.baostock_calendar import (
    BAOSTOCK_CALENDAR_DATASET_NAME,
    BAOSTOCK_CALENDAR_NORMALIZER_VERSION,
    BaoStockSessionCalendarAcquisitionError,
    BaoStockSessionCalendarFailureReason,
    BaoStockSessionCalendarRequest,
    acquire_mainland_session_calendar,
)
from tradingagents.market_history import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)

_NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


def _history_config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


class _ProviderResult:
    def __init__(
        self,
        rows: tuple[tuple[object, ...], ...] = (),
        *,
        fields: tuple[str, ...] = ("calendar_date", "is_trading_day"),
        error_code: str = "0",
        error_msg: str = "",
    ) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = fields
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[object]:
        return list(self._rows[self._index])


class _FakeBaoStockTransport:
    def __init__(
        self,
        *,
        rows: tuple[tuple[object, ...], ...] = (
            ("2026-08-10", "1"),
            ("2026-08-11", "0"),
        ),
        login: object | None = None,
        query: object | None = None,
        logout: object | None = None,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._rows = rows
        self._login = login
        self._query = query
        self._logout = logout
        self._lock = Lock()

    def login(self) -> object:
        with self._lock:
            self.calls.append(("login",))
        if isinstance(self._login, BaseException):
            raise self._login
        return self._login or _ProviderResult()

    def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
        with self._lock:
            self.calls.append(("query_trade_dates", start_date, end_date))
        if isinstance(self._query, BaseException):
            raise self._query
        return self._query or _ProviderResult(self._rows)

    def logout(self) -> object:
        with self._lock:
            self.calls.append(("logout",))
        if isinstance(self._logout, BaseException):
            raise self._logout
        return self._logout or _ProviderResult()


def _request(**overrides: object) -> BaoStockSessionCalendarRequest:
    values: dict[str, object] = {
        "start_date": date(2026, 8, 10),
        "end_date": date(2026, 8, 11),
        "data_usage_mode": DataUsageMode.PERSONAL_RESEARCH,
        "operator_provider_io_authorized": True,
    }
    values.update(overrides)
    return BaoStockSessionCalendarRequest(**values)


def _coordinator(store: MarketHistoryStore) -> ProviderRequestCoordinator:
    coordinator = ProviderRequestCoordinator(store)
    upstream_id, service_name = upstream_service_identity_for_provider("baostock")
    coordinator.register_upstream_service(upstream_id, service_name)
    return coordinator


@pytest.mark.unit
def test_valid_bounded_calendar_request_has_exact_three_attempt_workflow(tmp_path) -> None:
    transport = _FakeBaoStockTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        result = acquire_mainland_session_calendar(
            _request(),
            coordinator=coordinator,
            transport_factory=lambda: transport,
            owner_id="cal-01-success",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        persisted_attempts = tuple(
            store._connection.execute(
                "SELECT attempt_index, operation, outcome, "
                "final_physical_attempt_count FROM provider_request_attempts "
                "WHERE sequence_id = ? ORDER BY attempt_index",
                (result.workflow_sequence_id,),
            )
        )
        calendar_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_session_calendars"
        ).fetchone()
        session_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_sessions"
        ).fetchone()

    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-11"),
        ("logout",),
    ]
    assert [(item.calendar_date, item.is_trading_day) for item in result.membership] == [
        (date(2026, 8, 10), True),
        (date(2026, 8, 11), False),
    ]
    assert result.dataset_name == BAOSTOCK_CALENDAR_DATASET_NAME
    assert result.normalizer_version == BAOSTOCK_CALENDAR_NORMALIZER_VERSION
    assert result.physical_attempt_count == 3
    assert tuple(event.operation for event in result.attempt_events) == (
        "baostock_login",
        "query_trade_dates",
        "baostock_logout",
    )
    assert persisted_attempts == (
        (1, "baostock_login", "available", 3),
        (2, "query_trade_dates", "available", 3),
        (3, "baostock_logout", "available", 3),
    )
    assert calendar_count == (0,)
    assert session_count == (0,)


@pytest.mark.unit
def test_calendar_acquisition_contains_sdk_output_through_complete_row_drain(
    tmp_path,
    capsys,
) -> None:
    raw_canary = "[WinError 10057] calendar socket is not connected"

    class PrintingResult(_ProviderResult):
        def next(self) -> bool:
            print(raw_canary)
            print(raw_canary, file=sys.stderr)
            return super().next()

        def get_row_data(self) -> list[object]:
            print(raw_canary)
            print(raw_canary, file=sys.stderr)
            return super().get_row_data()

    class PrintingTransport(_FakeBaoStockTransport):
        def login(self) -> object:
            print(raw_canary)
            print(raw_canary, file=sys.stderr)
            return super().login()

        def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
            with self._lock:
                self.calls.append(("query_trade_dates", start_date, end_date))
            print(raw_canary)
            print(raw_canary, file=sys.stderr)
            return PrintingResult(self._rows)

        def logout(self) -> object:
            print(raw_canary)
            print(raw_canary, file=sys.stderr)
            return super().logout()

    transport = PrintingTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = acquire_mainland_session_calendar(
            _request(),
            coordinator=_coordinator(store),
            transport_factory=lambda: transport,
            owner_id="ticket07-calendar-output",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

    captured = capsys.readouterr()
    assert [(item.calendar_date, item.is_trading_day) for item in result.membership] == [
        (date(2026, 8, 10), True),
        (date(2026, 8, 11), False),
    ]
    assert raw_canary not in captured.out
    assert raw_canary not in captured.err


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        (
            {"operator_provider_io_authorized": False},
            BaoStockSessionCalendarFailureReason.OPERATOR_AUTHORIZATION_REQUIRED,
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
            {"start_date": date(2026, 8, 12)},
            BaoStockSessionCalendarFailureReason.REVERSED_RANGE,
        ),
        (
            {"end_date": date(2028, 8, 12)},
            BaoStockSessionCalendarFailureReason.RANGE_OUT_OF_POLICY,
        ),
        (
            {"provider": "yfinance"},
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
            {"data_usage_mode": DataUsageMode.PRODUCTION},
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_DATA_USAGE_MODE,
        ),
        (
            {"account_scope": "api-token-secret"},
            BaoStockSessionCalendarFailureReason.UNSAFE_ACCOUNT_SCOPE,
        ),
        (
            {"capacity_scope": "history"},
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_CAPACITY_SCOPE,
        ),
        (
            {"dataset_name": "other-calendar"},
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_DATASET,
        ),
        (
            {"normalizer_version": "other-normalizer"},
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_NORMALIZER,
        ),
    ),
)
def test_invalid_request_fails_before_transport_and_attempts(
    tmp_path,
    overrides: dict[str, object],
    reason: BaoStockSessionCalendarFailureReason,
) -> None:
    transport_constructed = False

    def transport_factory() -> _FakeBaoStockTransport:
        nonlocal transport_constructed
        transport_constructed = True
        return _FakeBaoStockTransport()

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
            acquire_mainland_session_calendar(
                _request(**overrides),
                coordinator=coordinator,
                transport_factory=transport_factory,
                owner_id="cal-01-invalid-request",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        attempt_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
        sequence_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_sequences"
        ).fetchone()

    assert captured.value.reason is reason
    assert transport_constructed is False
    assert attempt_count == (0,)
    assert sequence_count == (0,)


@pytest.mark.unit
@pytest.mark.parametrize("coercible_authorization", (1, "true", "yes"))
def test_operator_authorization_rejects_truthy_non_booleans(
    coercible_authorization,
) -> None:
    with pytest.raises(ValidationError):
        _request(operator_provider_io_authorized=coercible_authorization)


@pytest.mark.unit
def test_unsafe_owner_scope_fails_before_transport_or_coordinator_work(tmp_path) -> None:
    transport_constructed = False

    def transport_factory() -> _FakeBaoStockTransport:
        nonlocal transport_constructed
        transport_constructed = True
        return _FakeBaoStockTransport()

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=transport_factory,
                owner_id="calendar-secret-token",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        attempt_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert captured.value.reason is BaoStockSessionCalendarFailureReason.UNSAFE_OWNER_SCOPE
    assert transport_constructed is False
    assert attempt_count == (0,)


@pytest.mark.unit
def test_request_is_closed_and_has_no_instrument_or_analysis_fields() -> None:
    with pytest.raises(ValidationError):
        BaoStockSessionCalendarRequest(
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 11),
            data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
            operator_provider_io_authorized=True,
            symbol="600519.SS",
        )

    request_fields = set(_request().model_dump())
    forbidden = {
        "symbol",
        "ticker",
        "instrument_identity",
        "analysis_run_id",
        "requested_session",
        "capability_routing_plan",
        "financial_eligibility_context",
        "qualified_v1",
        "force_refresh",
    }
    assert request_fields.isdisjoint(forbidden)


@pytest.mark.unit
def test_absent_bounds_are_typed_unbounded_before_transport(tmp_path) -> None:
    transport_constructed = False

    def transport_factory() -> _FakeBaoStockTransport:
        nonlocal transport_constructed
        transport_constructed = True
        return _FakeBaoStockTransport()

    request = BaoStockSessionCalendarRequest(
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
        operator_provider_io_authorized=True,
    )
    with (
        MarketHistoryStore.open(_history_config(tmp_path)) as store,
        pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured,
    ):
        acquire_mainland_session_calendar(
            request,
            coordinator=_coordinator(store),
            transport_factory=transport_factory,
            owner_id="cal-01-absent-bounds",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

    assert captured.value.reason is BaoStockSessionCalendarFailureReason.UNBOUNDED_RANGE
    assert transport_constructed is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rows", "reason"),
    (
        (
            (("2026-08-10", "1"),),
            BaoStockSessionCalendarFailureReason.INCOMPLETE_RANGE,
        ),
        (
            (("2026-08-10", "1"), ("2026-08-10", "1")),
            BaoStockSessionCalendarFailureReason.DUPLICATE_DATE,
        ),
        (
            (("2026-08-10", "1"), ("2026-08-10", "0")),
            BaoStockSessionCalendarFailureReason.CONTRADICTORY_MEMBERSHIP,
        ),
        (
            (("not-a-date", "1"), ("2026-08-11", "0")),
            BaoStockSessionCalendarFailureReason.MALFORMED_ROW,
        ),
        (
            (("20260810", "1"), ("2026-08-11", "0")),
            BaoStockSessionCalendarFailureReason.MALFORMED_ROW,
        ),
        (
            (("2026-08-09", "1"), ("2026-08-10", "0")),
            BaoStockSessionCalendarFailureReason.OUT_OF_RANGE_DATE,
        ),
        (
            (("2026-08-10", "yes"), ("2026-08-11", "0")),
            BaoStockSessionCalendarFailureReason.INVALID_TRADING_DAY,
        ),
        ((), BaoStockSessionCalendarFailureReason.EMPTY_RESPONSE),
    ),
)
def test_invalid_provider_membership_fails_typed_after_accounted_cleanup(
    tmp_path,
    rows: tuple[tuple[object, ...], ...],
    reason: BaoStockSessionCalendarFailureReason,
) -> None:
    transport = _FakeBaoStockTransport(rows=rows)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: transport,
                owner_id=f"cal-01-normalization-{reason.value}",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        attempts = tuple(
            store._connection.execute(
                "SELECT attempt_index, operation, outcome, "
                "final_physical_attempt_count FROM provider_request_attempts "
                "ORDER BY attempt_index"
            )
        )

    assert captured.value.reason is reason
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-11"),
        ("logout",),
    ]
    assert attempts[0][:3] == (1, "baostock_login", "available")
    assert attempts[1][:2] == (2, "query_trade_dates")
    assert attempts[1][2] in {"empty_frame", "malformed_response"}
    assert attempts[2][:3] == (3, "baostock_logout", "available")
    assert all(item[3] == 3 for item in attempts)


@pytest.mark.unit
def test_login_failure_stops_before_query_and_logout(tmp_path) -> None:
    transport = _FakeBaoStockTransport(
        login=_ProviderResult(error_code="1", error_msg="credential=must-not-leak")
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: transport,
                owner_id="cal-01-login-failure",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        attempts = tuple(
            store._connection.execute(
                "SELECT attempt_index, operation, outcome, "
                "final_physical_attempt_count FROM provider_request_attempts"
            )
        )

    assert captured.value.reason is BaoStockSessionCalendarFailureReason.LOGIN_FAILED
    assert transport.calls == [("login",)]
    assert attempts == ((1, "baostock_login", "authentication", 1),)
    assert "credential" not in str(captured.value).casefold()
    assert "must-not-leak" not in repr(captured.value)


@pytest.mark.unit
def test_raw_transport_exception_is_absent_from_public_exception_chain(tmp_path) -> None:
    secret = "password=CAL01-DO-NOT-LEAK"
    transport = _FakeBaoStockTransport(login=RuntimeError(secret))
    with (
        MarketHistoryStore.open(_history_config(tmp_path)) as store,
        pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured,
    ):
        acquire_mainland_session_calendar(
            _request(),
            coordinator=_coordinator(store),
            transport_factory=lambda: transport,
            owner_id="cal-01-raw-error-security",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

    public = captured.value
    coordinator_error = public.__context__
    assert coordinator_error is not None
    physical_failure = coordinator_error.failure
    assert physical_failure.__context__ is None
    assert physical_failure.__cause__ is None
    safe_surface = repr((public, vars(public), coordinator_error, vars(coordinator_error)))
    assert secret not in safe_surface


@pytest.mark.unit
@pytest.mark.parametrize("logout_fails", (False, True))
def test_query_failure_still_accounts_logout_and_remains_primary(
    tmp_path,
    logout_fails: bool,
) -> None:
    transport = _FakeBaoStockTransport(
        query=_ProviderResult(
            error_code="500",
            error_msg="raw query secret must not leak",
        ),
        logout=(
            _ProviderResult(error_code="1", error_msg="raw logout secret")
            if logout_fails
            else None
        ),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: transport,
                owner_id=f"cal-01-query-failure-{logout_fails}",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        attempts = tuple(
            store._connection.execute(
                "SELECT attempt_index, operation, outcome, "
                "final_physical_attempt_count FROM provider_request_attempts "
                "ORDER BY attempt_index"
            )
        )

    assert captured.value.reason is BaoStockSessionCalendarFailureReason.QUERY_FAILED
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-11"),
        ("logout",),
    ]
    assert attempts == (
        (1, "baostock_login", "available", 3),
        (2, "query_trade_dates", "provider_error", 3),
        (
            3,
            "baostock_logout",
            "provider_error" if logout_fails else "available",
            3,
        ),
    )
    safe_failure = f"{captured.value!s} {captured.value!r}".casefold()
    assert "raw query" not in safe_failure
    assert "raw logout" not in safe_failure
    assert "secret" not in safe_failure


@pytest.mark.unit
def test_logout_failure_does_not_manufacture_an_acquisition(tmp_path) -> None:
    transport = _FakeBaoStockTransport(
        logout=_ProviderResult(error_code="1", error_msg="raw logout provider text")
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: transport,
                owner_id="cal-01-logout-failure",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )

    assert captured.value.reason is BaoStockSessionCalendarFailureReason.LOGOUT_FAILED
    assert captured.value.physical_attempt_count == 3
    assert tuple(event.operation for event in captured.value.attempt_events) == (
        "baostock_login",
        "query_trade_dates",
        "baostock_logout",
    )
    assert captured.value.attempt_events[-1].outcome is PhysicalAttemptOutcome.PROVIDER_ERROR


@pytest.mark.unit
def test_active_global_baostock_cooldown_has_zero_transport_and_attempts(tmp_path) -> None:
    transport_constructed = False

    def transport_factory() -> _FakeBaoStockTransport:
        nonlocal transport_constructed
        transport_constructed = True
        return _FakeBaoStockTransport()

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        upstream_id, _ = upstream_service_identity_for_provider("baostock")
        coordinator.record_rate_limit(
            upstream_service_id=upstream_id,
            cooldown_scope="all",
            observed_at=_NOW,
            retry_after=timedelta(minutes=5),
            provider_code="10001005",
        )
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=transport_factory,
                owner_id="cal-01-active-cooldown",
                now=lambda: _NOW + timedelta(seconds=1),
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        attempt_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert captured.value.reason is BaoStockSessionCalendarFailureReason.COOLDOWN_ACTIVE
    assert captured.value.physical_attempt_count == 0
    assert captured.value.attempt_events == ()
    assert transport_constructed is False
    assert attempt_count == (0,)


@pytest.mark.unit
def test_login_capacity_code_opens_existing_global_baostock_cooldown(tmp_path) -> None:
    first_transport = _FakeBaoStockTransport(
        login=_ProviderResult(error_code="10001005", error_msg="raw capacity text")
    )
    second_transport_constructed = False

    def second_transport_factory() -> _FakeBaoStockTransport:
        nonlocal second_transport_constructed
        second_transport_constructed = True
        return _FakeBaoStockTransport()

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as first_failure:
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: first_transport,
                owner_id="cal-01-login-capacity",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as blocked:
            acquire_mainland_session_calendar(
                _request(end_date=date(2026, 8, 10)),
                coordinator=coordinator,
                transport_factory=second_transport_factory,
                owner_id="cal-01-login-capacity-blocked",
                now=lambda: _NOW + timedelta(seconds=1),
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        cooldown = store._connection.execute(
            "SELECT cooldown_scope, reason FROM request_cooldowns"
        ).fetchone()
        attempt_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert first_failure.value.reason is BaoStockSessionCalendarFailureReason.RATE_LIMITED
    assert first_failure.value.physical_outcome is PhysicalAttemptOutcome.RATE_LIMITED
    assert first_failure.value.physical_attempt_count == 1
    assert first_failure.value.attempt_events[0].cooldown_changed is True
    assert first_failure.value.attempt_events[0].error_code == "10001005"
    assert blocked.value.reason is BaoStockSessionCalendarFailureReason.COOLDOWN_ACTIVE
    assert blocked.value.physical_attempt_count == 0
    assert first_transport.calls == [("login",)]
    assert second_transport_constructed is False
    assert cooldown == ("all", "rate_limited:10001005")
    assert attempt_count == (1,)


@pytest.mark.unit
def test_interruption_stops_before_any_new_transport_and_no_result_escapes(tmp_path) -> None:
    query_started = Event()
    release_query = Event()

    class BlockingTransport(_FakeBaoStockTransport):
        def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
            with self._lock:
                self.calls.append(("query_trade_dates", start_date, end_date))
            query_started.set()
            assert release_query.wait(timeout=5)
            return _ProviderResult(self._rows)

    transport = BlockingTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)

        def acquire():
            return acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: transport,
                owner_id="cal-01-interrupted",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(acquire)
            assert query_started.wait(timeout=5)
            coordinator.interrupt()
            release_query.set()
            with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
                future.result(timeout=5)
        attempts = tuple(
            store._connection.execute(
                "SELECT operation, outcome, final_physical_attempt_count "
                "FROM provider_request_attempts ORDER BY attempt_index"
            )
        )

    assert captured.value.reason is BaoStockSessionCalendarFailureReason.INTERRUPTED
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-11"),
    ]
    assert attempts == (
        ("baostock_login", "available", 2),
        ("query_trade_dates", "provider_error", 2),
    )


@pytest.mark.unit
def test_stop_at_physical_boundary_creates_neither_transport_nor_attempt(tmp_path) -> None:
    stop_signal = Event()
    transport = _FakeBaoStockTransport()

    class BoundaryStoppingCoordinator(ProviderRequestCoordinator):
        def record_physical_attempt(self, *args, **kwargs):
            diagnostic_id = super().record_physical_attempt(*args, **kwargs)
            stop_signal.set()
            return diagnostic_id

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = BoundaryStoppingCoordinator(store, stop_signal=stop_signal)
        upstream_id, service_name = upstream_service_identity_for_provider("baostock")
        coordinator.register_upstream_service(upstream_id, service_name)

        with pytest.raises(BaoStockSessionCalendarAcquisitionError) as captured:
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: transport,
                owner_id="cal-01-boundary-stop",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        attempt_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert captured.value.reason is BaoStockSessionCalendarFailureReason.INTERRUPTED
    assert transport.calls == []
    assert attempt_count == (0,)


@pytest.mark.unit
def test_result_encoding_failure_terminalizes_sequence_and_releases_lease(
    tmp_path,
    monkeypatch,
) -> None:
    transport = _FakeBaoStockTransport()

    def fail_encoding(_value):
        raise TypeError("unsupported coordinated result type: secret-provider-payload")

    monkeypatch.setattr(coordinator_module, "encode_coordinated_result", fail_encoding)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        with pytest.raises(TypeError, match="unsupported coordinated result type"):
            acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: transport,
                owner_id="cal-01-result-encoding-failure",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )
        sequence = store._connection.execute(
            "SELECT status, final_physical_attempt_count, failure_error_code "
            "FROM provider_request_sequences"
        ).fetchone()
        lease_count = store._connection.execute(
            "SELECT COUNT(*) FROM request_leases"
        ).fetchone()
        attempts = tuple(
            store._connection.execute(
                "SELECT operation, outcome, final_physical_attempt_count "
                "FROM provider_request_attempts ORDER BY attempt_index"
            )
        )

    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-11"),
        ("logout",),
    ]
    assert sequence == ("failed", 3, "coordinator_sequence_abandoned")
    assert lease_count == (0,)
    assert attempts == (
        ("baostock_login", "available", 3),
        ("query_trade_dates", "available", 3),
        ("baostock_logout", "available", 3),
    )


@pytest.mark.unit
def test_thirty_two_followers_share_one_terminal_acquisition(tmp_path) -> None:
    query_started = Event()
    release_query = Event()

    class BlockingTransport(_FakeBaoStockTransport):
        def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
            with self._lock:
                self.calls.append(("query_trade_dates", start_date, end_date))
            query_started.set()
            assert release_query.wait(timeout=10)
            return _ProviderResult(self._rows)

    transport = BlockingTransport()
    callers_ready = Barrier(34)
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)

        def acquire(index: int):
            callers_ready.wait(timeout=10)
            return acquire_mainland_session_calendar(
                _request(),
                coordinator=coordinator,
                transport_factory=lambda: transport,
                owner_id=f"cal-01-single-flight-{index}",
                now=lambda: _NOW + timedelta(microseconds=index),
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )

        with ThreadPoolExecutor(max_workers=33) as executor:
            futures = [executor.submit(acquire, index) for index in range(33)]
            callers_ready.wait(timeout=10)
            assert query_started.wait(timeout=5)
            release_query.set()
            results = tuple(future.result(timeout=10) for future in futures)
        persisted_attempts = tuple(
            store._connection.execute(
                "SELECT attempt_index, operation FROM provider_request_attempts "
                "ORDER BY attempt_index"
            )
        )

    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-11"),
        ("logout",),
    ]
    assert persisted_attempts == (
        (1, "baostock_login"),
        (2, "query_trade_dates"),
        (3, "baostock_logout"),
    )
    first = results[0]
    assert all(result == first for result in results)
    assert len({result.workflow_sequence_id for result in results}) == 1
    assert len({result.logical_request_id for result in results}) == 1
    assert all(result.physical_attempt_count == 3 for result in results)


@pytest.mark.unit
def test_separate_coordinator_follower_uses_persisted_workflow_handoff(tmp_path) -> None:
    config = _history_config(tmp_path)
    query_started = Event()
    release_query = Event()
    follower_observed_duplicate = Event()

    class BlockingTransport(_FakeBaoStockTransport):
        def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
            with self._lock:
                self.calls.append(("query_trade_dates", start_date, end_date))
            query_started.set()
            assert release_query.wait(timeout=10)
            return _ProviderResult(self._rows)

    class ObservedFollowerCoordinator(ProviderRequestCoordinator):
        def acquire(self, **kwargs):
            decision = super().acquire(**kwargs)
            if decision.disposition.value == "duplicate_in_flight":
                follower_observed_duplicate.set()
            return decision

    leader_transport = BlockingTransport()
    follower_transport_constructed = False

    def forbidden_follower_transport() -> _FakeBaoStockTransport:
        nonlocal follower_transport_constructed
        follower_transport_constructed = True
        return _FakeBaoStockTransport()

    with (
        MarketHistoryStore.open(config) as leader_store,
        MarketHistoryStore.open(config) as follower_store,
    ):
        leader_coordinator = _coordinator(leader_store)
        upstream_id, service_name = upstream_service_identity_for_provider("baostock")
        follower_coordinator = ObservedFollowerCoordinator(follower_store)
        follower_coordinator.register_upstream_service(upstream_id, service_name)

        def acquire_leader():
            return acquire_mainland_session_calendar(
                _request(),
                coordinator=leader_coordinator,
                transport_factory=lambda: leader_transport,
                owner_id="cal-01-cross-coordinator-leader",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )

        def acquire_follower():
            return acquire_mainland_session_calendar(
                _request(),
                coordinator=follower_coordinator,
                transport_factory=forbidden_follower_transport,
                owner_id="cal-01-cross-coordinator-follower",
                now=lambda: _NOW,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            leader_future = executor.submit(acquire_leader)
            assert query_started.wait(timeout=5)
            follower_future = executor.submit(acquire_follower)
            assert follower_observed_duplicate.wait(timeout=5)
            release_query.set()
            results = (
                leader_future.result(timeout=10),
                follower_future.result(timeout=10),
            )
        attempt_count = leader_store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert results[0] == results[1]
    assert results[0].workflow_sequence_id == results[1].workflow_sequence_id
    assert leader_transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-11"),
        ("logout",),
    ]
    assert follower_transport_constructed is False
    assert attempt_count == (3,)


@pytest.mark.unit
def test_different_ranges_have_distinct_logical_keys_and_workflows(tmp_path) -> None:
    transports = (
        _FakeBaoStockTransport(rows=(("2026-08-10", "1"),)),
        _FakeBaoStockTransport(rows=(("2026-08-11", "0"),)),
    )
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        first = acquire_mainland_session_calendar(
            _request(end_date=date(2026, 8, 10)),
            coordinator=coordinator,
            transport_factory=lambda: transports[0],
            owner_id="cal-01-range-a",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        second = acquire_mainland_session_calendar(
            _request(start_date=date(2026, 8, 11)),
            coordinator=coordinator,
            transport_factory=lambda: transports[1],
            owner_id="cal-01-range-b",
            now=lambda: _NOW + timedelta(seconds=1),
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        attempt_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert first.logical_request_id != second.logical_request_id
    assert first.workflow_sequence_id != second.workflow_sequence_id
    assert attempt_count == (6,)
    assert transports[0].calls[1] == (
        "query_trade_dates",
        "2026-08-10",
        "2026-08-10",
    )
    assert transports[1].calls[1] == (
        "query_trade_dates",
        "2026-08-11",
        "2026-08-11",
    )


@pytest.mark.unit
def test_explicit_range_policy_participates_in_logical_request_identity(tmp_path) -> None:
    transports = (_FakeBaoStockTransport(), _FakeBaoStockTransport())
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = _coordinator(store)
        first = acquire_mainland_session_calendar(
            _request(maximum_inclusive_range_days=731),
            coordinator=coordinator,
            transport_factory=lambda: transports[0],
            owner_id="cal-01-range-policy-a",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )
        second = acquire_mainland_session_calendar(
            _request(maximum_inclusive_range_days=732),
            coordinator=coordinator,
            transport_factory=lambda: transports[1],
            owner_id="cal-01-range-policy-b",
            now=lambda: _NOW + timedelta(seconds=1),
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

    assert first.logical_request_id != second.logical_request_id
    assert transports[0].calls == transports[1].calls


@pytest.mark.unit
def test_result_is_immutable_and_safe_public_projection(tmp_path) -> None:
    secret = "CAL01_SUPER_SECRET"
    transport = _FakeBaoStockTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = acquire_mainland_session_calendar(
            _request(),
            coordinator=_coordinator(store),
            transport_factory=lambda: transport,
            owner_id="cal-01-safe-result",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

    with pytest.raises(ValidationError):
        result.dataset_name = secret
    public_text = repr(result).casefold()
    forbidden = (
        "credential",
        "password",
        "api_key",
        "query=",
        "raw_payload",
        "sdk object",
        "d:\\",
    )
    assert all(marker not in public_text for marker in forbidden)
    assert secret.casefold() not in public_text
    assert result.requested_at == _NOW
    assert result.retrieved_at == _NOW
    assert result.observed_at == _NOW
    serialized = result.model_dump_json()
    assert "operator_provider_io_authorized" not in _request().model_dump()
    assert "operator_provider_io_authorized" not in repr(_request())
    assert "raw_payload" not in serialized


@pytest.mark.unit
def test_public_calendar_seam_has_no_instrument_or_subrequest_cache_dependency(
    tmp_path,
    monkeypatch,
) -> None:
    from tradingagents.dataflows.provider_subrequests import ProviderSubrequestCache

    def forbidden_cache_execute(*_args, **_kwargs):
        raise AssertionError("Instrument-specific Provider Subrequest Cache was used")

    monkeypatch.setattr(ProviderSubrequestCache, "execute", forbidden_cache_execute)
    parameters = set(signature(acquire_mainland_session_calendar).parameters)
    assert parameters.isdisjoint(
        {"symbol", "ticker", "instrument", "instrument_identity"}
    )

    transport = _FakeBaoStockTransport()
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        result = acquire_mainland_session_calendar(
            _request(),
            coordinator=_coordinator(store),
            transport_factory=lambda: transport,
            owner_id="cal-01-no-instrument-cache",
            now=lambda: _NOW,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

    assert result.physical_attempt_count == 3
    assert "600519" not in result.logical_request_id

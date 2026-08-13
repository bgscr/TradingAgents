from __future__ import annotations

import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import cli.main as cli_main
import tradingagents.dataflows.financial_eligibility_acquisition as eligibility_module
import tradingagents.graph.trading_graph as trading_graph_module
import tradingagents.llm_clients.factory as llm_factory_module
import tradingagents.market_history.bootstrap as bootstrap_module
import tradingagents.qualified_routing_composition as composition_module
from cli.main import app
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import preflight_mainland_capability_routing
from tradingagents.dataflows.baostock_calendar import (
    BaoStockSessionCalendarAcquisitionError,
    BaoStockSessionCalendarFailureReason,
    acquire_mainland_session_calendar as acquire_with_fake_transport,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.market_history import (
    DataUsageMode,
    MainlandSessionCalendarReadinessError,
    MainlandSessionCalendarReadinessFailure,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSessionCalendarIntegrityError,
    MarketSessionCalendarReadFailure,
    ProviderRequestCoordinator,
    RevisionKind,
    bootstrap_mainland_session_calendar,
    latest_completed_session,
    market_session_close,
    next_open_market_session,
    revision_identity,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.bootstrap import (
    MARKET_SESSION_CALENDAR_BOOTSTRAP_RECEIPT_CONTRACT,
    MARKET_SESSION_CALENDAR_BOOTSTRAP_REQUEST_CONTRACT,
    MarketSessionCalendarBootstrapError,
    MarketSessionCalendarBootstrapFailureReason,
    MarketSessionCalendarBootstrapRequest,
)
from tradingagents.market_history.calendar import (
    CONTRACT_DERIVED_SESSION_TIME_SOURCE,
    MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
    MAINLAND_SESSION_CALENDAR_DATASET,
    MAINLAND_SESSION_CALENDAR_NORMALIZER,
    MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT,
    MAINLAND_SESSION_CALENDAR_READINESS_CONTRACT,
    PROVIDER_MEMBERSHIP_SOURCE,
)

_NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
_ROWS = (
    ("2026-08-10", "1"),
    ("2026-08-11", "0"),
    ("2026-08-12", "1"),
)
_OPERATIONS = ("baostock_login", "query_trade_dates", "baostock_logout")


class _FixedClock:
    def __init__(self, now: datetime = _NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def sleep(self, _seconds: float) -> None:
        return None


class _ProviderResult:
    def __init__(
        self,
        rows: tuple[tuple[object, ...], ...] = (),
        *,
        error_code: str = "0",
        error_msg: str = "",
    ) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = ("calendar_date", "is_trading_day")
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
        rows: tuple[tuple[object, ...], ...] = _ROWS,
        *,
        login: _ProviderResult | None = None,
        query: _ProviderResult | None = None,
        logout: _ProviderResult | None = None,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._rows = rows
        self._login = login
        self._query = query
        self._logout = logout

    def login(self) -> object:
        self.calls.append(("login",))
        return self._login or _ProviderResult()

    def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
        self.calls.append(("query_trade_dates", start_date, end_date))
        return self._query or _ProviderResult(self._rows)

    def logout(self) -> object:
        self.calls.append(("logout",))
        return self._logout or _ProviderResult()


def _history_config(tmp_path, name: str = "history") -> MarketHistoryConfig:
    root = tmp_path / name
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _request(**overrides: object) -> MarketSessionCalendarBootstrapRequest:
    values: dict[str, object] = {
        "provider": "baostock",
        "reference_market": "mainland-cn",
        "timezone_name": "Asia/Shanghai",
        "start_date": date(2026, 8, 10),
        "end_date": date(2026, 8, 12),
        "evaluated_at": _EVALUATED_AT,
        "data_usage_mode": DataUsageMode.PERSONAL_RESEARCH,
        "authorize_provider_io": True,
    }
    values.update(overrides)
    return MarketSessionCalendarBootstrapRequest(**values)


def _install_fake_acquisition(
    monkeypatch: pytest.MonkeyPatch,
    transport_factory,
) -> None:
    def acquire(request, **kwargs):
        return acquire_with_fake_transport(
            request,
            transport_factory=transport_factory,
            **kwargs,
        )

    monkeypatch.setattr(bootstrap_module, "acquire_mainland_session_calendar", acquire)


def _attempt_rows(config: MarketHistoryConfig) -> tuple[tuple[object, ...], ...]:
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        return tuple(
            authority._connection.execute(
                "SELECT sequence_id, attempt_index, operation, outcome, "
                "final_physical_attempt_count FROM provider_request_attempts "
                "ORDER BY attempted_at, sequence_id, attempt_index"
            )
        )


def _calendar_counts(config: MarketHistoryConfig) -> tuple[int, int, int]:
    with MarketHistoryStore.open(config) as store:
        return tuple(
            int(value)
            for value in store._connection.execute(
                "SELECT (SELECT COUNT(*) FROM market_session_calendars), "
                "(SELECT COUNT(*) FROM market_sessions), "
                "(SELECT COUNT(*) FROM ingestion_runs "
                "WHERE operation = 'calendar_refresh' AND status = 'published')"
            ).fetchone()
        )


def _coordinator_counts(config: MarketHistoryConfig) -> tuple[int, int, int]:
    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        return tuple(
            int(value)
            for value in authority._connection.execute(
                "SELECT (SELECT COUNT(*) FROM provider_request_sequences), "
                "(SELECT COUNT(*) FROM provider_request_attempts), "
                "(SELECT COUNT(*) FROM request_leases)"
            ).fetchone()
        )


def _file_inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _semantic_revision_fixture(
    *,
    payload_digest: str,
    provider_dataset_id: str,
    reference_market: str,
    timezone_name: str,
    sessions,
) -> str:
    return revision_identity(
        RevisionKind.MARKET_SESSION_CALENDAR,
        {
            "payload_digest": payload_digest,
            "provider_dataset_id": provider_dataset_id,
            "reference_market": reference_market,
            "sessions": [
                {
                    "closes_at": (
                        session.closes_at.astimezone(timezone.utc).isoformat()
                        if session.closes_at is not None
                        else None
                    ),
                    "opens_at": (
                        session.opens_at.astimezone(timezone.utc).isoformat()
                        if session.opens_at is not None
                        else None
                    ),
                    "session_date": session.session_date.isoformat(),
                    "status": session.status.value,
                }
                for session in sessions
            ],
            "timezone_name": timezone_name,
        },
    )


def _forbid_analysis_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("calendar bootstrap must not construct analysis state")

    monkeypatch.setattr(trading_graph_module, "TradingAgentsGraph", forbidden)
    monkeypatch.setattr(
        composition_module,
        "build_production_qualified_routing_composition_blueprint",
        forbidden,
    )
    monkeypatch.setattr(
        composition_module,
        "build_registered_production_qualified_routing_composition",
        forbidden,
    )
    monkeypatch.setattr(
        eligibility_module,
        "FinancialEligibilityAcquisitionContext",
        forbidden,
    )
    monkeypatch.setattr(llm_factory_module, "create_llm_client", forbidden)


@pytest.fixture(autouse=True)
def _cal04_forbids_analysis_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_analysis_construction(monkeypatch)


@pytest.mark.unit
def test_cal04_public_bootstrap_lifecycle_is_exact_deterministic_and_read_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)

    with MarketHistoryStore.open(config) as store:
        store._connection.execute(
            "CREATE TABLE cal04_historical_canary "
            "(identity TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        store._connection.executemany(
            "INSERT INTO cal04_historical_canary VALUES (?, ?)",
            (
                ("calendar-history-rows", "immutable-calendar-history"),
                ("snapshot-pins", "immutable-snapshot-pin"),
                ("checkpoints", "immutable-checkpoint"),
                ("reports", "immutable-report"),
                ("provider-attempts", "immutable-provider-attempt"),
                ("failed-ticket-07-evidence", "immutable-ticket07-evidence"),
            ),
        )
        assert store._connection.execute(
            "SELECT COUNT(*) FROM market_session_calendars"
        ).fetchone() == (0,)
        schema_before = tuple(
            store._connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        )

    first_transport = _FakeBaoStockTransport()
    _install_fake_acquisition(monkeypatch, lambda: first_transport)
    created = bootstrap_mainland_session_calendar(
        _request(),
        history_config=config,
        clock=_FixedClock(),
    )

    assert first_transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    assert created.disposition == "created"
    assert created.provider_io_performed is True
    assert created.logical_request_count == 1
    assert created.physical_attempt_count == 3
    assert tuple(item.operation for item in created.physical_attempts) == _OPERATIONS
    assert _calendar_counts(config) == (1, 3, 1)

    read_attempt_snapshot = _attempt_rows(config)
    read_coordinator_snapshot = _coordinator_counts(config)
    read_calendar_snapshot = _calendar_counts(config)
    provider_call_snapshot = tuple(first_transport.calls)
    with MarketHistoryStore.open(config) as store:
        exact = store.read_session_calendar_revision(created.calendar_revision_id)
        current = store.read_current_session_calendar("mainland-cn")
        readiness = store.assess_mainland_session_calendar_readiness(
            created.calendar_revision_id,
            requested_start_date=date(2026, 8, 10),
            requested_end_date=date(2026, 8, 12),
            evaluated_at=_EVALUATED_AT,
        )
        old_payload = store.read_payload(exact.payload_digest)
        historical_before = tuple(
            store._connection.execute(
                "SELECT identity, payload FROM cal04_historical_canary"
            )
        )

    assert exact.calendar_revision_id == created.calendar_revision_id
    assert current.calendar_revision_id == created.calendar_revision_id
    assert exact.provider_name == created.provider == "baostock"
    assert exact.dataset_name == created.dataset_name == MAINLAND_SESSION_CALENDAR_DATASET
    assert exact.reference_market == created.reference_market == "mainland-cn"
    assert exact.timezone_name == created.timezone_name == "Asia/Shanghai"
    assert exact.payload_digest == created.canonical_content_digest
    assert exact.normalizer_version == MAINLAND_SESSION_CALENDAR_NORMALIZER
    assert exact.payload_contract == MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT
    assert exact.session_time_contract == MAINLAND_REGULAR_SESSION_TIME_CONTRACT
    assert exact.membership_source == PROVIDER_MEMBERSHIP_SOURCE
    assert exact.session_time_source == CONTRACT_DERIVED_SESSION_TIME_SOURCE
    assert tuple(session.status.value for session in exact.sessions) == (
        "open",
        "closed",
        "open",
    )
    assert exact.sessions[0].opens_at == datetime(
        2026, 8, 10, 9, 30, tzinfo=timezone(timedelta(hours=8))
    )
    assert exact.sessions[0].closes_at == datetime(
        2026, 8, 10, 15, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert exact.sessions[1].opens_at is exact.sessions[1].closes_at is None
    assert readiness.contract_version == MAINLAND_SESSION_CALENDAR_READINESS_CONTRACT
    assert readiness.latest_completed_session == date(2026, 8, 10)
    assert readiness.latest_completed_session_close == datetime(
        2026, 8, 10, 15, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert readiness.next_open_session == date(2026, 8, 12)
    assert readiness.next_open_session_close == datetime(
        2026, 8, 12, 15, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert latest_completed_session(exact, as_of=_EVALUATED_AT) == date(2026, 8, 10)
    next_session = next_open_market_session(exact, after=date(2026, 8, 10))
    assert next_session is not None
    assert next_session.session_date == date(2026, 8, 12)
    assert market_session_close(exact, session=next_session) == datetime(
        2026, 8, 12, 15, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert _attempt_rows(config) == read_attempt_snapshot
    assert _coordinator_counts(config) == read_coordinator_snapshot
    assert _calendar_counts(config) == read_calendar_snapshot
    assert tuple(first_transport.calls) == provider_call_snapshot
    identity_payload = json.loads(old_payload)
    identity_payload["session_time_contract"] = (
        "mainland-cn-regular-session-times-fixture-v2"
    )
    fixture_digest = sha256(
        json.dumps(
            identity_payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert _semantic_revision_fixture(
        payload_digest=fixture_digest,
        provider_dataset_id=exact.provider_dataset_id,
        reference_market=exact.reference_market,
        timezone_name=exact.timezone_name,
        sessions=exact.sessions,
    ) != exact.calendar_revision_id

    attempt_snapshot = _attempt_rows(config)
    calendar_snapshot = _calendar_counts(config)
    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        lambda *_args, **_kwargs: pytest.fail("ready reuse must not acquire"),
    )
    reused = bootstrap_mainland_session_calendar(
        _request(authorize_provider_io=False),
        history_config=config,
        clock=_FixedClock(),
    )
    assert reused.disposition == "reused"
    assert reused.calendar_revision_id == created.calendar_revision_id
    assert reused.calendar_refresh_ingestion_id == created.calendar_refresh_ingestion_id
    assert reused.provider_io_performed is False
    assert reused.logical_request_count == reused.physical_attempt_count == 0
    assert reused.physical_attempts == ()
    assert _attempt_rows(config) == attempt_snapshot
    assert _calendar_counts(config) == calendar_snapshot

    caller_barrier = Barrier(9)

    def ready_caller():
        caller_barrier.wait(timeout=10)
        return bootstrap_mainland_session_calendar(
            _request(authorize_provider_io=False),
            history_config=config,
            clock=_FixedClock(),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = tuple(executor.submit(ready_caller) for _ in range(8))
        caller_barrier.wait(timeout=10)
        ready_receipts = tuple(future.result(timeout=10) for future in futures)
    assert {item.disposition.value for item in ready_receipts} == {"reused"}
    assert {item.calendar_revision_id for item in ready_receipts} == {
        created.calendar_revision_id
    }
    assert _attempt_rows(config) == attempt_snapshot
    assert _calendar_counts(config) == calendar_snapshot

    identical_transport = _FakeBaoStockTransport()
    _install_fake_acquisition(monkeypatch, lambda: identical_transport)
    identical_attempts_before = _attempt_rows(config)
    identical = bootstrap_mainland_session_calendar(
        _request(force_refresh=True),
        history_config=config,
        clock=_FixedClock(_NOW + timedelta(hours=1)),
    )
    assert identical.disposition == "refreshed_same_revision"
    assert identical.calendar_revision_id == created.calendar_revision_id
    assert identical.calendar_refresh_ingestion_id != created.calendar_refresh_ingestion_id
    assert identical_transport.calls == first_transport.calls
    identical_attempts_after = _attempt_rows(config)
    identical_attempt_delta = identical_attempts_after[
        len(identical_attempts_before) :
    ]
    assert tuple(row[2] for row in identical_attempt_delta) == _OPERATIONS
    assert len(identical_attempt_delta) == len(identical_transport.calls) == 3
    assert len({row[0] for row in identical_attempt_delta}) == 1
    assert _calendar_counts(config) == (1, 3, 2)

    changed_transport = _FakeBaoStockTransport(
        rows=(
            ("2026-08-10", "1"),
            ("2026-08-11", "1"),
            ("2026-08-12", "1"),
        )
    )
    _install_fake_acquisition(monkeypatch, lambda: changed_transport)
    changed = bootstrap_mainland_session_calendar(
        _request(force_refresh=True),
        history_config=config,
        clock=_FixedClock(_NOW + timedelta(hours=2)),
    )
    assert changed.disposition == "created"
    assert changed.calendar_revision_id != created.calendar_revision_id
    assert changed_transport.calls == first_transport.calls

    with MarketHistoryStore.open(config) as store:
        old_exact = store.read_session_calendar_revision(created.calendar_revision_id)
        new_exact = store.read_session_calendar_revision(changed.calendar_revision_id)
        current_after_change = store.read_current_session_calendar("mainland-cn")
        schema_after = tuple(
            store._connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        )
        historical_after = tuple(
            store._connection.execute(
                "SELECT identity, payload FROM cal04_historical_canary"
            )
        )
        assert store.read_payload(old_exact.payload_digest) == old_payload
        duplicate_rows = store._connection.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT calendar_revision_id || session_date) "
            "FROM market_sessions"
        ).fetchone()

    assert old_exact == exact
    assert old_exact.sessions != new_exact.sessions
    assert current_after_change.calendar_revision_id == changed.calendar_revision_id
    assert _calendar_counts(config) == (2, 6, 3)
    assert duplicate_rows == (0,)
    assert schema_after == schema_before
    assert historical_after == historical_before == (
        ("calendar-history-rows", "immutable-calendar-history"),
        ("snapshot-pins", "immutable-snapshot-pin"),
        ("checkpoints", "immutable-checkpoint"),
        ("reports", "immutable-report"),
        ("provider-attempts", "immutable-provider-attempt"),
        ("failed-ticket-07-evidence", "immutable-ticket07-evidence"),
    )

    all_attempts = _attempt_rows(config)
    assert len(all_attempts) == 9
    sequence_ids = tuple(dict.fromkeys(str(row[0]) for row in all_attempts))
    assert len(sequence_ids) == 3
    for sequence_id in sequence_ids:
        workflow = tuple(row for row in all_attempts if row[0] == sequence_id)
        assert tuple(str(row[2]) for row in workflow) == _OPERATIONS
        assert tuple(int(row[1]) for row in workflow) == (1, 2, 3)
        assert all(int(row[4]) == 3 for row in workflow)

    assert MARKET_SESSION_CALENDAR_BOOTSTRAP_REQUEST_CONTRACT == (
        "market-session-calendar-bootstrap-request-v1"
    )
    assert MARKET_SESSION_CALENDAR_BOOTSTRAP_RECEIPT_CONTRACT == (
        "market-session-calendar-bootstrap-receipt-v1"
    )
    assert created.calendar_revision_id.startswith("market-session-calendar=sha256:")
    assert MarketHistoryStore.publish_session_calendar.__name__ == (
        "publish_session_calendar"
    )
    assert MarketHistoryStore.read_current_session_calendar.__name__ == (
        "read_current_session_calendar"
    )
    assert MarketHistoryStore.read_session_calendar_revision.__name__ == (
        "read_session_calendar_revision"
    )
    assert DEFAULT_CONFIG["mainland_capability_routing_mode"] == "legacy"


@pytest.mark.unit
def test_cal04_concurrent_acquisition_bootstrap_has_one_leader_and_publication(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    callers_ready = Barrier(9)
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
            assert release_query.wait(timeout=10)
            return _ProviderResult(_ROWS)

        def logout(self) -> object:
            with call_lock:
                transport_calls.append(("logout",))
            return _ProviderResult()

    _install_fake_acquisition(monkeypatch, BlockingTransport)

    def bootstrap_caller():
        callers_ready.wait(timeout=10)
        return bootstrap_mainland_session_calendar(
            _request(),
            history_config=config,
            clock=_FixedClock(),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = tuple(executor.submit(bootstrap_caller) for _ in range(8))
        callers_ready.wait(timeout=10)
        assert query_started.wait(timeout=10)
        release_query.set()
        receipts = tuple(future.result(timeout=10) for future in futures)

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
    assert {receipt.disposition.value for receipt in receipts} == {"created"}
    assert _calendar_counts(config) == (1, 3, 1)
    attempts = _attempt_rows(config)
    assert tuple(row[2] for row in attempts) == _OPERATIONS
    assert len(attempts) == len(transport_calls) == 3
    assert len({row[0] for row in attempts}) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rows", "cal01_reason"),
    (
        (
            (("2026-08-10", "1"), ("2026-08-12", "1")),
            BaoStockSessionCalendarFailureReason.INCOMPLETE_RANGE,
        ),
        (
            (
                ("2026-08-10", "1"),
                ("2026-08-10", "1"),
                ("2026-08-11", "0"),
                ("2026-08-12", "1"),
            ),
            BaoStockSessionCalendarFailureReason.DUPLICATE_DATE,
        ),
        (
            (
                ("2026-08-10", "1"),
                ("2026-08-10", "0"),
                ("2026-08-11", "0"),
                ("2026-08-12", "1"),
            ),
            BaoStockSessionCalendarFailureReason.CONTRADICTORY_MEMBERSHIP,
        ),
        (
            (
                ("not-a-date", "1"),
                ("2026-08-11", "0"),
                ("2026-08-12", "1"),
            ),
            BaoStockSessionCalendarFailureReason.MALFORMED_ROW,
        ),
        (
            (
                ("2026-08-09", "1"),
                ("2026-08-10", "1"),
                ("2026-08-11", "0"),
                ("2026-08-12", "1"),
            ),
            BaoStockSessionCalendarFailureReason.OUT_OF_RANGE_DATE,
        ),
    ),
)
def test_cal04_bootstrap_maps_incomplete_candidates_without_publication(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    rows: tuple[tuple[object, ...], ...],
    cal01_reason: BaoStockSessionCalendarFailureReason,
) -> None:
    config = _history_config(tmp_path, cal01_reason.value)
    transport = _FakeBaoStockTransport(rows=rows)
    _install_fake_acquisition(monkeypatch, lambda: transport)

    with pytest.raises(MainlandSessionCalendarReadinessError) as captured:
        bootstrap_mainland_session_calendar(
            _request(),
            history_config=config,
            clock=_FixedClock(),
        )

    assert captured.value.reason is MainlandSessionCalendarReadinessFailure.INCOMPLETE_RANGE
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    assert _calendar_counts(config) == (0, 0, 0)
    attempts = _attempt_rows(config)
    assert tuple(row[2] for row in attempts) == _OPERATIONS
    assert len(attempts) == len(transport.calls) == 3


@pytest.mark.unit
def test_cal04_bootstrap_preserves_shared_cooldown_and_failure_accounting(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cooldown_config = _history_config(tmp_path, "cooldown")
    with MarketHistoryStore.open_provider_request_authority(cooldown_config) as authority:
        coordinator = ProviderRequestCoordinator(authority)
        upstream_id, upstream_name = upstream_service_identity_for_provider("baostock")
        coordinator.register_upstream_service(upstream_id, upstream_name)
        coordinator.record_rate_limit(
            upstream_service_id=upstream_id,
            cooldown_scope="all",
            observed_at=_NOW,
            retry_after=timedelta(minutes=5),
            provider_code="10001005",
        )
        coordinator.close()

    transport_constructed = False

    def forbidden_transport() -> _FakeBaoStockTransport:
        nonlocal transport_constructed
        transport_constructed = True
        return _FakeBaoStockTransport()

    _install_fake_acquisition(monkeypatch, forbidden_transport)
    with pytest.raises(BaoStockSessionCalendarAcquisitionError) as blocked:
        bootstrap_mainland_session_calendar(
            _request(),
            history_config=cooldown_config,
            clock=_FixedClock(_NOW + timedelta(seconds=1)),
        )
    assert blocked.value.reason is BaoStockSessionCalendarFailureReason.COOLDOWN_ACTIVE
    assert blocked.value.physical_attempt_count == 0
    assert transport_constructed is False
    assert _attempt_rows(cooldown_config) == ()
    assert _calendar_counts(cooldown_config) == (0, 0, 0)

    login_cap_config = _history_config(tmp_path, "login-cap")
    login_cap = _FakeBaoStockTransport(
        login=_ProviderResult(
            error_code="10001005",
            error_msg="credential=raw-login-capacity-canary",
        )
    )
    _install_fake_acquisition(monkeypatch, lambda: login_cap)
    with pytest.raises(BaoStockSessionCalendarAcquisitionError) as rate_limited:
        bootstrap_mainland_session_calendar(
            _request(),
            history_config=login_cap_config,
            clock=_FixedClock(),
        )
    assert rate_limited.value.reason is BaoStockSessionCalendarFailureReason.RATE_LIMITED
    assert login_cap.calls == [("login",)]
    assert len(_attempt_rows(login_cap_config)) == 1
    with MarketHistoryStore.open_provider_request_authority(login_cap_config) as authority:
        assert authority._connection.execute(
            "SELECT cooldown_scope, reason FROM request_cooldowns"
        ).fetchone() == ("all", "rate_limited:10001005")
    assert "raw-login-capacity-canary" not in repr(rate_limited.value)
    assert _calendar_counts(login_cap_config) == (0, 0, 0)

    query_config = _history_config(tmp_path, "query-failure")
    query_failure = _FakeBaoStockTransport(
        query=_ProviderResult(
            error_code="500",
            error_msg="https://provider.invalid/query?token=query-canary",
        ),
        logout=_ProviderResult(
            error_code="1",
            error_msg=r"C:\private\credential=logout-canary",
        ),
    )
    _install_fake_acquisition(monkeypatch, lambda: query_failure)
    with pytest.raises(BaoStockSessionCalendarAcquisitionError) as query_failed:
        bootstrap_mainland_session_calendar(
            _request(),
            history_config=query_config,
            clock=_FixedClock(),
        )
    assert query_failed.value.reason is BaoStockSessionCalendarFailureReason.QUERY_FAILED
    assert query_failure.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    query_attempts = _attempt_rows(query_config)
    assert tuple(row[2] for row in query_attempts) == _OPERATIONS
    assert tuple(row[3] for row in query_attempts) == (
        "available",
        "provider_error",
        "provider_error",
    )
    assert all(row[4] == 3 for row in query_attempts)
    safe_failure = f"{query_failed.value!s} {query_failed.value!r}"
    assert "provider.invalid" not in safe_failure
    assert "query-canary" not in safe_failure
    assert "logout-canary" not in safe_failure
    assert _calendar_counts(query_config) == (0, 0, 0)


@pytest.mark.unit
@pytest.mark.parametrize(
    "fault_phase",
    (
        "calendar_upstream_verified",
        "calendar_provider_dataset_verified",
        "calendar_ingestion_completed",
        "calendar_revision_written",
        "calendar_sessions_written",
        "calendar_revision_verified",
        "calendar_before_commit",
    ),
)
def test_cal04_bootstrap_publication_failures_are_atomic(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    fault_phase: str,
) -> None:
    config = _history_config(tmp_path, fault_phase)
    transport = _FakeBaoStockTransport()
    _install_fake_acquisition(monkeypatch, lambda: transport)

    original_open = MarketHistoryStore.open.__func__

    class InjectedPublicationFailure(RuntimeError):
        pass

    def fail_at_phase(phase: str, _subject: str) -> None:
        if phase == fault_phase:
            raise InjectedPublicationFailure(fault_phase)

    def open_with_fault(cls, store_config, **kwargs):
        return original_open(cls, store_config, _phase_hook=fail_at_phase, **kwargs)

    monkeypatch.setattr(
        MarketHistoryStore,
        "open",
        classmethod(open_with_fault),
    )
    receipt = None
    with pytest.raises(InjectedPublicationFailure, match=fault_phase) as captured:
        receipt = bootstrap_mainland_session_calendar(
            _request(),
            history_config=config,
            clock=_FixedClock(),
        )

    assert receipt is None
    assert captured.value.__cause__ is None
    assert transport.calls == [
        ("login",),
        ("query_trade_dates", "2026-08-10", "2026-08-12"),
        ("logout",),
    ]
    assert _calendar_counts(config) == (0, 0, 0)
    assert tuple(row[2] for row in _attempt_rows(config)) == _OPERATIONS
    with MarketHistoryStore.open(config) as store:
        handoff_table = store._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'calendar_bootstrap_handoffs'"
        ).fetchone()
        if handoff_table is not None:
            assert store._connection.execute(
                "SELECT COUNT(*) FROM calendar_bootstrap_handoffs"
            ).fetchone() == (0,)
        orphan_digests = store.orphan_payload_digests()
        assert len(orphan_digests) <= 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "tamper",
    (
        "authority",
        "payload_digest",
        "normalizer_contract",
        "time_contract",
        "payload_contract",
        "membership",
        "session_times",
        "counts",
    ),
)
def test_cal04_exact_reopen_rejects_every_identity_tamper(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    config = _history_config(tmp_path, tamper)
    _install_fake_acquisition(monkeypatch, _FakeBaoStockTransport)
    created = bootstrap_mainland_session_calendar(
        _request(),
        history_config=config,
        clock=_FixedClock(),
    )
    attempt_snapshot = _attempt_rows(config)
    coordinator_snapshot = _coordinator_counts(config)
    calendar_snapshot = _calendar_counts(config)
    revision_id = created.calendar_revision_id

    with MarketHistoryStore.open(config) as store:
        original_payload = store.read_payload(created.canonical_content_digest)
        original_sessions = store._connection.execute(
            "SELECT session_date, session_status, opens_at, closes_at "
            "FROM market_sessions WHERE calendar_revision_id = ? "
            "ORDER BY session_date",
            (revision_id,),
        ).fetchall()
        if tamper == "authority":
            store._connection.execute(
                "UPDATE upstream_services SET service_name = 'Other service' "
                "WHERE upstream_service_id = (SELECT d.upstream_service_id "
                "FROM provider_datasets AS d JOIN market_session_calendars AS c "
                "ON c.provider_dataset_id = d.provider_dataset_id "
                "WHERE c.calendar_revision_id = ?)",
                (revision_id,),
            )
        elif tamper == "payload_digest":
            artifact = store.install_payload(
                b'{"credential":"payload-canary"}',
                media_type="application/json",
            )
            store._connection.execute(
                "UPDATE market_session_calendars SET payload_digest = ? "
                "WHERE calendar_revision_id = ?",
                (artifact.digest, revision_id),
            )
        elif tamper in {
            "normalizer_contract",
            "time_contract",
            "payload_contract",
        }:
            payload = json.loads(original_payload)
            field = {
                "normalizer_contract": "normalizer_version",
                "time_contract": "session_time_contract",
                "payload_contract": "contract_version",
            }[tamper]
            payload[field] = f"tampered-{field}-v2"
            artifact = store.install_payload(
                json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode(),
                media_type="application/json",
            )
            store._connection.execute(
                "UPDATE market_session_calendars SET payload_digest = ? "
                "WHERE calendar_revision_id = ?",
                (artifact.digest, revision_id),
            )
        elif tamper == "membership":
            store._connection.execute(
                "UPDATE market_sessions SET session_status = 'closed', "
                "opens_at = NULL, closes_at = NULL "
                "WHERE calendar_revision_id = ? AND session_date = '2026-08-10'",
                (revision_id,),
            )
        elif tamper == "session_times":
            store._connection.execute(
                "UPDATE market_sessions SET closes_at = ? "
                "WHERE calendar_revision_id = ? AND session_date = '2026-08-10'",
                ("2026-08-10T08:00:00+00:00", revision_id),
            )
        else:
            store._connection.execute(
                "DELETE FROM market_sessions WHERE calendar_revision_id = ? "
                "AND session_date = '2026-08-11'",
                (revision_id,),
            )

        with pytest.raises(MarketSessionCalendarIntegrityError) as exact_failure:
            store.read_session_calendar_revision(revision_id)
        with pytest.raises(MainlandSessionCalendarReadinessError) as ready_failure:
            store.assess_mainland_session_calendar_readiness(
                revision_id,
                requested_start_date=date(2026, 8, 10),
                requested_end_date=date(2026, 8, 12),
                evaluated_at=_EVALUATED_AT,
            )
        assert store._connection.execute(
            "SELECT session_date, session_status, opens_at, closes_at "
            "FROM market_sessions WHERE calendar_revision_id = ? "
            "ORDER BY session_date",
            (revision_id,),
        ).fetchall() != original_sessions or tamper in {
            "authority",
            "payload_digest",
            "normalizer_contract",
            "time_contract",
            "payload_contract",
        }

    assert (
        exact_failure.value.reason
        is MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
    )
    assert (
        ready_failure.value.reason
        is MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH
    )
    assert str(exact_failure.value) == "revision_identity_mismatch"
    assert str(ready_failure.value) == "revision_identity_mismatch"
    assert _attempt_rows(config) == attempt_snapshot
    assert _coordinator_counts(config) == coordinator_snapshot
    if tamper == "counts":
        assert _calendar_counts(config) == (1, 2, 1)
    else:
        assert _calendar_counts(config) == calendar_snapshot


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rows", "evaluated_at", "failure"),
    (
        (
            _ROWS,
            datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
            MainlandSessionCalendarReadinessFailure.STALE_COVERAGE,
        ),
        (
            (
                ("2026-08-10", "1"),
                ("2026-08-11", "0"),
                ("2026-08-12", "0"),
            ),
            _EVALUATED_AT,
            MainlandSessionCalendarReadinessFailure.MISSING_FORWARD_HORIZON,
        ),
    ),
)
def test_cal04_structural_coverage_failures_are_persisted_and_provider_free(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    rows: tuple[tuple[object, ...], ...],
    evaluated_at: datetime,
    failure: MainlandSessionCalendarReadinessFailure,
) -> None:
    config = _history_config(tmp_path, failure.value)
    transport = _FakeBaoStockTransport(rows=rows)
    _install_fake_acquisition(monkeypatch, lambda: transport)
    with pytest.raises(MainlandSessionCalendarReadinessError) as acquired_failure:
        bootstrap_mainland_session_calendar(
            _request(evaluated_at=evaluated_at),
            history_config=config,
            clock=_FixedClock(),
        )
    assert acquired_failure.value.reason is failure
    assert _calendar_counts(config) == (1, 3, 1)
    attempts = _attempt_rows(config)
    coordinator_snapshot = _coordinator_counts(config)

    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        lambda *_args, **_kwargs: pytest.fail("persisted readiness must not acquire"),
    )
    with MarketHistoryStore.open(config) as store:
        revision_id = store.read_current_session_calendar(
            "mainland-cn"
        ).calendar_revision_id
        with pytest.raises(MainlandSessionCalendarReadinessError) as replay_failure:
            store.assess_mainland_session_calendar_readiness(
                revision_id,
                requested_start_date=date(2026, 8, 10),
                requested_end_date=date(2026, 8, 12),
                evaluated_at=evaluated_at,
            )
    assert replay_failure.value.reason is failure
    assert _attempt_rows(config) == attempts
    assert _coordinator_counts(config) == coordinator_snapshot
    assert _calendar_counts(config) == (1, 3, 1)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "error_type", "reason"),
    (
        (
            {"authorize_provider_io": False},
            BaoStockSessionCalendarAcquisitionError,
            BaoStockSessionCalendarFailureReason.OPERATOR_AUTHORIZATION_REQUIRED,
        ),
        (
            {"provider": "other"},
            BaoStockSessionCalendarAcquisitionError,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_PROVIDER,
        ),
        (
            {"reference_market": "other-market"},
            BaoStockSessionCalendarAcquisitionError,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_REFERENCE_MARKET,
        ),
        (
            {"timezone_name": "UTC"},
            BaoStockSessionCalendarAcquisitionError,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_TIMEZONE,
        ),
        (
            {"start_date": None},
            BaoStockSessionCalendarAcquisitionError,
            BaoStockSessionCalendarFailureReason.UNBOUNDED_RANGE,
        ),
        (
            {"start_date": date(2026, 8, 12), "end_date": date(2026, 8, 10)},
            BaoStockSessionCalendarAcquisitionError,
            BaoStockSessionCalendarFailureReason.REVERSED_RANGE,
        ),
        (
            {"end_date": date(2028, 8, 12)},
            BaoStockSessionCalendarAcquisitionError,
            BaoStockSessionCalendarFailureReason.RANGE_OUT_OF_POLICY,
        ),
        (
            {"evaluated_at": datetime(2026, 8, 11, 8, 0)},
            MarketSessionCalendarBootstrapError,
            MarketSessionCalendarBootstrapFailureReason.INVALID_EVALUATED_AT,
        ),
    ),
)
def test_cal04_authorization_and_material_preflight_fail_before_transport(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    error_type: type[Exception],
    reason: object,
) -> None:
    config = _history_config(tmp_path, str(getattr(reason, "value", reason)))
    transport_constructed = False

    def forbidden_transport() -> _FakeBaoStockTransport:
        nonlocal transport_constructed
        transport_constructed = True
        return _FakeBaoStockTransport()

    _install_fake_acquisition(monkeypatch, forbidden_transport)
    with pytest.raises(error_type) as captured:
        bootstrap_mainland_session_calendar(
            _request(**overrides),
            history_config=config,
            clock=_FixedClock(),
        )
    assert captured.value.reason is reason
    assert transport_constructed is False
    assert _coordinator_counts(config) == (0, 0, 0)
    assert _calendar_counts(config) == (0, 0, 0)


@pytest.mark.unit
def test_cal04_safe_projection_and_historical_inventory_are_bounded_and_immutable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _history_config(tmp_path)
    historical_root = tmp_path / "historical-ticket07"
    historical_root.mkdir()
    canaries = (
        "credential=calendar-secret-canary",
        "https://provider.invalid/query?token=url-canary",
        r"C:\private\calendar-path-canary",
        '{"payload":"raw-payload-canary"}',
    )
    for index, canary in enumerate(canaries):
        (historical_root / f"artifact-{index}.txt").write_text(
            canary,
            encoding="utf-8",
        )
    historical_inventory = _file_inventory(historical_root)

    transport = _FakeBaoStockTransport()
    _install_fake_acquisition(monkeypatch, lambda: transport)
    request = _request()
    request_projection = request.model_dump(mode="json")
    created = bootstrap_mainland_session_calendar(
        request,
        history_config=config,
        clock=_FixedClock(),
    )
    with MarketHistoryStore.open(config) as store:
        exact = store.read_session_calendar_revision(created.calendar_revision_id)
        persisted_metadata = tuple(
            store._connection.execute(
                "SELECT calendar_revision_id, reference_market, timezone_name, "
                "provider_dataset_id, payload_digest, ingestion_run_id "
                "FROM market_session_calendars"
            )
        )
    receipt_json = json.dumps(created.model_dump(mode="json"), sort_keys=True)
    attempt_json = json.dumps(
        [attempt.model_dump(mode="json") for attempt in created.physical_attempts],
        sort_keys=True,
    )
    public_surfaces = " ".join(
        (
            json.dumps(request_projection, sort_keys=True),
            repr(request),
            receipt_json,
            repr(created),
            attempt_json,
            repr(exact),
            repr(persisted_metadata),
            " ".join(record.getMessage() for record in caplog.records),
        )
    )
    for canary in canaries:
        assert canary not in public_surfaces
    assert "authorize_provider_io" not in request_projection
    assert len(receipt_json) < 16_384
    assert historical_inventory == _file_inventory(historical_root)

    for hostile in (*canaries, "x" * 129):
        with pytest.raises(ValidationError):
            MarketSessionCalendarBootstrapRequest(
                **{
                    **request.model_dump(),
                    "provider": hostile,
                    "authorize_provider_io": True,
                }
            )


@pytest.mark.unit
def test_cal04_cli_programmatic_parity_and_qualified_preflight_are_read_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _history_config(tmp_path)
    transport = _FakeBaoStockTransport()
    _install_fake_acquisition(monkeypatch, lambda: transport)
    created = bootstrap_mainland_session_calendar(
        _request(),
        history_config=config,
        clock=_FixedClock(),
    )
    attempt_snapshot = _attempt_rows(config)
    calendar_snapshot = _calendar_counts(config)

    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_database_path",
        str(config.database_path),
    )
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_payload_root",
        str(config.payload_root),
    )
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_backup_root",
        str(config.backup_root),
    )
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "market_history_mode", "shadow")
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "data_usage_mode",
        "personal_research",
    )
    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        lambda *_args, **_kwargs: pytest.fail("CLI ready reuse must not acquire"),
    )
    monkeypatch.setattr(
        cli_main,
        "run_analysis",
        lambda *_args, **_kwargs: pytest.fail("calendar CLI must not run analysis"),
    )

    cli_result = CliRunner().invoke(
        app,
        [
            "market-session-calendar-bootstrap",
            "--reference-market",
            "mainland-cn",
            "--provider",
            "baostock",
            "--start-date",
            "2026-08-10",
            "--end-date",
            "2026-08-12",
            "--evaluated-at",
            "2026-08-11T16:00:00+08:00",
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output
    assert len(cli_result.stdout.splitlines()) == 1
    cli_receipt = json.loads(cli_result.stdout)
    assert cli_receipt["disposition"] == "reused"
    assert cli_receipt["calendar_revision_id"] == created.calendar_revision_id
    assert cli_receipt["canonical_content_digest"] == created.canonical_content_digest
    assert cli_receipt["latest_completed_session"] == "2026-08-10"
    assert cli_receipt["next_open_session"] == "2026-08-12"
    assert cli_receipt["total_session_count"] == created.total_session_count
    assert cli_receipt["physical_attempt_count"] == 0
    assert _attempt_rows(config) == attempt_snapshot
    assert _calendar_counts(config) == calendar_snapshot

    failure_config = _history_config(tmp_path, "cli-failure")
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_database_path",
        str(failure_config.database_path),
    )
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_payload_root",
        str(failure_config.payload_root),
    )
    monkeypatch.setitem(
        cli_main.DEFAULT_CONFIG,
        "market_history_backup_root",
        str(failure_config.backup_root),
    )
    typed_failure = CliRunner().invoke(
        app,
        [
            "market-session-calendar-bootstrap",
            "--reference-market",
            "mainland-cn",
            "--provider",
            "baostock",
            "--start-date",
            "2026-08-10",
            "--end-date",
            "2026-08-12",
            "--evaluated-at",
            "2026-08-11T16:00:00+08:00",
        ],
    )
    assert typed_failure.exit_code == 2
    assert len(typed_failure.stdout.splitlines()) == 1
    assert json.loads(typed_failure.stdout) == {
        "contract_version": "market-session-calendar-bootstrap-error-v1",
        "diagnostic_code": "operator_authorization_required",
        "failure_category": "request",
        "physical_attempt_count": 0,
        "retryable": False,
    }
    assert str(failure_config.database_path) not in typed_failure.output
    assert "credential" not in typed_failure.output.casefold()
    assert _coordinator_counts(failure_config) == (0, 0, 0)
    assert _calendar_counts(failure_config) == (0, 0, 0)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("qualified preflight must remain calendar-write free")

    monkeypatch.setattr(bootstrap_module, "bootstrap_mainland_session_calendar", forbidden)
    monkeypatch.setattr(
        bootstrap_module,
        "acquire_mainland_session_calendar",
        forbidden,
    )
    monkeypatch.setattr(MarketHistoryStore, "publish_session_calendar", forbidden)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    rollout_config = copy.deepcopy(DEFAULT_CONFIG)
    rollout_config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": ("statements",),
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    asset = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    preflight = preflight_mainland_capability_routing(
        asset,
        config=rollout_config,
        environment={"TUSHARE_TOKEN": "deterministic-fixture-secret"},
    )
    assert preflight.failure is None
    assert preflight.plan is not None
    assert preflight.plan.mode.value == "qualified_v1_shadow"
    assert _attempt_rows(config) == attempt_snapshot
    assert _calendar_counts(config) == calendar_snapshot

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from threading import Event, Lock

import pytest

from tradingagents.market_history import (
    DataUsageMode,
    LeaseDisposition,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    PhysicalAttemptBudgetExhausted,
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderRequestAuthorityUnavailableError,
    ProviderRequestCoordinator,
    RequestPriority,
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


def _multiprocess_retry_caller(
    config: MarketHistoryConfig,
    role: str,
    first_attempt_started,
    release_first_attempt,
    follower_observed_duplicate,
    transport_calls,
    results,
) -> None:
    """Spawn-safe worker proving SQLite-backed result handoff across processes."""

    class ObservingCoordinator(ProviderRequestCoordinator):
        def acquire(self, **kwargs):
            decision = super().acquire(**kwargs)
            if (
                role == "follower"
                and decision.disposition is LeaseDisposition.DUPLICATE_IN_FLIGHT
            ):
                follower_observed_duplicate.set()
            return decision

    def physical_attempt(attempt_index: int) -> str:
        transport_calls.put((role, attempt_index))
        if role != "leader":
            raise AssertionError("the follower must not make a physical attempt")
        if attempt_index == 1:
            first_attempt_started.set()
            if not release_first_attempt.wait(timeout=60):
                raise AssertionError("leader release timed out")
        if attempt_index < 4:
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.DISCONNECT,
                retryable=True,
            )
        return "available-frame"

    try:
        with MarketHistoryStore.open(config) as store:
            result = ObservingCoordinator(store).execute_retry_sequence(
                request_key="history:BTC-USD:multiprocess-single-flight",
                upstream_service_id="upstream:yahoo",
                owner_id=f"{role}-process",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: datetime.now(timezone.utc),
                sleep=time.sleep,
                lease_duration=timedelta(minutes=2),
                max_physical_attempts=4,
                operation="market-snapshot",
                physical_attempt=physical_attempt,
            )
        results.put(
            (
                role,
                "ok",
                result.value,
                result.sequence_id,
                result.physical_attempt_count,
            )
        )
    except BaseException as exc:
        results.put((role, "error", type(exc).__name__, str(exc), 0))


@pytest.mark.unit
def test_coordinator_single_flights_and_allows_only_one_request_per_upstream(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    config = _config(tmp_path)
    with (
        MarketHistoryStore.open(config) as first_store,
        MarketHistoryStore.open(config) as second_store,
    ):
        first = ProviderRequestCoordinator(first_store)
        second = ProviderRequestCoordinator(second_store)
        first.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")

        acquired = first.acquire(
            request_key="history:600519.SS:2026-07",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        duplicate = second.acquire(
            request_key="history:600519.SS:2026-07",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-b",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        busy = second.acquire(
            request_key="history:000001.SZ:2026-07",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-b",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now,
            lease_duration=timedelta(seconds=30),
        )

        assert acquired.disposition is LeaseDisposition.ACQUIRED
        assert duplicate.disposition is LeaseDisposition.DUPLICATE_IN_FLIGHT
        assert busy.disposition is LeaseDisposition.UPSTREAM_BUSY
        first.release(acquired.lease)

        after_release = second.acquire(
            request_key="history:000001.SZ:2026-07",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-b",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        assert after_release.disposition is LeaseDisposition.ACQUIRED


@pytest.mark.unit
def test_concurrent_process_connections_deduplicate_one_physical_request(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        ProviderRequestCoordinator(store).register_upstream_service(
            "upstream:yahoo",
            "Yahoo Finance",
        )

    def acquire(owner_id: str) -> LeaseDisposition:
        with MarketHistoryStore.open(config) as store:
            decision = ProviderRequestCoordinator(store).acquire(
                request_key="history:BTC-USD:2026-07",
                upstream_service_id="upstream:yahoo",
                owner_id=owner_id,
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=now,
                lease_duration=timedelta(seconds=30),
            )
            return decision.disposition

    with ThreadPoolExecutor(max_workers=8) as executor:
        dispositions = tuple(executor.map(acquire, (f"process-{i}" for i in range(8))))

    assert dispositions.count(LeaseDisposition.ACQUIRED) == 1
    assert dispositions.count(LeaseDisposition.DUPLICATE_IN_FLIGHT) == 7


@pytest.mark.unit
def test_expired_lease_is_recovered_after_owner_crash(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:baostock", "BaoStock TCP")
        abandoned = coordinator.acquire(
            request_key="history:600519.SS",
            upstream_service_id="upstream:baostock",
            owner_id="crashed-process",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now,
            lease_duration=timedelta(seconds=5),
        )
        recovered = coordinator.acquire(
            request_key="history:000001.SZ",
            upstream_service_id="upstream:baostock",
            owner_id="replacement-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=5),
            lease_duration=timedelta(seconds=5),
        )

    assert abandoned.disposition is LeaseDisposition.ACQUIRED
    assert recovered.disposition is LeaseDisposition.ACQUIRED


@pytest.mark.unit
def test_coordinator_normalizes_offset_timestamps_before_sqlite_comparison(tmp_path) -> None:
    config = _config(tmp_path)
    local_now = datetime.fromisoformat("2026-07-24T18:00:00+08:00")
    utc_expiry = datetime(2026, 7, 24, 10, 0, 5, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:baostock", "BaoStock TCP")
        first = coordinator.acquire(
            request_key="history:600519.SS",
            upstream_service_id="upstream:baostock",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=local_now,
            lease_duration=timedelta(seconds=5),
        )
        recovered = coordinator.acquire(
            request_key="history:000001.SZ",
            upstream_service_id="upstream:baostock",
            owner_id="process-b",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=utc_expiry,
            lease_duration=timedelta(seconds=5),
        )

    assert first.disposition is LeaseDisposition.ACQUIRED
    assert first.lease is not None
    assert first.lease.acquired_at == datetime(
        2026, 7, 24, 10, 0, tzinfo=timezone.utc
    )
    assert recovered.disposition is LeaseDisposition.ACQUIRED


@pytest.mark.unit
def test_cancelling_abandoned_waiter_removes_priority_blocker(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        blocker = coordinator.acquire(
            request_key="active",
            upstream_service_id="upstream:eastmoney",
            owner_id="active-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        abandoned = coordinator.acquire(
            request_key="abandoned",
            upstream_service_id="upstream:eastmoney",
            owner_id="abandoning-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        assert abandoned.disposition is LeaseDisposition.UPSTREAM_BUSY
        coordinator.cancel_queued_request(
            request_key="abandoned",
            upstream_service_id="upstream:eastmoney",
        )
        coordinator.release(blocker.lease)
        later = coordinator.acquire(
            request_key="later",
            upstream_service_id="upstream:eastmoney",
            owner_id="later-process",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now,
            lease_duration=timedelta(seconds=30),
        )

    assert later.disposition is LeaseDisposition.ACQUIRED


@pytest.mark.unit
def test_rate_limit_cooldown_is_typed_persisted_and_shared_across_processes(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        recorded = coordinator.record_rate_limit(
            upstream_service_id="upstream:yahoo",
            cooldown_scope="market-history",
            observed_at=now,
            retry_after=timedelta(seconds=60),
            provider_code="HTTP_429",
        )

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        cooling = coordinator.acquire(
            request_key="history:600519.SS",
            upstream_service_id="upstream:yahoo",
            owner_id="process-b",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=30),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="market-history",
        )
        after = coordinator.acquire(
            request_key="history:600519.SS",
            upstream_service_id="upstream:yahoo",
            owner_id="process-b",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=60),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="market-history",
        )

    assert recorded.reason == "rate_limited:HTTP_429"
    assert recorded.retry_after_seconds == 60.0
    assert cooling.disposition is LeaseDisposition.COOLDOWN
    assert cooling.cooldown_until == now + timedelta(seconds=60)
    assert after.disposition is LeaseDisposition.ACQUIRED


@pytest.mark.unit
def test_interactive_mainland_request_precedes_lower_priority_waiters(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        blocker = coordinator.acquire(
            request_key="active-request",
            upstream_service_id="upstream:eastmoney",
            owner_id="active-process",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        low_waiter = coordinator.acquire(
            request_key="incremental-request",
            upstream_service_id="upstream:eastmoney",
            owner_id="maintenance-process",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        )
        high_waiter = coordinator.acquire(
            request_key="interactive-request",
            upstream_service_id="upstream:eastmoney",
            owner_id="foreground-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=2),
            lease_duration=timedelta(seconds=30),
        )
        coordinator.release(blocker.lease)
        low_after_release = coordinator.acquire(
            request_key="incremental-request",
            upstream_service_id="upstream:eastmoney",
            owner_id="maintenance-process",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now + timedelta(seconds=3),
            lease_duration=timedelta(seconds=30),
        )
        high_after_release = coordinator.acquire(
            request_key="interactive-request",
            upstream_service_id="upstream:eastmoney",
            owner_id="foreground-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=3),
            lease_duration=timedelta(seconds=30),
        )

    assert low_waiter.disposition is LeaseDisposition.UPSTREAM_BUSY
    assert high_waiter.disposition is LeaseDisposition.UPSTREAM_BUSY
    assert low_after_release.disposition is LeaseDisposition.WAITING_FOR_PRIORITY
    assert low_after_release.active_request_key == "interactive-request"
    assert high_after_release.disposition is LeaseDisposition.ACQUIRED


@pytest.mark.unit
def test_background_prewarming_is_disabled_without_operator_safety_ceiling(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")

        decision = coordinator.acquire(
            request_key="prewarm:BTC-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="prewarm-process",
            priority=RequestPriority.CONFIGURED_PREWARMING,
            now=now,
            lease_duration=timedelta(seconds=30),
        )

    assert decision.disposition is LeaseDisposition.PREWARMING_DISABLED
    assert decision.lease is None


@pytest.mark.unit
def test_operator_pacing_ceiling_is_persisted_and_shared_across_processes(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        ceiling = coordinator.configure_operator_ceiling(
            upstream_service_id="upstream:yahoo",
            minimum_interval=timedelta(seconds=10),
            allow_prewarming=True,
            configured_at=now,
        )
        first = coordinator.acquire(
            request_key="interactive:BTC-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="foreground-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        coordinator.record_physical_attempt(first.lease, occurred_at=now)
        coordinator.release(first.lease)

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        paced = coordinator.acquire(
            request_key="prewarm:ETH-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="prewarm-process",
            priority=RequestPriority.CONFIGURED_PREWARMING,
            now=now + timedelta(seconds=5),
            lease_duration=timedelta(seconds=30),
        )
        after_interval = coordinator.acquire(
            request_key="prewarm:ETH-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="prewarm-process",
            priority=RequestPriority.CONFIGURED_PREWARMING,
            now=now + timedelta(seconds=10),
            lease_duration=timedelta(seconds=30),
        )

    assert ceiling.policy_source == "operator_policy"
    assert ceiling.minimum_interval == timedelta(seconds=10)
    assert ceiling.allow_prewarming is True
    assert paced.disposition is LeaseDisposition.PACING
    assert paced.cooldown_until == now + timedelta(seconds=10)
    assert after_interval.disposition is LeaseDisposition.ACQUIRED


@pytest.mark.unit
def test_identical_request_single_flight_precedes_operator_capacity_checks(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        coordinator.configure_operator_ceiling(
            upstream_service_id="upstream:yahoo",
            minimum_interval=timedelta(seconds=10),
            allow_prewarming=False,
            configured_at=now,
        )
        first = coordinator.acquire(
            request_key="history:BTC-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        coordinator.record_physical_attempt(first.lease, occurred_at=now)

        duplicate = coordinator.acquire(
            request_key="history:BTC-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="process-b",
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        )

    assert duplicate.disposition is LeaseDisposition.DUPLICATE_IN_FLIGHT
    assert duplicate.active_request_key == "history:BTC-USD"


@pytest.mark.unit
def test_yahoo_retry_sequence_persists_one_typed_event_per_physical_attempt(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls: list[int] = []

    def physical_attempt(attempt_index: int) -> str:
        transport_calls.append(attempt_index)
        if attempt_index < 4:
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.DISCONNECT,
                retryable=True,
            )
        return "available-frame"

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")

        result = coordinator.execute_retry_sequence(
            request_key="history:BTC-USD:2026-07",
            upstream_service_id="upstream:yahoo",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            max_physical_attempts=4,
            operation="market-snapshot",
            physical_attempt=physical_attempt,
        )

    with MarketHistoryStore.open(config) as store:
        persisted = ProviderRequestCoordinator(store).physical_attempt_events(
            result.sequence_id
        )

    assert result.value == "available-frame"
    assert transport_calls == [1, 2, 3, 4]
    assert result.physical_attempt_count == 4
    assert [event.attempt_index for event in persisted] == [1, 2, 3, 4]
    assert [event.outcome for event in persisted] == [
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.DISCONNECT,
        PhysicalAttemptOutcome.AVAILABLE,
    ]
    assert all(event.upstream_service_id == "upstream:yahoo" for event in persisted)
    assert all(event.pacing_event == "permit_acquired" for event in persisted)
    assert all(event.final_physical_attempt_count == 4 for event in persisted)


@pytest.mark.unit
def test_yahoo_retry_budget_exhaustion_stops_at_the_exact_physical_cap(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls: list[int] = []

    def disconnected(attempt_index: int) -> None:
        transport_calls.append(attempt_index)
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.DISCONNECT,
            retryable=True,
        )

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        with pytest.raises(PhysicalAttemptBudgetExhausted) as captured:
            coordinator.execute_retry_sequence(
                request_key="history:BTC-USD:exhausted",
                upstream_service_id="upstream:yahoo",
                owner_id="process-a",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                max_physical_attempts=3,
                operation="market-snapshot",
                physical_attempt=disconnected,
            )

    assert transport_calls == [1, 2, 3]
    assert captured.value.physical_attempt_count == 3
    assert [event.attempt_index for event in captured.value.attempt_events] == [1, 2, 3]
    assert all(
        event.final_physical_attempt_count == 3
        for event in captured.value.attempt_events
    )


@pytest.mark.unit
def test_yahoo_429_retry_after_persists_cooldown_and_blocks_the_next_process(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls = 0

    def rate_limited(_attempt_index: int) -> None:
        nonlocal transport_calls
        transport_calls += 1
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            status_code=429,
            error_code="YAHOO_HTTP_429",
            retry_after_seconds=60,
        )

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        with pytest.raises(PhysicalAttemptBudgetExhausted) as captured:
            coordinator.execute_retry_sequence(
                request_key="history:BTC-USD:rate-limited",
                upstream_service_id="upstream:yahoo",
                owner_id="process-a",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                max_physical_attempts=4,
                operation="market-snapshot",
                physical_attempt=rate_limited,
                cooldown_scope="market-snapshot",
            )

    with MarketHistoryStore.open(config) as store:
        blocked = ProviderRequestCoordinator(store).acquire(
            request_key="history:ETH-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="process-b",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="market-snapshot",
        )

    event = captured.value.attempt_events[0]
    assert transport_calls == 1
    assert captured.value.failure.outcome is PhysicalAttemptOutcome.RATE_LIMITED
    assert event.outcome is PhysicalAttemptOutcome.RATE_LIMITED
    assert event.cooldown_changed is True
    assert event.cooldown_until == now + timedelta(seconds=60)
    assert blocked.disposition is LeaseDisposition.COOLDOWN
    assert blocked.cooldown_until == now + timedelta(seconds=60)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outcome", "retryable"),
    (
        (PhysicalAttemptOutcome.DISCONNECT, True),
        (PhysicalAttemptOutcome.EMPTY_FRAME, True),
        (PhysicalAttemptOutcome.AUTHENTICATION, False),
        (PhysicalAttemptOutcome.MALFORMED_RESPONSE, False),
        (PhysicalAttemptOutcome.PROVIDER_ERROR, False),
    ),
)
def test_yahoo_non_capacity_physical_failures_retain_distinct_types(
    tmp_path,
    outcome: PhysicalAttemptOutcome,
    retryable: bool,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)

    def fail(_attempt_index: int) -> None:
        raise PhysicalAttemptFailure(outcome=outcome, retryable=retryable)

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        with pytest.raises(PhysicalAttemptBudgetExhausted) as captured:
            coordinator.execute_retry_sequence(
                request_key=f"history:BTC-USD:{outcome.value}",
                upstream_service_id="upstream:yahoo",
                owner_id="process-a",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                max_physical_attempts=1,
                operation="market-snapshot",
                physical_attempt=fail,
            )

    assert captured.value.failure.outcome is outcome
    assert captured.value.attempt_events[0].outcome is outcome


@pytest.mark.unit
def test_identical_yahoo_callers_share_the_complete_retry_sequence(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    first_attempt_started = Event()
    release_first_attempt = Event()
    calls_lock = Lock()
    transport_calls: list[int] = []
    follower_ready = Event()

    with MarketHistoryStore.open(config) as store:
        ProviderRequestCoordinator(store).register_upstream_service(
            "upstream:yahoo", "Yahoo Finance"
        )

    def physical_attempt(attempt_index: int) -> str:
        with calls_lock:
            transport_calls.append(attempt_index)
        if attempt_index == 1:
            first_attempt_started.set()
            assert release_first_attempt.wait(timeout=5)
        if attempt_index < 4:
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.DISCONNECT,
                retryable=True,
            )
        return "available-frame"

    def acquire(owner_id: str):
        with MarketHistoryStore.open(config) as store:
            if owner_id == "process-b":
                follower_ready.set()
            return ProviderRequestCoordinator(store).execute_retry_sequence(
                request_key="history:BTC-USD:single-flight",
                upstream_service_id="upstream:yahoo",
                owner_id=owner_id,
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                max_physical_attempts=4,
                operation="market-snapshot",
                physical_attempt=physical_attempt,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(acquire, "process-a")
        assert first_attempt_started.wait(timeout=5)
        follower = executor.submit(acquire, "process-b")
        assert follower_ready.wait(timeout=5)
        release_first_attempt.set()
        results = (leader.result(timeout=5), follower.result(timeout=5))

    assert transport_calls == [1, 2, 3, 4]
    assert [result.value for result in results] == [
        "available-frame",
        "available-frame",
    ]
    assert results[0].sequence_id == results[1].sequence_id
    assert results[0].physical_attempt_count == results[1].physical_attempt_count == 4


@pytest.mark.unit
def test_attempt_audit_migration_preserves_existing_coordinator_state(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        coordinator.configure_operator_ceiling(
            upstream_service_id="upstream:yahoo",
            minimum_interval=timedelta(seconds=10),
            allow_prewarming=False,
            configured_at=now,
        )
        coordinator.record_rate_limit(
            upstream_service_id="upstream:yahoo",
            cooldown_scope="market-snapshot",
            observed_at=now,
            retry_after=timedelta(seconds=60),
            provider_code="YAHOO_HTTP_429",
        )
        original_ceiling = store._connection.execute(
            "SELECT operator_ceiling_json FROM upstream_services "
            "WHERE upstream_service_id = 'upstream:yahoo'"
        ).fetchone()

    with sqlite3.connect(config.database_path) as connection:
        connection.execute("DROP TABLE provider_request_attempts")
        connection.execute("DROP TABLE provider_request_sequences")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 5")

    with MarketHistoryStore.open(config) as upgraded:
        migrated_version = upgraded._connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        preserved_ceiling = upgraded._connection.execute(
            "SELECT operator_ceiling_json FROM upstream_services "
            "WHERE upstream_service_id = 'upstream:yahoo'"
        ).fetchone()
        blocked = ProviderRequestCoordinator(upgraded).acquire(
            request_key="history:ETH-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="process-b",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="market-snapshot",
        )

    assert migrated_version == (7,)
    assert preserved_ceiling == original_ceiling
    assert blocked.disposition is LeaseDisposition.COOLDOWN
    assert blocked.cooldown_until == now + timedelta(seconds=60)


@pytest.mark.unit
def test_dedicated_authority_imports_existing_primary_cooldown_and_ceiling(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as primary:
        coordinator = ProviderRequestCoordinator(primary)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        coordinator.configure_operator_ceiling(
            upstream_service_id="upstream:yahoo",
            minimum_interval=timedelta(seconds=10),
            allow_prewarming=False,
            configured_at=now,
        )
        coordinator.record_rate_limit(
            upstream_service_id="upstream:yahoo",
            cooldown_scope="market-snapshot",
            observed_at=now,
            retry_after=timedelta(seconds=60),
            provider_code="YAHOO_HTTP_429",
        )

    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        coordinator = ProviderRequestCoordinator(authority)
        blocked = coordinator.acquire(
            request_key="history:ETH-USD",
            upstream_service_id="upstream:yahoo",
            owner_id="process-b",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="market-snapshot",
        )
        ceiling = authority._connection.execute(
            "SELECT operator_ceiling_json FROM upstream_services "
            "WHERE upstream_service_id = 'upstream:yahoo'"
        ).fetchone()
        imports = authority._connection.execute(
            "SELECT COUNT(*) FROM provider_request_state_imports"
        ).fetchone()

    assert blocked.disposition is LeaseDisposition.COOLDOWN
    assert blocked.cooldown_until == now + timedelta(seconds=60)
    assert ceiling is not None and ceiling[0] is not None
    assert imports == (1,)


@pytest.mark.unit
def test_dedicated_authority_fails_closed_for_corrupt_existing_primary(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.database_path.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(
        ProviderRequestAuthorityUnavailableError,
        match="legacy provider-request state",
    ):
        MarketHistoryStore.open_provider_request_authority(config)


@pytest.mark.unit
def test_dedicated_authority_fails_closed_for_conflicting_legacy_request_state(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as primary:
        coordinator = ProviderRequestCoordinator(primary)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        lease = coordinator.acquire(
            request_key="history:BTC-USD:conflict",
            upstream_service_id="upstream:yahoo",
            owner_id="legacy-owner",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=timedelta(minutes=2),
        ).lease
        assert lease is not None
        sequence_id = coordinator._sequence_id(lease)
        coordinator._create_retry_sequence(sequence_id, lease)

    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        authority._connection.execute(
            "DELETE FROM provider_request_state_imports"
        )
        authority._connection.execute(
            "UPDATE provider_request_sequences SET owner_id = 'different-owner' "
            "WHERE sequence_id = ?",
            (sequence_id,),
        )

    with pytest.raises(
        ProviderRequestAuthorityUnavailableError,
        match="legacy provider-request state",
    ):
        MarketHistoryStore.open_provider_request_authority(config)


@pytest.mark.unit
def test_deferred_attempt_aborted_before_io_finishes_with_zero_physical_attempts(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")

        with pytest.raises(PhysicalAttemptBudgetExhausted) as captured:
            coordinator.execute_retry_sequence(
                request_key="history:BTC-USD:pre-io-abort",
                upstream_service_id="upstream:yahoo",
                owner_id="process-a",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                max_physical_attempts=4,
                operation="market-snapshot",
                physical_attempt=lambda _attempt_index: (_ for _ in ()).throw(
                    RuntimeError("aborted before transport")
                ),
                record_at_physical_io=True,
            )
        sequence = store._connection.execute(
            "SELECT status, final_physical_attempt_count, failure_error_code "
            "FROM provider_request_sequences"
        ).fetchone()

    assert captured.value.attempt_events == ()
    assert sequence == ("failed", 0, "unhandled_transport_exception")


@pytest.mark.unit
def test_retry_sequence_renews_lease_while_waiting_for_operator_pacing(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    started_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    elapsed_seconds = 0.0
    sleeps: list[float] = []
    calls: list[int] = []

    def clock() -> datetime:
        return started_at + timedelta(seconds=elapsed_seconds)

    def sleep(seconds: float) -> None:
        nonlocal elapsed_seconds
        sleeps.append(seconds)
        elapsed_seconds += seconds

    def physical_attempt(attempt_index: int) -> str:
        calls.append(attempt_index)
        if attempt_index == 1:
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.DISCONNECT,
                retryable=True,
            )
        return "available"

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        coordinator.configure_operator_ceiling(
            upstream_service_id="upstream:yahoo",
            minimum_interval=timedelta(minutes=3),
            allow_prewarming=False,
            configured_at=started_at,
        )
        result = coordinator.execute_retry_sequence(
            request_key="history:BTC-USD:paced-retry",
            upstream_service_id="upstream:yahoo",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=clock,
            sleep=sleep,
            lease_duration=timedelta(minutes=2),
            max_physical_attempts=2,
            operation="market-snapshot",
            physical_attempt=physical_attempt,
        )

    assert calls == [1, 2]
    assert sum(sleeps) == 180
    assert max(sleeps) <= 60
    assert result.physical_attempt_count == 2
    assert result.attempt_events[1].pacing_event == "paced_then_permit_acquired"
    assert result.attempt_events[1].pacing_wait_seconds == 180


@pytest.mark.unit
def test_identical_yahoo_requests_single_flight_across_spawned_processes(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    with MarketHistoryStore.open(config) as store:
        ProviderRequestCoordinator(store).register_upstream_service(
            "upstream:yahoo",
            "Yahoo Finance",
        )

    context = get_context("spawn")
    first_attempt_started = context.Event()
    release_first_attempt = context.Event()
    follower_observed_duplicate = context.Event()
    transport_calls = context.Queue()
    results = context.Queue()
    leader = context.Process(
        target=_multiprocess_retry_caller,
        args=(
            config,
            "leader",
            first_attempt_started,
            release_first_attempt,
            follower_observed_duplicate,
            transport_calls,
            results,
        ),
    )
    follower = context.Process(
        target=_multiprocess_retry_caller,
        args=(
            config,
            "follower",
            first_attempt_started,
            release_first_attempt,
            follower_observed_duplicate,
            transport_calls,
            results,
        ),
    )

    started_processes = []
    try:
        leader.start()
        started_processes.append(leader)
        assert first_attempt_started.wait(timeout=60)
        follower.start()
        started_processes.append(follower)
        assert follower_observed_duplicate.wait(timeout=60)
        release_first_attempt.set()
        leader.join(timeout=60)
        follower.join(timeout=60)
        assert not leader.is_alive()
        assert not follower.is_alive()
        assert leader.exitcode == 0
        assert follower.exitcode == 0

        observed_calls = [transport_calls.get(timeout=2) for _ in range(4)]
        observed_results = sorted(
            (results.get(timeout=2) for _ in range(2)),
            key=lambda item: item[0],
        )
    finally:
        release_first_attempt.set()
        for process in started_processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=60)

    assert observed_calls == [
        ("leader", 1),
        ("leader", 2),
        ("leader", 3),
        ("leader", 4),
    ]
    assert [item[1:3] for item in observed_results] == [
        ("ok", "available-frame"),
        ("ok", "available-frame"),
    ]
    assert observed_results[0][3] == observed_results[1][3]
    assert observed_results[0][4] == observed_results[1][4] == 4


@pytest.mark.unit
def test_expired_retry_owner_recovers_started_attempt_into_closed_audit_event(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        acquired = coordinator.acquire(
            request_key="history:BTC-USD:abandoned",
            upstream_service_id="upstream:yahoo",
            owner_id="crashed-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=timedelta(seconds=5),
        )
        assert acquired.lease is not None
        sequence_id = coordinator._sequence_id(acquired.lease)
        coordinator._start_attempt_event(
            sequence_id=sequence_id,
            lease=acquired.lease,
            service_name="Yahoo Finance",
            operation="market-snapshot",
            attempt_index=1,
            attempted_at=now,
            pacing_event="permit_acquired",
            pacing_wait_seconds=0,
        )

        recovered = coordinator.acquire(
            request_key="history:ETH-USD:replacement",
            upstream_service_id="upstream:yahoo",
            owner_id="replacement-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=5),
            lease_duration=timedelta(seconds=5),
        )
        events = coordinator.physical_attempt_events(sequence_id)

    assert recovered.disposition is LeaseDisposition.ACQUIRED
    assert len(events) == 1
    assert events[0].outcome is PhysicalAttemptOutcome.PROVIDER_ERROR
    assert events[0].error_code == "coordinator_lease_expired"
    assert events[0].final_physical_attempt_count == 1


@pytest.mark.unit
def test_expired_retry_owner_recovers_sequence_before_its_first_attempt(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        acquired = coordinator.acquire(
            request_key="history:BTC-USD:zero-attempt-crash",
            upstream_service_id="upstream:yahoo",
            owner_id="crashed-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now,
            lease_duration=timedelta(seconds=5),
        )
        assert acquired.lease is not None
        sequence_id = coordinator._sequence_id(acquired.lease)
        coordinator._create_retry_sequence(sequence_id, acquired.lease)

        recovered = coordinator.acquire(
            request_key="history:ETH-USD:replacement",
            upstream_service_id="upstream:yahoo",
            owner_id="replacement-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=5),
            lease_duration=timedelta(seconds=5),
        )
        sequence = store._connection.execute(
            "SELECT status, final_physical_attempt_count, failure_error_code "
            "FROM provider_request_sequences WHERE sequence_id = ?",
            (sequence_id,),
        ).fetchone()

    assert recovered.disposition is LeaseDisposition.ACQUIRED
    assert sequence == ("failed", 0, "coordinator_lease_expired")


@pytest.mark.unit
def test_unsupported_handoff_result_closes_attempt_sequence_and_lease(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)

    class UnsupportedResult:
        pass

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:yahoo", "Yahoo Finance")
        with pytest.raises(TypeError, match="unsupported coordinated result type"):
            coordinator.execute_retry_sequence(
                request_key="history:BTC-USD:unsupported-result",
                upstream_service_id="upstream:yahoo",
                owner_id="process-a",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                max_physical_attempts=1,
                operation="market-snapshot",
                physical_attempt=lambda _attempt: UnsupportedResult(),
            )
        sequence = store._connection.execute(
            "SELECT status, final_physical_attempt_count, failure_error_code "
            "FROM provider_request_sequences"
        ).fetchone()
        lease_count = store._connection.execute(
            "SELECT COUNT(*) FROM request_leases"
        ).fetchone()
        sequence_id = str(
            store._connection.execute(
                "SELECT sequence_id FROM provider_request_sequences"
            ).fetchone()[0]
        )
        events = coordinator.physical_attempt_events(sequence_id)

    assert sequence == ("failed", 1, "coordinator_sequence_abandoned")
    assert lease_count == (0,)
    assert events[0].outcome is PhysicalAttemptOutcome.PROVIDER_ERROR
    assert events[0].final_physical_attempt_count == 1

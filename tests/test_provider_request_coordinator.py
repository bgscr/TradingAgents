from __future__ import annotations

import json
import os
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
    PhysicalAttemptNotMade,
    PhysicalAttemptOutcome,
    ProviderRequestAuthorityUnavailableError,
    ProviderRequestCoordinator,
    RateLimitScope,
    RequestPriority,
    upstream_service_identity_for_provider,
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


@pytest.mark.unit
def test_tushare_attempt_persists_account_identity_and_endpoint_capacity_scope(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    config = _config(tmp_path)
    upstream_service_id, service_name = upstream_service_identity_for_provider(
        "tushare",
        account_scope="personal-research-primary",
    )

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            upstream_service_id,
            service_name,
            account_scope="personal-research-primary",
        )
        result = coordinator.execute_direct_physical_request(
            request_key="provider-subrequest:v1:tushare-income",
            upstream_service_id=upstream_service_id,
            owner_id="ticket-03-test",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="financial-statement:income",
            physical_request=lambda: "immutable-income-artifact",
            cooldown_scope="income",
        )
        persisted = store._connection.execute(
            "SELECT capacity_scope FROM provider_request_attempts "
            "WHERE sequence_id = ?",
            (result.sequence_id,),
        ).fetchone()

    assert result.physical_attempt_count == 1
    assert result.attempt_events[0].upstream_service_id == upstream_service_id
    assert result.attempt_events[0].capacity_scope == "income"
    assert persisted == ("income",)


@pytest.mark.unit
def test_tushare_global_and_endpoint_cooldowns_have_distinct_scope(tmp_path) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    config = _config(tmp_path)
    upstream_service_id, service_name = upstream_service_identity_for_provider(
        "tushare",
        account_scope="personal-research-primary",
    )

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            upstream_service_id,
            service_name,
            account_scope="personal-research-primary",
        )
        coordinator.record_rate_limit(
            upstream_service_id=upstream_service_id,
            cooldown_scope="all",
            observed_at=now,
            retry_after=timedelta(seconds=60),
            provider_code="TUSHARE_GLOBAL_THROTTLE",
        )
        global_income = coordinator.acquire(
            request_key="income-during-global",
            upstream_service_id=upstream_service_id,
            owner_id="income-global",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="income",
        )
        global_balance = coordinator.acquire(
            request_key="balance-during-global",
            upstream_service_id=upstream_service_id,
            owner_id="balance-global",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="balancesheet",
        )

        endpoint_observed_at = now + timedelta(seconds=61)
        coordinator.record_rate_limit(
            upstream_service_id=upstream_service_id,
            cooldown_scope="income",
            observed_at=endpoint_observed_at,
            retry_after=timedelta(seconds=30),
            provider_code="TUSHARE_ENDPOINT_THROTTLE",
        )
        endpoint_income = coordinator.acquire(
            request_key="income-during-endpoint",
            upstream_service_id=upstream_service_id,
            owner_id="income-endpoint",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=endpoint_observed_at + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="income",
        )
        endpoint_balance = coordinator.acquire(
            request_key="balance-during-endpoint",
            upstream_service_id=upstream_service_id,
            owner_id="balance-endpoint",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=endpoint_observed_at + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="balancesheet",
        )
        coordinator.release(endpoint_balance.lease)

    assert global_income.disposition is LeaseDisposition.COOLDOWN
    assert global_balance.disposition is LeaseDisposition.COOLDOWN
    assert endpoint_income.disposition is LeaseDisposition.COOLDOWN
    assert endpoint_balance.disposition is LeaseDisposition.ACQUIRED


@pytest.mark.unit
def test_physical_account_rate_limit_blocks_every_tushare_endpoint(tmp_path) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    upstream_service_id, service_name = upstream_service_identity_for_provider(
        "tushare",
        account_scope="personal-research-primary",
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            upstream_service_id,
            service_name,
            account_scope="personal-research-primary",
        )

        def account_throttled() -> None:
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.RATE_LIMITED,
                retryable=True,
                retry_after_seconds=60,
                rate_limit_scope=RateLimitScope.UPSTREAM,
            )

        with pytest.raises(PhysicalAttemptBudgetExhausted) as exc_info:
            coordinator.execute_direct_physical_request(
                request_key="provider-subrequest:v1:tushare-income-global-limit",
                upstream_service_id=upstream_service_id,
                owner_id="ticket-03-global-limit",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="financial-statement:income",
                physical_request=account_throttled,
                cooldown_scope="income",
            )
        blocked = coordinator.acquire(
            request_key="balancesheet-after-global-limit",
            upstream_service_id=upstream_service_id,
            owner_id="ticket-03-global-limit-follower",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            cooldown_scope="balancesheet",
        )
        cooldown_scopes = tuple(
            row[0]
            for row in store._connection.execute(
                "SELECT cooldown_scope FROM request_cooldowns"
            )
        )

    assert blocked.disposition is LeaseDisposition.COOLDOWN
    assert cooldown_scopes == ("all",)
    assert exc_info.value.attempt_events[0].capacity_scope == "income"
    assert exc_info.value.attempt_events[0].cooldown_changed is True


@pytest.mark.unit
@pytest.mark.parametrize("unsafe_scope", ["income endpoint", "x" * 129])
def test_coordinator_rejects_unsafe_capacity_scope_before_persistence(
    tmp_path,
    unsafe_scope: str,
) -> None:
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    config = _config(tmp_path)
    upstream_service_id, service_name = upstream_service_identity_for_provider(
        "tushare",
        account_scope="personal-research-primary",
    )

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            upstream_service_id,
            service_name,
            account_scope="personal-research-primary",
        )

        with pytest.raises(ValueError, match="bounded capacity-scope token"):
            coordinator.acquire(
                request_key="unsafe-capacity-scope",
                upstream_service_id=upstream_service_id,
                owner_id="ticket-03-test",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=now,
                lease_duration=timedelta(seconds=30),
                cooldown_scope=unsafe_scope,
            )
        with pytest.raises(ValueError, match="bounded capacity-scope token"):
            coordinator.record_rate_limit(
                upstream_service_id=upstream_service_id,
                cooldown_scope=unsafe_scope,
                observed_at=now,
                retry_after=timedelta(seconds=30),
                provider_code="TUSHARE_ENDPOINT_THROTTLE",
            )
        with pytest.raises(ValueError, match="bounded provider-code token"):
            coordinator.record_rate_limit(
                upstream_service_id=upstream_service_id,
                cooldown_scope="income",
                observed_at=now,
                retry_after=timedelta(seconds=30),
                provider_code="RAW token=must-not-persist",
            )

        assert store._connection.execute(
            "SELECT COUNT(*) FROM request_leases"
        ).fetchone() == (0,)
        assert store._connection.execute(
            "SELECT COUNT(*) FROM request_cooldowns"
        ).fetchone() == (0,)


@pytest.mark.unit
def test_tushare_account_scope_rejects_credential_like_text_before_persistence(
    tmp_path,
) -> None:
    unsafe_scope = "token-must-not-persist-or-echo"

    with pytest.raises(ValueError) as identity_error:
        upstream_service_identity_for_provider(
            "tushare",
            account_scope=unsafe_scope,
        )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        with pytest.raises(ValueError) as registration_error:
            ProviderRequestCoordinator(store).register_upstream_service(
                "upstream:tushare-test",
                "Tushare Pro account",
                account_scope=unsafe_scope,
            )
        persisted = store._connection.execute(
            "SELECT COUNT(*) FROM upstream_services"
        ).fetchone()

    assert unsafe_scope not in str(identity_error.value)
    assert unsafe_scope not in str(registration_error.value)
    assert persisted == (0,)


@pytest.mark.unit
def test_service_registration_rejects_account_scope_identity_mismatch(tmp_path) -> None:
    upstream_service_id, service_name = upstream_service_identity_for_provider(
        "tushare",
        account_scope="personal-research-primary",
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service(
            upstream_service_id,
            service_name,
            account_scope="personal-research-primary",
        )
        with pytest.raises(ValueError, match="account scope mismatch"):
            coordinator.register_upstream_service(
                upstream_service_id,
                service_name,
                account_scope="different-research-account",
            )
        persisted = store._connection.execute(
            "SELECT account_scope FROM upstream_services "
            "WHERE upstream_service_id = ?",
            (upstream_service_id,),
        ).fetchone()

    assert persisted == ("personal-research-primary",)


@pytest.mark.unit
def test_physical_failure_rejects_secret_like_error_code_without_echo() -> None:
    unsafe_code = "token:must-not-persist"

    with pytest.raises(ValueError) as exc_info:
        PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
            retryable=False,
            error_code=unsafe_code,
        )

    assert unsafe_code not in str(exc_info.value)


@pytest.mark.unit
def test_endpoint_scope_migration_preserves_legacy_rows_as_all_without_io(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    physical_calls = 0

    def physical_request() -> str:
        nonlocal physical_calls
        physical_calls += 1
        return "legacy-artifact"

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:legacy", "Legacy service")
        original = coordinator.execute_direct_physical_request(
            request_key="legacy-request",
            upstream_service_id="upstream:legacy",
            owner_id="legacy-owner",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="legacy-operation",
            physical_request=physical_request,
        )
        legacy_attempt = store._connection.execute(
            "SELECT sequence_id, attempt_index, request_key, upstream_service_id, "
            "outcome, final_physical_attempt_count FROM provider_request_attempts"
        ).fetchone()
        retained_lease = coordinator.acquire(
            request_key="legacy-active-lease",
            upstream_service_id="upstream:legacy",
            owner_id="legacy-active-owner",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(minutes=5),
            cooldown_scope="legacy-endpoint",
        )
        coordinator.record_rate_limit(
            upstream_service_id="upstream:legacy",
            cooldown_scope="legacy-endpoint",
            observed_at=now + timedelta(seconds=1),
            retry_after=timedelta(minutes=1),
            provider_code="LEGACY_THROTTLE",
        )
        legacy_lease = store._connection.execute(
            "SELECT request_key, upstream_service_id, owner_id, priority, "
            "acquired_at, expires_at FROM request_leases"
        ).fetchone()
        legacy_cooldown = store._connection.execute(
            "SELECT upstream_service_id, cooldown_scope, cooldown_until, reason, "
            "retry_after_seconds, updated_at FROM request_cooldowns"
        ).fetchone()

    _downgrade_endpoint_scope_schema(config.database_path)

    with MarketHistoryStore.open(config) as migrated:
        migrated_attempt = migrated._connection.execute(
            "SELECT sequence_id, attempt_index, request_key, upstream_service_id, "
            "outcome, final_physical_attempt_count, capacity_scope "
            "FROM provider_request_attempts"
        ).fetchone()
        projected = ProviderRequestCoordinator(migrated).physical_attempt_events(
            original.sequence_id
        )
        migrated_lease = migrated._connection.execute(
            "SELECT request_key, upstream_service_id, owner_id, priority, "
            "acquired_at, expires_at, capacity_scope FROM request_leases"
        ).fetchone()
        migrated_cooldown = migrated._connection.execute(
            "SELECT upstream_service_id, cooldown_scope, cooldown_until, reason, "
            "retry_after_seconds, updated_at FROM request_cooldowns"
        ).fetchone()

    assert physical_calls == 1
    assert retained_lease.disposition is LeaseDisposition.ACQUIRED
    assert migrated_attempt[:-1] == legacy_attempt
    assert migrated_attempt[-1] is None
    assert migrated_lease[:-1] == legacy_lease
    assert migrated_lease[-1] is None
    assert migrated_cooldown == legacy_cooldown
    assert projected[0].capacity_scope == "all"


def _downgrade_attempt_event_identity_schema(database_path) -> None:
    """Convert a test database from schema v8 to the exact v7 attempt shape."""

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE provider_request_attempts DROP COLUMN terminal_outcome_kind"
        )
        connection.execute(
            "ALTER TABLE provider_request_attempts DROP COLUMN capacity_scope"
        )
        connection.execute(
            "ALTER TABLE provider_request_sequences DROP COLUMN failure_outcome_kind"
        )
        connection.execute(
            "ALTER TABLE provider_request_sequences DROP COLUMN capacity_scope"
        )
        connection.execute("ALTER TABLE request_leases DROP COLUMN capacity_scope")
        connection.execute("DROP INDEX provider_request_attempts_by_event_id")
        connection.execute(
            "ALTER TABLE provider_request_attempts DROP COLUMN terminal_outcome"
        )
        connection.execute(
            "ALTER TABLE provider_request_attempts DROP COLUMN attempt_event_id"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version >= 8")


def _downgrade_endpoint_scope_schema(database_path) -> None:
    """Convert a test database from schema v9 to the exact v8 coordinator shape."""

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE provider_request_attempts DROP COLUMN terminal_outcome_kind"
        )
        connection.execute(
            "ALTER TABLE provider_request_attempts DROP COLUMN capacity_scope"
        )
        connection.execute(
            "ALTER TABLE provider_request_sequences DROP COLUMN failure_outcome_kind"
        )
        connection.execute(
            "ALTER TABLE provider_request_sequences DROP COLUMN capacity_scope"
        )
        connection.execute("ALTER TABLE request_leases DROP COLUMN capacity_scope")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")


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


def _interrupt_direct_physical_request_caller(
    config: MarketHistoryConfig,
    request_started,
    transport_calls,
) -> None:
    """Spawn-safe worker that is terminated after its typed attempt starts."""

    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)

    def physical_request() -> str:
        with transport_calls.get_lock():
            transport_calls.value += 1
        request_started.set()
        os._exit(0)

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        coordinator.execute_direct_physical_request(
            request_key="history:600519.SS:direct-interrupted",
            upstream_service_id="upstream:eastmoney",
            owner_id="crashed-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=5),
            operation="current-market-frame",
            physical_request=physical_request,
        )


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
def test_direct_physical_request_success_persists_one_terminal_attempt(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls = 0

    def physical_request() -> str:
        nonlocal transport_calls
        transport_calls += 1
        return "available-frame"

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")

        result = coordinator.execute_direct_physical_request(
            request_key="history:600519.SS:direct-success",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="current-market-frame",
            physical_request=physical_request,
        )
        persisted = coordinator.physical_attempt_events(result.sequence_id)
        diagnostic_detail = store._connection.execute(
            "SELECT detail FROM history_store_diagnostics "
            "WHERE operation = 'provider_request' AND code = 'physical_attempt'"
        ).fetchone()

    assert transport_calls == 1
    assert result.value == "available-frame"
    assert result.physical_attempt_count == 1
    assert result.attempt_events == persisted
    assert len(persisted) == 1
    event = persisted[0]
    assert event.attempt_event_id.startswith("provider-physical-attempt=sha256:")
    assert event.attempt_index == 1
    assert event.outcome is PhysicalAttemptOutcome.AVAILABLE
    assert event.final_physical_attempt_count == 1
    assert diagnostic_detail is not None
    assert json.loads(str(diagnostic_detail[0]))["attempt_event_id"] == (
        event.attempt_event_id
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outcome", "retryable"),
    (
        (PhysicalAttemptOutcome.RATE_LIMITED, True),
        (PhysicalAttemptOutcome.TIMEOUT, True),
        (PhysicalAttemptOutcome.PROVIDER_ERROR, False),
        (PhysicalAttemptOutcome.EMPTY_FRAME, False),
        (PhysicalAttemptOutcome.MALFORMED_RESPONSE, False),
        (PhysicalAttemptOutcome.AUTHENTICATION, False),
        (PhysicalAttemptOutcome.DISCONNECT, True),
    ),
)
def test_direct_physical_request_persists_each_terminal_failure_type(
    tmp_path,
    outcome: PhysicalAttemptOutcome,
    retryable: bool,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls = 0

    def physical_request() -> None:
        nonlocal transport_calls
        transport_calls += 1
        raise PhysicalAttemptFailure(
            outcome=outcome,
            retryable=retryable,
            status_code=429 if outcome is PhysicalAttemptOutcome.RATE_LIMITED else None,
            error_code=f"EASTMONEY_{outcome.value.upper()}",
            retry_after_seconds=(
                75 if outcome is PhysicalAttemptOutcome.RATE_LIMITED else None
            ),
        )

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        with pytest.raises(PhysicalAttemptBudgetExhausted) as captured:
            coordinator.execute_direct_physical_request(
                request_key=f"history:600519.SS:direct-{outcome.value}",
                upstream_service_id="upstream:eastmoney",
                owner_id="process-a",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="current-market-frame",
                physical_request=physical_request,
                cooldown_scope="market-snapshot",
            )

    failure = captured.value.failure
    events = captured.value.attempt_events
    assert transport_calls == 1
    assert captured.value.physical_attempt_count == 1
    assert failure.outcome is outcome
    assert len(events) == 1
    assert events[0].outcome is outcome
    assert events[0].retryable is retryable
    assert events[0].status_code == failure.status_code
    assert events[0].error_code == failure.error_code
    assert events[0].retry_after_seconds == failure.retry_after_seconds
    assert events[0].final_physical_attempt_count == 1
    if outcome is PhysicalAttemptOutcome.RATE_LIMITED:
        assert events[0].cooldown_changed is True
        assert events[0].cooldown_until == now + timedelta(seconds=75)
    else:
        assert events[0].cooldown_changed is False
        assert events[0].cooldown_until is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "invalid_metadata",
    (
        {"status_code": 99},
        {"error_code": "raw provider secret with spaces"},
        {"retry_after_seconds": float("inf")},
    ),
)
def test_direct_physical_attempt_bounds_failure_metadata(
    tmp_path,
    invalid_metadata: dict[str, object],
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls = 0

    def physical_request() -> None:
        nonlocal transport_calls
        transport_calls += 1
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
            retryable=False,
            **invalid_metadata,
        )

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        with pytest.raises(PhysicalAttemptBudgetExhausted) as captured:
            coordinator.execute_direct_physical_request(
                request_key="history:600519.SS:invalid-failure-metadata",
                upstream_service_id="upstream:eastmoney",
                owner_id="process-a",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="current-market-frame",
                physical_request=physical_request,
            )

    event = captured.value.attempt_events[0]
    assert transport_calls == 1
    assert event.outcome is PhysicalAttemptOutcome.PROVIDER_ERROR
    assert event.status_code is None
    assert event.error_code == "unhandled_transport_exception"
    assert event.retry_after_seconds is None


@pytest.mark.unit
def test_n_direct_physical_requests_persist_exactly_n_attempt_events(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls: list[int] = []

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        results = []
        for request_index in range(1, 4):
            results.append(
                coordinator.execute_direct_physical_request(
                    request_key=f"history:600519.SS:direct-{request_index}",
                    upstream_service_id="upstream:eastmoney",
                    owner_id=f"process-{request_index}",
                    priority=RequestPriority.INTERACTIVE_MAINLAND,
                    now=lambda: now,
                    sleep=lambda _seconds: None,
                    lease_duration=timedelta(seconds=30),
                    operation="current-market-frame",
                    physical_request=lambda index=request_index: (
                        transport_calls.append(index) or f"frame-{index}"
                    ),
                )
            )
        persisted_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert transport_calls == [1, 2, 3]
    assert persisted_count == (3,)
    assert sum(result.physical_attempt_count for result in results) == 3
    assert len({result.attempt_events[0].attempt_event_id for result in results}) == 3


@pytest.mark.unit
@pytest.mark.parametrize("disposition", ("cache_hit", "circuit_open"))
def test_direct_physical_attempt_no_io_result_persists_zero_events(
    tmp_path,
    disposition: str,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls = 0

    def no_io_result() -> PhysicalAttemptNotMade[str]:
        return PhysicalAttemptNotMade(disposition)

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        result = coordinator.execute_direct_physical_request(
            request_key=f"history:600519.SS:{disposition}",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="current-market-frame",
            physical_request=no_io_result,
        )
        persisted_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert transport_calls == 0
    assert result.value == disposition
    assert result.physical_attempt_count == 0
    assert result.attempt_events == ()
    assert persisted_count == (0,)


@pytest.mark.unit
@pytest.mark.parametrize(
    "blocked_by",
    ("cooldown", "operator_policy"),
)
def test_direct_physical_attempt_pre_io_skip_persists_zero_events(
    tmp_path,
    blocked_by: str,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls = 0

    def physical_request() -> str:
        nonlocal transport_calls
        transport_calls += 1
        return "must-not-run"

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        priority = RequestPriority.INTERACTIVE_MAINLAND
        if blocked_by == "cooldown":
            coordinator.record_rate_limit(
                upstream_service_id="upstream:eastmoney",
                cooldown_scope="market-snapshot",
                observed_at=now,
                retry_after=timedelta(seconds=60),
                provider_code="EASTMONEY_HTTP_429",
            )
        else:
            priority = RequestPriority.CONFIGURED_PREWARMING
        with pytest.raises(PhysicalAttemptBudgetExhausted) as captured:
            coordinator.execute_direct_physical_request(
                request_key=f"history:600519.SS:{blocked_by}",
                upstream_service_id="upstream:eastmoney",
                owner_id="process-a",
                priority=priority,
                now=lambda: now + timedelta(seconds=1),
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="current-market-frame",
                physical_request=physical_request,
                cooldown_scope="market-snapshot",
            )
        persisted_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert transport_calls == 0
    assert captured.value.physical_attempt_count == 0
    assert captured.value.attempt_events == ()
    assert persisted_count == (0,)


@pytest.mark.unit
def test_direct_physical_attempt_single_flight_follower_adds_no_event(tmp_path) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    request_started = Event()
    release_request = Event()
    follower_observed_duplicate = Event()
    transport_calls = 0

    class CrossProcessLikeCoordinator(ProviderRequestCoordinator):
        _single_flight_lock = Lock()
        _single_flights = {}

        def acquire(self, **kwargs):
            decision = super().acquire(**kwargs)
            if decision.disposition is LeaseDisposition.DUPLICATE_IN_FLIGHT:
                follower_observed_duplicate.set()
            return decision

    with MarketHistoryStore.open(config) as store:
        ProviderRequestCoordinator(store).register_upstream_service(
            "upstream:eastmoney",
            "Eastmoney push2his",
        )

    def invoke(owner_id: str):
        nonlocal transport_calls

        def physical_request() -> str:
            nonlocal transport_calls
            transport_calls += 1
            request_started.set()
            assert release_request.wait(timeout=5)
            return "available-frame"

        with MarketHistoryStore.open(config) as store:
            coordinator_type = (
                ProviderRequestCoordinator
                if owner_id == "process-a"
                else CrossProcessLikeCoordinator
            )
            return coordinator_type(store).execute_direct_physical_request(
                request_key="history:600519.SS:direct-single-flight",
                upstream_service_id="upstream:eastmoney",
                owner_id=owner_id,
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: now,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="current-market-frame",
                physical_request=physical_request,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(invoke, "process-a")
        assert request_started.wait(timeout=5)
        follower = executor.submit(invoke, "process-b")
        assert follower_observed_duplicate.wait(timeout=5)
        release_request.set()
        results = (leader.result(timeout=5), follower.result(timeout=5))

    with MarketHistoryStore.open(config) as store:
        persisted_count = store._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()

    assert transport_calls == 1
    assert persisted_count == (1,)
    assert results[0].sequence_id == results[1].sequence_id
    assert results[0].attempt_events == results[1].attempt_events


@pytest.mark.unit
def test_direct_physical_attempt_persists_initial_pacing_wait(tmp_path) -> None:
    config = _config(tmp_path)
    started_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    elapsed_seconds = 0.0
    sleeps: list[float] = []
    transport_calls = 0

    def clock() -> datetime:
        return started_at + timedelta(seconds=elapsed_seconds)

    def sleep(seconds: float) -> None:
        nonlocal elapsed_seconds
        sleeps.append(seconds)
        elapsed_seconds += seconds

    def physical_request() -> str:
        nonlocal transport_calls
        transport_calls += 1
        return "available-frame"

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        coordinator.configure_operator_ceiling(
            upstream_service_id="upstream:eastmoney",
            minimum_interval=timedelta(seconds=10),
            allow_prewarming=False,
            configured_at=started_at,
        )
        coordinator.execute_direct_physical_request(
            request_key="history:600519.SS:first-direct",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=clock,
            sleep=sleep,
            lease_duration=timedelta(seconds=30),
            operation="current-market-frame",
            physical_request=physical_request,
        )
        elapsed_seconds = 1
        paced = coordinator.execute_direct_physical_request(
            request_key="history:000001.SZ:paced-direct",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-b",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=clock,
            sleep=sleep,
            lease_duration=timedelta(seconds=30),
            operation="current-market-frame",
            physical_request=physical_request,
        )

    assert transport_calls == 2
    assert sleeps == [9]
    assert paced.physical_attempt_count == 1
    assert paced.attempt_events[0].pacing_event == "paced_then_permit_acquired"
    assert paced.attempt_events[0].pacing_wait_seconds == 9


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

    assert migrated_version == (9,)
    assert preserved_ceiling == original_ceiling
    assert blocked.disposition is LeaseDisposition.COOLDOWN
    assert blocked.cooldown_until == now + timedelta(seconds=60)


@pytest.mark.unit
def test_direct_attempt_migration_preserves_typed_rows_and_diagnostic_history(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls = 0

    def physical_request() -> str:
        nonlocal transport_calls
        transport_calls += 1
        return "available-frame"

    with MarketHistoryStore.open(config) as store:
        coordinator = ProviderRequestCoordinator(store)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        original = coordinator.execute_direct_physical_request(
            request_key="history:600519.SS:pre-v8-typed",
            upstream_service_id="upstream:eastmoney",
            owner_id="process-a",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="current-market-frame",
            physical_request=physical_request,
        )
        diagnostic_lease = coordinator.acquire(
            request_key="history:000001.SZ:diagnostic-only",
            upstream_service_id="upstream:eastmoney",
            owner_id="legacy-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ).lease
        coordinator.record_physical_attempt(
            diagnostic_lease,
            occurred_at=now + timedelta(seconds=1),
            attempt_key="legacy-diagnostic-only",
        )
        coordinator.release(diagnostic_lease)
        original_attempt = store._connection.execute(
            "SELECT sequence_id, attempt_index, request_key, upstream_service_id, "
            "upstream_service_name, owner_id, priority, operation, attempted_at, "
            "pacing_event, pacing_wait_seconds, outcome, retryable, status_code, "
            "error_code, retry_after_seconds, cooldown_changed, cooldown_until, "
            "final_physical_attempt_count FROM provider_request_attempts"
        ).fetchone()
        original_diagnostics = tuple(
            store._connection.execute(
                "SELECT diagnostic_id, occurred_at, code, detail "
                "FROM history_store_diagnostics "
                "WHERE operation = 'provider_request' ORDER BY diagnostic_id"
            )
        )

    _downgrade_attempt_event_identity_schema(config.database_path)

    with MarketHistoryStore.open(config) as upgraded:
        migrated_version = upgraded._connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        migrated_attempt = upgraded._connection.execute(
            "SELECT sequence_id, attempt_index, request_key, upstream_service_id, "
            "upstream_service_name, owner_id, priority, operation, attempted_at, "
            "pacing_event, pacing_wait_seconds, outcome, retryable, status_code, "
            "error_code, retry_after_seconds, cooldown_changed, cooldown_until, "
            "final_physical_attempt_count FROM provider_request_attempts"
        ).fetchone()
        stored_event_identity = upgraded._connection.execute(
            "SELECT attempt_event_id FROM provider_request_attempts"
        ).fetchone()
        migrated_diagnostics = tuple(
            upgraded._connection.execute(
                "SELECT diagnostic_id, occurred_at, code, detail "
                "FROM history_store_diagnostics "
                "WHERE operation = 'provider_request' ORDER BY diagnostic_id"
            )
        )
        typed_count = upgraded._connection.execute(
            "SELECT COUNT(*) FROM provider_request_attempts"
        ).fetchone()
        projected = ProviderRequestCoordinator(upgraded).physical_attempt_events(
            original.sequence_id
        )

    assert transport_calls == 1
    assert migrated_version == (9,)
    assert migrated_attempt == original_attempt
    assert stored_event_identity == (None,)
    assert migrated_diagnostics == original_diagnostics
    assert typed_count == (1,)
    assert len(projected) == 1
    assert projected[0].attempt_event_id == original.attempt_events[0].attempt_event_id


@pytest.mark.unit
def test_direct_attempt_identity_migration_is_failure_atomic(
    tmp_path,
    monkeypatch,
) -> None:
    import tradingagents.market_history.store as store_module

    config = _config(tmp_path)
    with MarketHistoryStore.open(config):
        pass
    _downgrade_attempt_event_identity_schema(config.database_path)
    valid_alter = store_module.MIGRATION_V8[0]
    monkeypatch.setattr(
        store_module,
        "MIGRATION_V8",
        (valid_alter, "INVALID MIGRATION STATEMENT"),
    )

    with pytest.raises(sqlite3.DatabaseError):
        MarketHistoryStore.open(config)

    with sqlite3.connect(config.database_path) as connection:
        migrated_version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        attempt_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(provider_request_attempts)")
        }

    assert migrated_version == (7,)
    assert "attempt_event_id" not in attempt_columns
    assert "terminal_outcome" not in attempt_columns


@pytest.mark.unit
def test_endpoint_scope_migration_is_failure_atomic(tmp_path, monkeypatch) -> None:
    import tradingagents.market_history.store as store_module

    config = _config(tmp_path)
    with MarketHistoryStore.open(config):
        pass
    _downgrade_endpoint_scope_schema(config.database_path)
    original_migration = store_module.MIGRATION_V9
    monkeypatch.setattr(
        store_module,
        "MIGRATION_V9",
        (
            original_migration[0],
            "INVALID ENDPOINT SCOPE MIGRATION",
            *original_migration[2:],
        ),
    )

    with pytest.raises(sqlite3.DatabaseError):
        MarketHistoryStore.open(config)

    with sqlite3.connect(config.database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        lease_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(request_leases)")
        }
        sequence_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(provider_request_sequences)"
            )
        }
        attempt_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(provider_request_attempts)"
            )
        }

    assert version == (8,)
    assert "capacity_scope" not in lease_columns
    assert "capacity_scope" not in sequence_columns
    assert "failure_outcome_kind" not in sequence_columns
    assert "capacity_scope" not in attempt_columns
    assert "terminal_outcome_kind" not in attempt_columns


@pytest.mark.unit
def test_direct_attempt_open_repairs_an_additive_v8_shape_without_provider_io(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    with MarketHistoryStore.open(config):
        pass
    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            "ALTER TABLE provider_request_attempts DROP COLUMN terminal_outcome"
        )
        version_before = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()

    with MarketHistoryStore.open(config) as repaired:
        columns_after = {
            str(row[1])
            for row in repaired._connection.execute(
                "PRAGMA table_info(provider_request_attempts)"
            )
        }
        version_after = repaired._connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()

    assert version_before == version_after == (9,)
    assert "attempt_event_id" in columns_after
    assert "terminal_outcome" in columns_after


@pytest.mark.unit
def test_dedicated_authority_imports_v7_typed_attempt_without_promoting_diagnostic(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    transport_calls = 0

    def physical_request() -> str:
        nonlocal transport_calls
        transport_calls += 1
        return "available-frame"

    with MarketHistoryStore.open(config) as primary:
        coordinator = ProviderRequestCoordinator(primary)
        coordinator.register_upstream_service("upstream:eastmoney", "Eastmoney push2his")
        original = coordinator.execute_direct_physical_request(
            request_key="history:600519.SS:legacy-primary",
            upstream_service_id="upstream:eastmoney",
            owner_id="legacy-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=lambda: now,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
            operation="current-market-frame",
            physical_request=physical_request,
        )
        lease = coordinator.acquire(
            request_key="history:000001.SZ:legacy-diagnostic",
            upstream_service_id="upstream:eastmoney",
            owner_id="diagnostic-process",
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        ).lease
        coordinator.record_physical_attempt(
            lease,
            occurred_at=now + timedelta(seconds=1),
            attempt_key="diagnostic-only",
        )
        coordinator.release(lease)

    _downgrade_attempt_event_identity_schema(config.database_path)

    with MarketHistoryStore.open_provider_request_authority(config) as authority:
        typed_rows = authority._connection.execute(
            "SELECT COUNT(*), attempt_event_id FROM provider_request_attempts"
        ).fetchone()
        diagnostic_rows = authority._connection.execute(
            "SELECT COUNT(*) FROM history_store_diagnostics "
            "WHERE operation = 'provider_request' AND code = 'physical_attempt'"
        ).fetchone()
        projected = ProviderRequestCoordinator(authority).physical_attempt_events(
            original.sequence_id
        )

    with sqlite3.connect(config.database_path) as legacy:
        legacy_version = legacy.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()

    assert transport_calls == 1
    assert typed_rows == (1, None)
    assert diagnostic_rows == (2,)
    assert len(projected) == 1
    assert projected[0].attempt_event_id == original.attempt_events[0].attempt_event_id
    assert legacy_version == (7,)


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
def test_interrupted_direct_physical_attempt_recovery_closes_original_event_once(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    context = get_context("spawn")
    request_started = context.Event()
    transport_calls = context.Value("i", 0)
    with MarketHistoryStore.open(config) as store:
        ProviderRequestCoordinator(store).register_upstream_service(
            "upstream:eastmoney",
            "Eastmoney push2his",
        )
    worker = context.Process(
        target=_interrupt_direct_physical_request_caller,
        args=(config, request_started, transport_calls),
    )

    worker.start()
    try:
        assert request_started.wait(timeout=60)
        worker.join(timeout=60)
        assert not worker.is_alive()
        assert worker.exitcode == 0
        with MarketHistoryStore.open(config) as store:
            started_row = store._connection.execute(
                "SELECT sequence_id, attempt_event_id, outcome, terminal_outcome "
                "FROM provider_request_attempts"
            ).fetchone()
        assert started_row is not None
        assert started_row[2] == "started"

        with MarketHistoryStore.open(config) as store:
            coordinator = ProviderRequestCoordinator(store)
            recovered = coordinator.acquire(
                request_key="history:000001.SZ:replacement",
                upstream_service_id="upstream:eastmoney",
                owner_id="replacement-process",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=now + timedelta(seconds=5),
                lease_duration=timedelta(seconds=5),
            )
            events = coordinator.physical_attempt_events(str(started_row[0]))
            terminal_row = store._connection.execute(
                "SELECT outcome, terminal_outcome FROM provider_request_attempts "
                "WHERE sequence_id = ?",
                (str(started_row[0]),),
            ).fetchone()
            row_count = store._connection.execute(
                "SELECT COUNT(*) FROM provider_request_attempts"
            ).fetchone()
            coordinator.release(recovered.lease)
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(timeout=60)

    assert transport_calls.value == 1
    assert recovered.disposition is LeaseDisposition.ACQUIRED
    assert row_count == (1,)
    assert len(events) == 1
    assert events[0].attempt_event_id == started_row[1]
    assert terminal_row == ("provider_error", "abandoned")
    assert events[0].outcome is PhysicalAttemptOutcome.ABANDONED
    assert events[0].error_code == "coordinator_lease_expired"
    assert events[0].final_physical_attempt_count == 1


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
    assert events[0].outcome is PhysicalAttemptOutcome.ABANDONED
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
    assert events[0].outcome is PhysicalAttemptOutcome.ABANDONED
    assert events[0].final_physical_attempt_count == 1

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Generic, TypeVar

from tradingagents.market_history.result_codec import (
    decode_coordinated_result,
    encode_coordinated_result,
)

if TYPE_CHECKING:
    from tradingagents.market_history.store import MarketHistoryStore


T = TypeVar("T")


_ACTIVE_PHYSICAL_ATTEMPT_IO_RECORDER: ContextVar[Callable[[], None] | None] = (
    ContextVar("active_physical_attempt_io_recorder", default=None)
)


def record_active_physical_attempt_io() -> None:
    """Persist the active coordinator attempt immediately before transport I/O."""

    recorder = _ACTIVE_PHYSICAL_ATTEMPT_IO_RECORDER.get()
    if recorder is None:
        raise RuntimeError("Yahoo transport I/O has no active coordinator permit")
    recorder()


_UPSTREAM_SERVICES = {
    "akshare": ("eastmoney-push2his", "Eastmoney push2his"),
    "baostock": ("baostock-tcp", "BaoStock TCP service"),
    "yfinance": ("yahoo-finance", "Yahoo Finance"),
}


def _as_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def upstream_service_identity_for_provider(provider: str) -> tuple[str, str]:
    service_key, service_name = _UPSTREAM_SERVICES.get(
        provider,
        (f"provider-{provider}", provider),
    )
    digest = sha256()
    for component in (service_key,):
        encoded = component.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"upstream-service=sha256:{digest.hexdigest()}", service_name


class RequestPriority(IntEnum):
    INTERACTIVE_MAINLAND = 0
    INCREMENTAL_REFRESH = 1
    CONFIGURED_PREWARMING = 2
    RECONCILIATION = 3


class PhysicalAttemptOutcome(str, Enum):
    STARTED = "started"
    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    DISCONNECT = "disconnect"
    EMPTY_FRAME = "empty_frame"
    AUTHENTICATION = "authentication"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_ERROR = "provider_error"
    UPSTREAM_BUSY = "upstream_busy"


class PhysicalAttemptFailure(Exception):
    """Typed result of exactly one physical provider transport attempt."""

    def __init__(
        self,
        *,
        outcome: PhysicalAttemptOutcome,
        retryable: bool,
        status_code: int | None = None,
        error_code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        if outcome in {PhysicalAttemptOutcome.STARTED, PhysicalAttemptOutcome.AVAILABLE}:
            raise ValueError("a physical-attempt failure requires a failure outcome")
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("Retry-After must be nonnegative")
        self.outcome = outcome
        self.retryable = retryable
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(outcome.value)


@dataclass(frozen=True)
class ProviderPhysicalAttemptEvent:
    sequence_id: str
    request_key: str
    upstream_service_id: str
    upstream_service_name: str
    attempt_index: int
    attempted_at: datetime
    pacing_event: str
    pacing_wait_seconds: float
    outcome: PhysicalAttemptOutcome
    retryable: bool
    status_code: int | None
    error_code: str | None
    retry_after_seconds: float | None
    cooldown_changed: bool
    cooldown_until: datetime | None
    final_physical_attempt_count: int | None


@dataclass(frozen=True)
class CoordinatedRequestResult(Generic[T]):
    value: T
    sequence_id: str
    physical_attempt_count: int
    attempt_events: tuple[ProviderPhysicalAttemptEvent, ...]


@dataclass(frozen=True)
class PhysicalAttemptNotMade(Generic[T]):
    """A permitted high-level call completed from cache without network I/O."""

    value: T


class PhysicalAttemptBudgetExhausted(Exception):
    """A coordinated sequence completed without an available transport result."""

    def __init__(
        self,
        *,
        sequence_id: str,
        failure: PhysicalAttemptFailure,
        attempt_events: tuple[ProviderPhysicalAttemptEvent, ...],
    ) -> None:
        self.sequence_id = sequence_id
        self.failure = failure
        self.attempt_events = attempt_events
        self.physical_attempt_count = len(attempt_events)
        super().__init__(failure.outcome.value)


class LeaseDisposition(str, Enum):
    ACQUIRED = "acquired"
    DUPLICATE_IN_FLIGHT = "duplicate_in_flight"
    UPSTREAM_BUSY = "upstream_busy"
    COOLDOWN = "cooldown"
    WAITING_FOR_PRIORITY = "waiting_for_priority"
    PREWARMING_DISABLED = "prewarming_disabled"
    PACING = "pacing"
    OPERATOR_POLICY_INVALID = "operator_policy_invalid"


@dataclass(frozen=True)
class RequestLease:
    request_key: str
    upstream_service_id: str
    owner_id: str
    priority: RequestPriority
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class LeaseDecision:
    disposition: LeaseDisposition
    lease: RequestLease | None = None
    cooldown_until: datetime | None = None
    active_request_key: str | None = None
    active_owner_id: str | None = None


@dataclass(frozen=True)
class CooldownRecord:
    upstream_service_id: str
    cooldown_scope: str
    cooldown_until: datetime
    reason: str
    retry_after_seconds: float | None
    updated_at: datetime


@dataclass(frozen=True)
class OperatorSafetyCeiling:
    upstream_service_id: str
    policy_source: str
    minimum_interval: timedelta
    allow_prewarming: bool
    configured_at: datetime


@dataclass(frozen=True)
class ProviderRequestOperationalSummary:
    physical_attempts: int
    rate_limit_events: int
    active_cooldowns: int
    active_leases: int
    queued_requests: int
    latest_diagnostic_at: datetime | None


class ProviderRequestCoordinator:
    """Cross-process SQLite lease and cooldown authority for provider requests."""

    def __init__(self, store: MarketHistoryStore) -> None:
        self._store = store

    def register_upstream_service(
        self,
        upstream_service_id: str,
        service_name: str,
        *,
        account_scope: str = "",
    ) -> None:
        if not upstream_service_id.strip() or not service_name.strip():
            raise ValueError("upstream service identity and name must not be blank")
        self._store._connection.execute(
            "INSERT OR IGNORE INTO upstream_services "
            "(upstream_service_id, service_name, account_scope, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                upstream_service_id,
                service_name,
                account_scope,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def configure_operator_ceiling(
        self,
        *,
        upstream_service_id: str,
        minimum_interval: timedelta,
        allow_prewarming: bool,
        configured_at: datetime,
    ) -> OperatorSafetyCeiling:
        if not upstream_service_id.strip():
            raise ValueError("upstream service identity must not be blank")
        if minimum_interval <= timedelta(0):
            raise ValueError("operator minimum interval must be positive")
        if not isinstance(allow_prewarming, bool):
            raise TypeError("allow_prewarming must be a boolean")
        configured_at = _as_utc(configured_at, label="operator-ceiling time")
        ceiling = OperatorSafetyCeiling(
            upstream_service_id=upstream_service_id,
            policy_source="operator_policy",
            minimum_interval=minimum_interval,
            allow_prewarming=allow_prewarming,
            configured_at=configured_at,
        )
        payload = {
            "schema_version": "1.0",
            "policy_source": ceiling.policy_source,
            "minimum_interval_seconds": minimum_interval.total_seconds(),
            "allow_prewarming": allow_prewarming,
            "configured_at": configured_at.isoformat(),
        }
        cursor = self._store._connection.execute(
            "UPDATE upstream_services SET operator_ceiling_json = ? "
            "WHERE upstream_service_id = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                upstream_service_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("upstream service must be registered before configuration")
        return ceiling

    @staticmethod
    def _parse_operator_ceiling(
        upstream_service_id: str,
        raw_value: object,
    ) -> OperatorSafetyCeiling:
        try:
            payload = json.loads(str(raw_value))
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "policy_source",
                "minimum_interval_seconds",
                "allow_prewarming",
                "configured_at",
            }:
                raise ValueError
            if (
                payload["schema_version"] != "1.0"
                or payload["policy_source"] != "operator_policy"
                or not isinstance(payload["allow_prewarming"], bool)
                or isinstance(payload["minimum_interval_seconds"], bool)
            ):
                raise ValueError
            minimum_interval = timedelta(
                seconds=float(payload["minimum_interval_seconds"])
            )
            configured_at = _as_utc(
                datetime.fromisoformat(str(payload["configured_at"])),
                label="operator-ceiling time",
            )
            if minimum_interval <= timedelta(0) or configured_at.tzinfo is None:
                raise ValueError
        except (OverflowError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("operator safety ceiling is malformed") from exc
        return OperatorSafetyCeiling(
            upstream_service_id=upstream_service_id,
            policy_source="operator_policy",
            minimum_interval=minimum_interval,
            allow_prewarming=payload["allow_prewarming"],
            configured_at=configured_at,
        )

    def _latest_physical_attempt_at(
        self,
        upstream_service_id: str,
    ) -> datetime | None:
        attempts: list[datetime] = []
        for occurred_at, raw_detail in self._store._connection.execute(
            "SELECT occurred_at, detail FROM history_store_diagnostics "
            "WHERE operation = 'provider_request' AND code = 'physical_attempt'"
        ):
            try:
                detail = json.loads(str(raw_detail))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(detail, dict):
                continue
            if detail.get("upstream_service_id") == upstream_service_id:
                try:
                    attempt_at = datetime.fromisoformat(str(occurred_at))
                except ValueError:
                    continue
                if attempt_at.tzinfo is not None:
                    attempts.append(attempt_at.astimezone(timezone.utc))
        return max(attempts) if attempts else None

    def _recover_expired_retry_sequences(self, now: datetime) -> None:
        """Close durable attempt rows left behind by an expired request owner."""

        expired = tuple(
            self._store._connection.execute(
                "SELECT request_key, upstream_service_id, owner_id "
                "FROM request_leases WHERE expires_at <= ?",
                (now.isoformat(),),
            )
        )
        for request_key, upstream_service_id, owner_id in expired:
            sequence_ids = {
                str(row[0])
                for row in self._store._connection.execute(
                    "SELECT sequence_id FROM provider_request_sequences "
                    "WHERE request_key = ? AND upstream_service_id = ? "
                    "AND owner_id = ? AND status = 'running'",
                    (request_key, upstream_service_id, owner_id),
                )
            }
            sequence_ids.update(
                str(row[0])
                for row in self._store._connection.execute(
                    "SELECT DISTINCT sequence_id FROM provider_request_attempts "
                    "WHERE request_key = ? AND upstream_service_id = ? "
                    "AND owner_id = ? AND final_physical_attempt_count IS NULL",
                    (request_key, upstream_service_id, owner_id),
                )
            )
            for sequence_id in sequence_ids:
                self._close_running_sequence(
                    sequence_id=sequence_id,
                    completed_at=now,
                    error_code="coordinator_lease_expired",
                )

    def acquire(
        self,
        *,
        request_key: str,
        upstream_service_id: str,
        owner_id: str,
        priority: RequestPriority,
        now: datetime,
        lease_duration: timedelta,
        cooldown_scope: str = "all",
    ) -> LeaseDecision:
        if not request_key.strip() or not upstream_service_id.strip() or not owner_id.strip():
            raise ValueError("request, upstream, and owner identities must not be blank")
        if not isinstance(priority, RequestPriority):
            raise TypeError("priority must be a RequestPriority")
        now = _as_utc(now, label="coordinator time")
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        expires_at = now + lease_duration
        connection = self._store._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._recover_expired_retry_sequences(now)
            connection.execute(
                "DELETE FROM request_leases WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            connection.execute(
                "DELETE FROM request_queue WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            active = connection.execute(
                "SELECT request_key, owner_id FROM request_leases "
                "WHERE upstream_service_id = ? ORDER BY acquired_at LIMIT 1",
                (upstream_service_id,),
            ).fetchone()
            active_key = str(active[0]) if active is not None else None
            active_owner_id = str(active[1]) if active is not None else None
            if active_key == request_key:
                connection.commit()
                return LeaseDecision(
                    LeaseDisposition.DUPLICATE_IN_FLIGHT,
                    active_request_key=active_key,
                    active_owner_id=active_owner_id,
                )
            ceiling_row = connection.execute(
                "SELECT operator_ceiling_json FROM upstream_services "
                "WHERE upstream_service_id = ?",
                (upstream_service_id,),
            ).fetchone()
            raw_ceiling = ceiling_row[0] if ceiling_row is not None else None
            ceiling = None
            if raw_ceiling is not None:
                try:
                    ceiling = self._parse_operator_ceiling(
                        upstream_service_id,
                        raw_ceiling,
                    )
                except ValueError:
                    connection.commit()
                    return LeaseDecision(LeaseDisposition.OPERATOR_POLICY_INVALID)
            if priority is RequestPriority.CONFIGURED_PREWARMING and (
                ceiling is None or not ceiling.allow_prewarming
            ):
                connection.commit()
                return LeaseDecision(LeaseDisposition.PREWARMING_DISABLED)
            cooldown = connection.execute(
                "SELECT cooldown_until FROM request_cooldowns "
                "WHERE upstream_service_id = ? AND cooldown_scope IN (?, 'all') "
                "AND cooldown_until > ? ORDER BY cooldown_until DESC LIMIT 1",
                (upstream_service_id, cooldown_scope, now.isoformat()),
            ).fetchone()
            if cooldown is not None:
                connection.commit()
                return LeaseDecision(
                    LeaseDisposition.COOLDOWN,
                    cooldown_until=datetime.fromisoformat(str(cooldown[0])),
                )
            if ceiling is not None:
                latest_attempt = self._latest_physical_attempt_at(
                    upstream_service_id
                )
                if latest_attempt is not None:
                    next_allowed_at = latest_attempt + ceiling.minimum_interval
                    if next_allowed_at > now:
                        connection.commit()
                        return LeaseDecision(
                            LeaseDisposition.PACING,
                            cooldown_until=next_allowed_at,
                        )
            if active_key is not None:
                self._enqueue(
                    request_key=request_key,
                    upstream_service_id=upstream_service_id,
                    priority=priority,
                    now=now,
                )
                connection.commit()
                return LeaseDecision(
                    LeaseDisposition.UPSTREAM_BUSY,
                    active_request_key=active_key,
                    active_owner_id=active_owner_id,
                )
            self._enqueue(
                request_key=request_key,
                upstream_service_id=upstream_service_id,
                priority=priority,
                now=now,
            )
            selected = connection.execute(
                "SELECT request_key FROM request_queue WHERE upstream_service_id = ? "
                "ORDER BY priority, first_enqueued_at, request_key LIMIT 1",
                (upstream_service_id,),
            ).fetchone()
            assert selected is not None
            selected_key = str(selected[0])
            if selected_key != request_key:
                connection.commit()
                return LeaseDecision(
                    LeaseDisposition.WAITING_FOR_PRIORITY,
                    active_request_key=selected_key,
                )
            lease = RequestLease(
                request_key=request_key,
                upstream_service_id=upstream_service_id,
                owner_id=owner_id,
                priority=priority,
                acquired_at=now,
                expires_at=expires_at,
            )
            connection.execute(
                "DELETE FROM request_queue WHERE request_key = ?",
                (request_key,),
            )
            connection.execute(
                "INSERT INTO request_leases "
                "(request_key, upstream_service_id, owner_id, priority, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    lease.request_key,
                    lease.upstream_service_id,
                    lease.owner_id,
                    int(lease.priority),
                    lease.acquired_at.isoformat(),
                    lease.expires_at.isoformat(),
                ),
            )
            connection.commit()
            return LeaseDecision(LeaseDisposition.ACQUIRED, lease=lease)
        except BaseException:
            connection.rollback()
            raise

    def _enqueue(
        self,
        *,
        request_key: str,
        upstream_service_id: str,
        priority: RequestPriority,
        now: datetime,
    ) -> None:
        expires_at = now + timedelta(minutes=5)
        self._store._connection.execute(
            "INSERT INTO request_queue "
            "(request_key, upstream_service_id, priority, first_enqueued_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(request_key) DO UPDATE SET "
            "priority = MIN(request_queue.priority, excluded.priority), "
            "updated_at = excluded.updated_at, expires_at = excluded.expires_at",
            (
                request_key,
                upstream_service_id,
                int(priority),
                now.isoformat(),
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )

    def record_rate_limit(
        self,
        *,
        upstream_service_id: str,
        cooldown_scope: str,
        observed_at: datetime,
        retry_after: timedelta,
        provider_code: str,
    ) -> CooldownRecord:
        observed_at = _as_utc(
            observed_at,
            label="rate-limit observation time",
        )
        if retry_after <= timedelta(0):
            raise ValueError("rate-limit cooldown must be positive")
        if not cooldown_scope.strip() or not provider_code.strip():
            raise ValueError("cooldown scope and provider code must not be blank")
        candidate_until = observed_at + retry_after
        retry_after_seconds = retry_after.total_seconds()
        self._store._connection.execute(
            "INSERT INTO request_cooldowns "
            "(upstream_service_id, cooldown_scope, cooldown_until, reason, "
            "retry_after_seconds, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(upstream_service_id, cooldown_scope) DO UPDATE SET "
            "cooldown_until = CASE "
            "WHEN excluded.cooldown_until > request_cooldowns.cooldown_until "
            "THEN excluded.cooldown_until ELSE request_cooldowns.cooldown_until END, "
            "reason = excluded.reason, retry_after_seconds = excluded.retry_after_seconds, "
            "updated_at = excluded.updated_at",
            (
                upstream_service_id,
                cooldown_scope,
                candidate_until.isoformat(),
                f"rate_limited:{provider_code}",
                retry_after_seconds,
                observed_at.isoformat(),
            ),
        )
        row = self._store._connection.execute(
            "SELECT cooldown_until, reason, retry_after_seconds, updated_at "
            "FROM request_cooldowns WHERE upstream_service_id = ? AND cooldown_scope = ?",
            (upstream_service_id, cooldown_scope),
        ).fetchone()
        assert row is not None
        self._record_diagnostic(
            occurred_at=observed_at,
            severity="warning",
            code="rate_limited",
            identity_parts=(
                upstream_service_id,
                cooldown_scope,
                observed_at.isoformat(),
                provider_code,
            ),
            detail={
                "upstream_service_id": upstream_service_id,
                "cooldown_scope": cooldown_scope,
                "cooldown_until": str(row[0]),
                "provider_code": provider_code,
                "retry_after_seconds": retry_after_seconds,
            },
        )
        return CooldownRecord(
            upstream_service_id=upstream_service_id,
            cooldown_scope=cooldown_scope,
            cooldown_until=datetime.fromisoformat(str(row[0])),
            reason=str(row[1]),
            retry_after_seconds=float(row[2]) if row[2] is not None else None,
            updated_at=datetime.fromisoformat(str(row[3])),
        )

    def record_physical_attempt(
        self,
        lease: RequestLease | None,
        *,
        occurred_at: datetime,
        attempt_key: str = "",
    ) -> str:
        if lease is None:
            raise ValueError("an acquired request lease is required")
        if not isinstance(attempt_key, str):
            raise TypeError("physical-attempt key must be a string")
        occurred_at = _as_utc(
            occurred_at,
            label="physical-attempt time",
        )
        return self._record_diagnostic(
            occurred_at=occurred_at,
            severity="info",
            code="physical_attempt",
            identity_parts=(
                lease.request_key,
                lease.upstream_service_id,
                lease.owner_id,
                occurred_at.isoformat(),
                attempt_key,
            ),
            detail={
                "request_key": lease.request_key,
                "upstream_service_id": lease.upstream_service_id,
                "owner_id": lease.owner_id,
                "priority": int(lease.priority),
                "attempt_key": attempt_key,
            },
        )

    def execute_retry_sequence(
        self,
        *,
        request_key: str,
        upstream_service_id: str,
        owner_id: str,
        priority: RequestPriority,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
        max_physical_attempts: int,
        operation: str,
        physical_attempt: Callable[[int], T],
        cooldown_scope: str = "all",
        record_at_physical_io: bool = False,
    ) -> CoordinatedRequestResult[T]:
        """Run one single-flighted sequence whose permits map 1:1 to I/O calls."""
        if max_physical_attempts < 1:
            raise ValueError("physical-attempt budget must be positive")
        if not operation.strip():
            raise ValueError("physical-attempt operation must not be blank")
        flight_key = (
            str(self._store.config.database_path),
            upstream_service_id,
            request_key,
        )
        with self._single_flight_lock:
            shared = self._single_flights.get(flight_key)
            if shared is None:
                shared = Future()
                self._single_flights[flight_key] = shared
                leader = True
            else:
                leader = False
        if not leader:
            return shared.result()  # type: ignore[return-value]

        try:
            result = self._execute_retry_sequence_as_leader(
                request_key=request_key,
                upstream_service_id=upstream_service_id,
                owner_id=owner_id,
                priority=priority,
                now=now,
                sleep=sleep,
                lease_duration=lease_duration,
                max_physical_attempts=max_physical_attempts,
                operation=operation,
                physical_attempt=physical_attempt,
                cooldown_scope=cooldown_scope,
                record_at_physical_io=record_at_physical_io,
            )
        except BaseException as exc:
            shared.set_exception(exc)
            raise
        else:
            shared.set_result(result)
            return result
        finally:
            with self._single_flight_lock:
                if self._single_flights.get(flight_key) is shared:
                    del self._single_flights[flight_key]

    def _execute_retry_sequence_as_leader(
        self,
        *,
        request_key: str,
        upstream_service_id: str,
        owner_id: str,
        priority: RequestPriority,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
        max_physical_attempts: int,
        operation: str,
        physical_attempt: Callable[[int], T],
        cooldown_scope: str,
        record_at_physical_io: bool,
    ) -> CoordinatedRequestResult[T]:
        started_at = _as_utc(now(), label="coordinator time")
        decision = self.acquire(
            request_key=request_key,
            upstream_service_id=upstream_service_id,
            owner_id=owner_id,
            priority=priority,
            now=started_at,
            lease_duration=lease_duration,
            cooldown_scope=cooldown_scope,
        )
        if decision.disposition is LeaseDisposition.DUPLICATE_IN_FLIGHT:
            assert decision.active_owner_id is not None
            return self._wait_for_persisted_sequence(
                request_key=request_key,
                upstream_service_id=upstream_service_id,
                owner_id=decision.active_owner_id,
                now=now,
                sleep=sleep,
            )
        if decision.disposition is not LeaseDisposition.ACQUIRED:
            retry_after = (
                max(0.0, (decision.cooldown_until - started_at).total_seconds())
                if decision.cooldown_until is not None
                else None
            )
            outcome = (
                PhysicalAttemptOutcome.RATE_LIMITED
                if decision.disposition is LeaseDisposition.COOLDOWN
                else PhysicalAttemptOutcome.UPSTREAM_BUSY
            )
            raise PhysicalAttemptBudgetExhausted(
                sequence_id="",
                failure=PhysicalAttemptFailure(
                    outcome=outcome,
                    retryable=True,
                    retry_after_seconds=retry_after,
                ),
                attempt_events=(),
            )
        lease = decision.lease
        assert lease is not None
        sequence_id = self._sequence_id(lease)
        self._create_retry_sequence(sequence_id, lease)
        try:
            for attempt_index in range(1, max_physical_attempts + 1):
                requested_at = _as_utc(now(), label="physical-attempt time")
                earliest_at = self.earliest_physical_attempt_at(
                    lease,
                    requested_at=requested_at,
                )
                pacing_wait_seconds = max(
                    0.0,
                    (earliest_at - requested_at).total_seconds(),
                )
                attempted_at = requested_at
                while attempted_at < earliest_at:
                    renewed = self.renew(
                        lease,
                        now=attempted_at,
                        lease_duration=lease_duration,
                    )
                    if renewed is None:
                        failure = PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.UPSTREAM_BUSY,
                            retryable=True,
                        )
                        self._finalize_attempt_sequence(
                            sequence_id,
                            attempt_index - 1,
                        )
                        raise PhysicalAttemptBudgetExhausted(
                            sequence_id=sequence_id,
                            failure=failure,
                            attempt_events=self.physical_attempt_events(sequence_id),
                        )
                    lease = renewed
                    wait_seconds = min(
                        60.0,
                        lease_duration.total_seconds() / 2,
                        (earliest_at - attempted_at).total_seconds(),
                    )
                    sleep(wait_seconds)
                    attempted_at = max(
                        _as_utc(now(), label="physical-attempt time"),
                        attempted_at + timedelta(seconds=wait_seconds),
                    )
                attempted_at = max(attempted_at, earliest_at)
                renewed = self.renew(
                    lease,
                    now=attempted_at,
                    lease_duration=lease_duration,
                )
                if renewed is None:
                    failure = PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.UPSTREAM_BUSY,
                        retryable=True,
                    )
                    self._finalize_attempt_sequence(sequence_id, attempt_index - 1)
                    raise PhysicalAttemptBudgetExhausted(
                        sequence_id=sequence_id,
                        failure=failure,
                        attempt_events=self.physical_attempt_events(sequence_id),
                    )
                lease = renewed
                pacing_event = (
                    "paced_then_permit_acquired"
                    if pacing_wait_seconds
                    else "permit_acquired"
                )
                connection = self._store._connection
                attempt_recorded = False
                diagnostic_id: str | None = None

                def record_attempt_at_io_boundary(
                    _connection=connection,
                    _lease=lease,
                    _attempt_index=attempt_index,
                    _pacing_event=pacing_event,
                    _pacing_wait_seconds=pacing_wait_seconds,
                ) -> None:
                    nonlocal attempt_recorded, attempted_at, diagnostic_id
                    if attempt_recorded:
                        raise RuntimeError(
                            "one coordinator permit cannot record multiple physical attempts"
                        )
                    io_at = _as_utc(now(), label="physical-attempt time")
                    _connection.execute("BEGIN IMMEDIATE")
                    try:
                        self._start_attempt_event(
                            sequence_id=sequence_id,
                            lease=_lease,
                            service_name=self._service_name(upstream_service_id),
                            operation=operation,
                            attempt_index=_attempt_index,
                            attempted_at=io_at,
                            pacing_event=_pacing_event,
                            pacing_wait_seconds=_pacing_wait_seconds,
                        )
                        diagnostic_id = self.record_physical_attempt(
                            _lease,
                            occurred_at=io_at,
                            attempt_key=(
                                f"{sequence_id}:{_attempt_index}:{operation}"
                            ),
                        )
                        _connection.commit()
                    except BaseException:
                        _connection.rollback()
                        raise
                    attempted_at = io_at
                    attempt_recorded = True

                if not record_at_physical_io:
                    record_attempt_at_io_boundary()
                recorder_token = _ACTIVE_PHYSICAL_ATTEMPT_IO_RECORDER.set(
                    record_attempt_at_io_boundary if record_at_physical_io else None
                )
                try:
                    try:
                        value = physical_attempt(attempt_index)
                    finally:
                        _ACTIVE_PHYSICAL_ATTEMPT_IO_RECORDER.reset(recorder_token)
                except PhysicalAttemptFailure as failure:
                    completed_count = (
                        attempt_index if attempt_recorded else attempt_index - 1
                    )
                    cooldown_until = None
                    cooldown_changed = False
                    should_stop = (
                        not attempt_recorded
                        or not failure.retryable
                        or failure.outcome is PhysicalAttemptOutcome.RATE_LIMITED
                        or attempt_index == max_physical_attempts
                    )
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        if (
                            attempt_recorded
                            and
                            failure.outcome is PhysicalAttemptOutcome.RATE_LIMITED
                            and failure.retry_after_seconds is not None
                            and failure.retry_after_seconds > 0
                        ):
                            cooldown = self.record_rate_limit(
                                upstream_service_id=upstream_service_id,
                                cooldown_scope=cooldown_scope,
                                observed_at=attempted_at,
                                retry_after=timedelta(
                                    seconds=failure.retry_after_seconds
                                ),
                                provider_code=(
                                    failure.error_code
                                    or str(failure.status_code or "provider_capacity")
                                ),
                            )
                            cooldown_until = cooldown.cooldown_until
                            cooldown_changed = True
                        if attempt_recorded:
                            self._finish_attempt_event(
                                sequence_id=sequence_id,
                                attempt_index=attempt_index,
                                failure=failure,
                                cooldown_changed=cooldown_changed,
                                cooldown_until=cooldown_until,
                            )
                        if should_stop:
                            self._finalize_attempt_sequence(
                                sequence_id,
                                completed_count,
                            )
                            self._finish_retry_sequence_failure(
                                sequence_id=sequence_id,
                                completed_at=_as_utc(
                                    now(),
                                    label="coordinator time",
                                ),
                                final_count=completed_count,
                                failure=failure,
                            )
                            self._delete_lease(lease)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    if should_stop:
                        raise PhysicalAttemptBudgetExhausted(
                            sequence_id=sequence_id,
                            failure=failure,
                            attempt_events=self.physical_attempt_events(sequence_id),
                        ) from failure
                    continue
                except BaseException as exc:
                    failure = PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
                        retryable=False,
                        error_code="unhandled_transport_exception",
                    )
                    completed_count = (
                        attempt_index if attempt_recorded else attempt_index - 1
                    )
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        if attempt_recorded:
                            self._finish_attempt_event(
                                sequence_id=sequence_id,
                                attempt_index=attempt_index,
                                failure=failure,
                                cooldown_changed=False,
                                cooldown_until=None,
                            )
                        self._finalize_attempt_sequence(
                            sequence_id,
                            completed_count,
                        )
                        self._finish_retry_sequence_failure(
                            sequence_id=sequence_id,
                            completed_at=_as_utc(
                                now(),
                                label="coordinator time",
                            ),
                            final_count=completed_count,
                            failure=failure,
                        )
                        self._delete_lease(lease)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    raise PhysicalAttemptBudgetExhausted(
                        sequence_id=sequence_id,
                        failure=failure,
                        attempt_events=self.physical_attempt_events(sequence_id),
                    ) from exc
                if isinstance(value, PhysicalAttemptNotMade):
                    cached_value = value.value
                    result_payload = encode_coordinated_result(cached_value)
                    final_count = attempt_index - 1
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        if attempt_recorded:
                            if record_at_physical_io:
                                raise RuntimeError(
                                    "physical I/O was recorded for a no-I/O result"
                                )
                            deleted = connection.execute(
                                "DELETE FROM provider_request_attempts "
                                "WHERE sequence_id = ? AND attempt_index = ? "
                                "AND outcome = 'started'",
                                (sequence_id, attempt_index),
                            )
                            if deleted.rowcount != 1:
                                raise RuntimeError(
                                    "cached result did not discard one pending attempt"
                                )
                            connection.execute(
                                "DELETE FROM history_store_diagnostics "
                                "WHERE diagnostic_id = ?",
                                (diagnostic_id,),
                            )
                        self._finalize_attempt_sequence(sequence_id, final_count)
                        self._finish_retry_sequence_success(
                            sequence_id=sequence_id,
                            completed_at=attempted_at,
                            final_count=final_count,
                            result_payload=result_payload,
                        )
                        self._delete_lease(lease)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    return CoordinatedRequestResult(
                        value=cached_value,
                        sequence_id=sequence_id,
                        physical_attempt_count=final_count,
                        attempt_events=self.physical_attempt_events(sequence_id),
                    )
                final_count = attempt_index if attempt_recorded else attempt_index - 1
                result_payload = encode_coordinated_result(value)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if attempt_recorded:
                        self._finish_attempt_event(
                            sequence_id=sequence_id,
                            attempt_index=attempt_index,
                            failure=None,
                            cooldown_changed=False,
                            cooldown_until=None,
                        )
                    self._finalize_attempt_sequence(sequence_id, final_count)
                    self._finish_retry_sequence_success(
                        sequence_id=sequence_id,
                        completed_at=_as_utc(
                            now(),
                            label="coordinator time",
                        ),
                        final_count=final_count,
                        result_payload=result_payload,
                    )
                    self._delete_lease(lease)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                return CoordinatedRequestResult(
                    value=value,
                    sequence_id=sequence_id,
                    physical_attempt_count=final_count,
                    attempt_events=self.physical_attempt_events(sequence_id),
                )
            raise AssertionError("unreachable physical-attempt budget state")
        finally:
            row = self._store._connection.execute(
                "SELECT status FROM provider_request_sequences WHERE sequence_id = ?",
                (sequence_id,),
            ).fetchone()
            if row is not None and str(row[0]) == "running":
                connection = self._store._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._close_running_sequence(
                        sequence_id=sequence_id,
                        completed_at=_as_utc(now(), label="coordinator time"),
                        error_code="coordinator_sequence_abandoned",
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            self.release(lease)

    @staticmethod
    def _sequence_id(lease: RequestLease) -> str:
        digest = sha256()
        for component in (
            "provider-request-sequence",
            lease.request_key,
            lease.upstream_service_id,
            lease.owner_id,
            lease.acquired_at.isoformat(),
        ):
            encoded = component.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return f"provider-request-sequence=sha256:{digest.hexdigest()}"

    def _create_retry_sequence(
        self,
        sequence_id: str,
        lease: RequestLease,
    ) -> None:
        self._store._connection.execute(
            "INSERT INTO provider_request_sequences "
            "(sequence_id, request_key, upstream_service_id, owner_id, started_at, "
            "status) VALUES (?, ?, ?, ?, ?, 'running')",
            (
                sequence_id,
                lease.request_key,
                lease.upstream_service_id,
                lease.owner_id,
                lease.acquired_at.isoformat(),
            ),
        )

    def _finish_retry_sequence_success(
        self,
        *,
        sequence_id: str,
        completed_at: datetime,
        final_count: int,
        result_payload: bytes,
    ) -> None:
        cursor = self._store._connection.execute(
            "UPDATE provider_request_sequences SET status = 'succeeded', "
            "completed_at = ?, final_physical_attempt_count = ?, result_payload = ?, "
            "result_sha256 = ? WHERE sequence_id = ? AND status = 'running'",
            (
                completed_at.isoformat(),
                final_count,
                result_payload,
                sha256(result_payload).hexdigest(),
                sequence_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("retry sequence success did not update exactly one row")

    def _finish_retry_sequence_failure(
        self,
        *,
        sequence_id: str,
        completed_at: datetime,
        final_count: int,
        failure: PhysicalAttemptFailure,
    ) -> None:
        cursor = self._store._connection.execute(
            "UPDATE provider_request_sequences SET status = 'failed', "
            "completed_at = ?, final_physical_attempt_count = ?, "
            "failure_outcome = ?, failure_retryable = ?, failure_status_code = ?, "
            "failure_error_code = ?, failure_retry_after_seconds = ? "
            "WHERE sequence_id = ? AND status = 'running'",
            (
                completed_at.isoformat(),
                final_count,
                failure.outcome.value,
                int(failure.retryable),
                failure.status_code,
                failure.error_code,
                failure.retry_after_seconds,
                sequence_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("retry sequence failure did not update exactly one row")

    def _wait_for_persisted_sequence(
        self,
        *,
        request_key: str,
        upstream_service_id: str,
        owner_id: str,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> CoordinatedRequestResult[T]:
        while True:
            row = self._store._connection.execute(
                "SELECT sequence_id, status, final_physical_attempt_count, "
                "result_payload, result_sha256, failure_outcome, "
                "failure_retryable, failure_status_code, failure_error_code, "
                "failure_retry_after_seconds FROM provider_request_sequences "
                "WHERE request_key = ? AND upstream_service_id = ? AND owner_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (request_key, upstream_service_id, owner_id),
            ).fetchone()
            if row is not None and str(row[1]) != "running":
                return self._load_persisted_sequence(row)

            current = _as_utc(now(), label="coordinator time")
            lease_row = self._store._connection.execute(
                "SELECT expires_at FROM request_leases WHERE request_key = ? "
                "AND upstream_service_id = ? AND owner_id = ?",
                (request_key, upstream_service_id, owner_id),
            ).fetchone()
            if lease_row is not None and datetime.fromisoformat(str(lease_row[0])) <= current:
                connection = self._store._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._recover_expired_retry_sequences(current)
                    connection.execute(
                        "DELETE FROM request_leases WHERE expires_at <= ?",
                        (current.isoformat(),),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                continue
            if lease_row is None and row is not None and str(row[1]) == "running":
                connection = self._store._connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._close_running_sequence(
                        sequence_id=str(row[0]),
                        completed_at=current,
                        error_code="coordinator_owner_unavailable",
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                continue
            if lease_row is None and row is None:
                failure = PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.UPSTREAM_BUSY,
                    retryable=True,
                    error_code="single_flight_owner_unavailable",
                )
                raise PhysicalAttemptBudgetExhausted(
                    sequence_id="",
                    failure=failure,
                    attempt_events=(),
                )
            sleep(0.05)

    def _load_persisted_sequence(self, row: tuple[object, ...]) -> CoordinatedRequestResult[T]:
        sequence_id = str(row[0])
        status = str(row[1])
        final_count = int(row[2])
        attempt_events = self.physical_attempt_events(sequence_id)
        if status == "succeeded":
            result_payload = bytes(row[3])
            if sha256(result_payload).hexdigest() != str(row[4]):
                raise RuntimeError("persisted single-flight result digest mismatch")
            value = decode_coordinated_result(result_payload)
            return CoordinatedRequestResult(
                value=value,
                sequence_id=sequence_id,
                physical_attempt_count=final_count,
                attempt_events=attempt_events,
            )
        failure = PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome(str(row[5])),
            retryable=bool(row[6]),
            status_code=int(row[7]) if row[7] is not None else None,
            error_code=str(row[8]) if row[8] is not None else None,
            retry_after_seconds=float(row[9]) if row[9] is not None else None,
        )
        raise PhysicalAttemptBudgetExhausted(
            sequence_id=sequence_id,
            failure=failure,
            attempt_events=attempt_events,
        )

    def _close_running_sequence(
        self,
        *,
        sequence_id: str,
        completed_at: datetime,
        error_code: str,
    ) -> None:
        count_row = self._store._connection.execute(
            "SELECT COALESCE(MAX(attempt_index), 0) "
            "FROM provider_request_attempts WHERE sequence_id = ?",
            (sequence_id,),
        ).fetchone()
        final_count = int(count_row[0]) if count_row is not None else 0
        self._store._connection.execute(
            "UPDATE provider_request_attempts SET outcome = 'provider_error', "
            "retryable = 0, error_code = ? "
            "WHERE sequence_id = ? AND outcome = 'started'",
            (error_code, sequence_id),
        )
        self._finalize_attempt_sequence(sequence_id, final_count)
        self._store._connection.execute(
            "UPDATE provider_request_sequences SET status = 'failed', "
            "completed_at = ?, final_physical_attempt_count = ?, "
            "failure_outcome = 'provider_error', failure_retryable = 0, "
            "failure_error_code = ? WHERE sequence_id = ? AND status = 'running'",
            (completed_at.isoformat(), final_count, error_code, sequence_id),
        )

    def _delete_lease(self, lease: RequestLease) -> None:
        cursor = self._store._connection.execute(
            "DELETE FROM request_leases WHERE request_key = ? AND owner_id = ?",
            (lease.request_key, lease.owner_id),
        )
        if cursor.rowcount not in (0, 1):
            raise RuntimeError("request lease release affected multiple rows")

    def _service_name(self, upstream_service_id: str) -> str:
        row = self._store._connection.execute(
            "SELECT service_name FROM upstream_services WHERE upstream_service_id = ?",
            (upstream_service_id,),
        ).fetchone()
        if row is None:
            raise ValueError("upstream service must be registered before execution")
        return str(row[0])

    def _start_attempt_event(
        self,
        *,
        sequence_id: str,
        lease: RequestLease,
        service_name: str,
        operation: str,
        attempt_index: int,
        attempted_at: datetime,
        pacing_event: str,
        pacing_wait_seconds: float,
    ) -> None:
        self._store._connection.execute(
            "INSERT INTO provider_request_attempts "
            "(sequence_id, attempt_index, request_key, upstream_service_id, "
            "upstream_service_name, owner_id, priority, operation, attempted_at, "
            "pacing_event, pacing_wait_seconds, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started')",
            (
                sequence_id,
                attempt_index,
                lease.request_key,
                lease.upstream_service_id,
                service_name,
                lease.owner_id,
                int(lease.priority),
                operation,
                attempted_at.isoformat(),
                pacing_event,
                pacing_wait_seconds,
            ),
        )

    def _finish_attempt_event(
        self,
        *,
        sequence_id: str,
        attempt_index: int,
        failure: PhysicalAttemptFailure | None,
        cooldown_changed: bool,
        cooldown_until: datetime | None,
    ) -> None:
        outcome = (
            PhysicalAttemptOutcome.AVAILABLE if failure is None else failure.outcome
        )
        cursor = self._store._connection.execute(
            "UPDATE provider_request_attempts SET outcome = ?, retryable = ?, "
            "status_code = ?, error_code = ?, retry_after_seconds = ?, "
            "cooldown_changed = ?, cooldown_until = ? "
            "WHERE sequence_id = ? AND attempt_index = ? AND outcome = 'started'",
            (
                outcome.value,
                int(failure.retryable) if failure is not None else 0,
                failure.status_code if failure is not None else None,
                failure.error_code if failure is not None else None,
                failure.retry_after_seconds if failure is not None else None,
                int(cooldown_changed),
                cooldown_until.isoformat() if cooldown_until is not None else None,
                sequence_id,
                attempt_index,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("physical-attempt outcome did not update exactly one row")

    def _finalize_attempt_sequence(self, sequence_id: str, final_count: int) -> None:
        if final_count < 0:
            raise ValueError("final physical-attempt count cannot be negative")
        self._store._connection.execute(
            "UPDATE provider_request_attempts SET final_physical_attempt_count = ? "
            "WHERE sequence_id = ?",
            (final_count, sequence_id),
        )

    def physical_attempt_events(
        self,
        sequence_id: str,
    ) -> tuple[ProviderPhysicalAttemptEvent, ...]:
        rows = self._store._connection.execute(
            "SELECT request_key, upstream_service_id, upstream_service_name, "
            "attempt_index, attempted_at, pacing_event, pacing_wait_seconds, "
            "outcome, retryable, status_code, error_code, retry_after_seconds, "
            "cooldown_changed, cooldown_until, final_physical_attempt_count "
            "FROM provider_request_attempts WHERE sequence_id = ? "
            "ORDER BY attempt_index",
            (sequence_id,),
        )
        return tuple(
            ProviderPhysicalAttemptEvent(
                sequence_id=sequence_id,
                request_key=str(row[0]),
                upstream_service_id=str(row[1]),
                upstream_service_name=str(row[2]),
                attempt_index=int(row[3]),
                attempted_at=datetime.fromisoformat(str(row[4])),
                pacing_event=str(row[5]),
                pacing_wait_seconds=float(row[6]),
                outcome=PhysicalAttemptOutcome(str(row[7])),
                retryable=bool(row[8]),
                status_code=int(row[9]) if row[9] is not None else None,
                error_code=str(row[10]) if row[10] is not None else None,
                retry_after_seconds=(
                    float(row[11]) if row[11] is not None else None
                ),
                cooldown_changed=bool(row[12]),
                cooldown_until=(
                    datetime.fromisoformat(str(row[13]))
                    if row[13] is not None
                    else None
                ),
                final_physical_attempt_count=(
                    int(row[14]) if row[14] is not None else None
                ),
            )
            for row in rows
        )

    def earliest_physical_attempt_at(
        self,
        lease: RequestLease | None,
        *,
        requested_at: datetime,
    ) -> datetime:
        """Return when the next request may start under local operator pacing."""
        if lease is None:
            raise ValueError("an acquired request lease is required")
        requested_at = _as_utc(requested_at, label="physical-attempt time")
        row = self._store._connection.execute(
            "SELECT operator_ceiling_json FROM upstream_services "
            "WHERE upstream_service_id = ?",
            (lease.upstream_service_id,),
        ).fetchone()
        raw_ceiling = row[0] if row is not None else None
        if raw_ceiling is None:
            return requested_at
        ceiling = self._parse_operator_ceiling(
            lease.upstream_service_id,
            raw_ceiling,
        )
        latest_attempt = self._latest_physical_attempt_at(lease.upstream_service_id)
        if latest_attempt is None:
            return requested_at
        return max(requested_at, latest_attempt + ceiling.minimum_interval)

    def renew(
        self,
        lease: RequestLease | None,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> RequestLease | None:
        """Extend a lease only while the same owner still holds an unexpired row."""
        if lease is None:
            raise ValueError("an acquired request lease is required")
        now = _as_utc(now, label="lease-renewal time")
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        expires_at = now + lease_duration
        cursor = self._store._connection.execute(
            "UPDATE request_leases SET expires_at = ? "
            "WHERE request_key = ? AND upstream_service_id = ? AND owner_id = ? "
            "AND expires_at > ?",
            (
                expires_at.isoformat(),
                lease.request_key,
                lease.upstream_service_id,
                lease.owner_id,
                now.isoformat(),
            ),
        )
        if cursor.rowcount != 1:
            return None
        return RequestLease(
            request_key=lease.request_key,
            upstream_service_id=lease.upstream_service_id,
            owner_id=lease.owner_id,
            priority=lease.priority,
            acquired_at=lease.acquired_at,
            expires_at=expires_at,
        )

    def operational_summary(
        self,
        *,
        now: datetime,
    ) -> ProviderRequestOperationalSummary:
        now = _as_utc(now, label="operational-summary time")
        connection = self._store._connection
        diagnostic_counts = {
            str(code): int(count)
            for code, count in connection.execute(
                "SELECT code, COUNT(*) FROM history_store_diagnostics "
                "WHERE operation = 'provider_request' "
                "GROUP BY code"
            )
        }
        latest = connection.execute(
            "SELECT MAX(occurred_at) FROM history_store_diagnostics "
            "WHERE operation = 'provider_request'"
        ).fetchone()
        latest_value = latest[0] if latest is not None else None

        def active_count(table: str, expiry_column: str) -> int:
            row = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {expiry_column} > ?",
                (now.isoformat(),),
            ).fetchone()
            assert row is not None
            return int(row[0])

        return ProviderRequestOperationalSummary(
            physical_attempts=diagnostic_counts.get("physical_attempt", 0),
            rate_limit_events=diagnostic_counts.get("rate_limited", 0),
            active_cooldowns=active_count("request_cooldowns", "cooldown_until"),
            active_leases=active_count("request_leases", "expires_at"),
            queued_requests=active_count("request_queue", "expires_at"),
            latest_diagnostic_at=(
                datetime.fromisoformat(str(latest_value))
                if latest_value is not None
                else None
            ),
        )

    def _record_diagnostic(
        self,
        *,
        occurred_at: datetime,
        severity: str,
        code: str,
        identity_parts: tuple[str, ...],
        detail: dict[str, object],
    ) -> str:
        occurred_at = _as_utc(occurred_at, label="diagnostic time")
        digest = sha256()
        for component in ("provider-request-diagnostic", code, *identity_parts):
            encoded = component.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        diagnostic_id = f"history-diagnostic=sha256:{digest.hexdigest()}"
        self._store._connection.execute(
            "INSERT OR IGNORE INTO history_store_diagnostics "
            "(diagnostic_id, occurred_at, operation, severity, code, detail) "
            "VALUES (?, ?, 'provider_request', ?, ?, ?)",
            (
                diagnostic_id,
                occurred_at.isoformat(),
                severity,
                code,
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
            ),
        )
        return diagnostic_id

    def release(self, lease: RequestLease | None) -> None:
        if lease is None:
            raise ValueError("an acquired request lease is required")
        self._delete_lease(lease)

    def cancel_queued_request(
        self,
        *,
        request_key: str,
        upstream_service_id: str,
    ) -> None:
        """Remove a waiter when its caller abandons retrying this request."""
        if not request_key.strip() or not upstream_service_id.strip():
            raise ValueError("request and upstream identities must not be blank")
        cursor = self._store._connection.execute(
            "DELETE FROM request_queue WHERE request_key = ? "
            "AND upstream_service_id = ?",
            (request_key, upstream_service_id),
        )
        if cursor.rowcount not in (0, 1):
            raise RuntimeError("queued request cancellation affected multiple rows")
    _single_flight_lock = threading.Lock()
    _single_flights: dict[tuple[str, str, str], Future[object]] = {}

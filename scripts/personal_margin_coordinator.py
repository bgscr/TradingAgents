"""Personal margin recovery on the shared coordinator's existing write transactions."""

from datetime import datetime, timezone

from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderRequestCoordinator,
    RequestLease,
    RequestPriority,
)


class PriorMarginRequestUnsettled(Exception):
    pass


class MarginCacheHit(Exception):
    def __init__(self, value):
        self.value = value


_ZERO_RECOVERED = "personal_margin_expired_before_send"


class PersonalMarginCoordinator(ProviderRequestCoordinator):
    """Each instance handles one account/snapshot-bound margin request."""

    def __init__(self, store, key, service, cached, clock):
        super().__init__(store)
        self.key = key
        self.service = service
        self.cached = cached
        self.clock = clock

    def _recover_expired_retry_sequences(self, now):
        # acquire() holds BEGIN IMMEDIATE throughout recovery, pacing/queue
        # checks and lease creation. Never change the request key to refresh.
        db = self._database_connection()
        unresolved = db.execute(
            """
            SELECT 1 FROM provider_request_attempts a
            LEFT JOIN provider_request_sequences s ON s.sequence_id=a.sequence_id
            WHERE (a.request_key=? OR s.request_key=?) AND (
                s.sequence_id IS NULL OR s.request_key IS NOT a.request_key
                OR s.owner_id IS NOT a.owner_id OR s.upstream_service_id IS NOT a.upstream_service_id
                OR s.upstream_service_id IS NOT ? OR a.capacity_scope IS NOT 'market-snapshot'
                OR s.capacity_scope IS NOT a.capacity_scope OR s.status='running'
                OR a.attempt_index<>1 OR a.final_physical_attempt_count IS NOT 1
                OR s.final_physical_attempt_count IS NOT 1
                OR a.outcome='started' OR a.final_physical_attempt_count IS NULL
                OR COALESCE(a.terminal_outcome_kind,a.terminal_outcome)='abandoned')
            LIMIT 1
        """,
            (self.key, self.key, self.service),
        ).fetchone()
        if unresolved:
            raise PriorMarginRequestUnsettled()
        pending = db.execute(
            """
            SELECT sequence_id, upstream_service_id, owner_id, capacity_scope, started_at,
                   status, completed_at, final_physical_attempt_count
            FROM provider_request_sequences WHERE request_key=? AND (
                status='running' OR completed_at IS NULL OR final_physical_attempt_count IS NULL
                OR (status='succeeded' AND final_physical_attempt_count<>1)
                OR final_physical_attempt_count<>(SELECT COUNT(*) FROM provider_request_attempts a
                    WHERE a.sequence_id=provider_request_sequences.sequence_id)
                OR (failure_outcome_kind='abandoned' AND
                    (final_physical_attempt_count<>0 OR failure_error_code IS NOT ?)))
            LIMIT 2
        """,
            (self.key, _ZERO_RECOVERED),
        ).fetchall()
        row = db.execute(
            """
            SELECT upstream_service_id, owner_id, priority, capacity_scope, acquired_at, expires_at
            FROM request_leases WHERE request_key=?
        """,
            (self.key,),
        ).fetchone()
        if row:
            service, owner, priority, scope, acquired, expires = row
            try:
                start, end = datetime.fromisoformat(acquired), datetime.fromisoformat(expires)
            except (ValueError, TypeError):
                raise PriorMarginRequestUnsettled() from None
            if (
                service != self.service
                or not isinstance(owner, str)
                or not owner.strip()
                or scope != "market-snapshot"
                or priority != RequestPriority.INTERACTIVE_MAINLAND
                or start.tzinfo != timezone.utc
                or end.tzinfo != timezone.utc
                or start.isoformat() != acquired
                or end.isoformat() != expires
                or not start < end <= now
            ):
                raise PriorMarginRequestUnsettled()
        if pending:
            if row is None:
                raise PriorMarginRequestUnsettled()
            lease = RequestLease(
                self.key, service, owner, RequestPriority(priority), scope, start, end
            )
            sequence = self._sequence_id(lease)
            if pending != [(sequence, service, owner, scope, acquired, "running", None, None)]:
                raise PriorMarginRequestUnsettled()
            self._close_running_sequence(
                sequence_id=sequence, completed_at=now, error_code=_ZERO_RECOVERED
            )
        value = self.cached()
        if value is not None:
            raise MarginCacheHit(value)
        super()._recover_expired_retry_sequences(now)

    @staticmethod
    def _lease_identity(lease):
        return (
            lease.request_key,
            lease.upstream_service_id,
            lease.owner_id,
            lease.capacity_scope,
            lease.acquired_at.isoformat(),
        )

    def _live_generation(self, lease, now):
        if lease.request_key != self.key or lease.upstream_service_id != self.service:
            return False
        db = self._database_connection()
        row = db.execute(
            """
            SELECT 1 FROM request_leases WHERE request_key=? AND upstream_service_id=? AND owner_id=?
              AND capacity_scope=? AND acquired_at=? AND acquired_at<=? AND expires_at>?
        """,
            (*self._lease_identity(lease), now, now),
        ).fetchone()
        return row is not None

    def renew(self, lease, *, now, lease_duration):
        db = self._database_connection()
        db.execute("BEGIN IMMEDIATE")
        try:
            # The caller may have sampled now before waiting for this write lock.
            locked_at = self.clock().astimezone(timezone.utc)
            renewed = None
            if self._live_generation(lease, locked_at.isoformat()):
                renewed = super().renew(lease, now=locked_at, lease_duration=lease_duration)
            db.commit()
            return renewed
        except BaseException:
            db.rollback()
            raise

    def _delete_lease(self, lease):
        self._database_connection().execute(
            "DELETE FROM request_leases WHERE request_key=? AND upstream_service_id=? AND owner_id=? "
            "AND capacity_scope=? AND acquired_at=?",
            self._lease_identity(lease),
        )

    def _create_retry_sequence(self, sequence_id, lease):
        db = self._database_connection()
        db.execute("BEGIN IMMEDIATE")
        try:
            if not self._live_generation(lease, self.clock().astimezone(timezone.utc).isoformat()):
                raise PriorMarginRequestUnsettled()
            super()._create_retry_sequence(sequence_id, lease)
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _start_attempt_event(self, *, sequence_id, lease, **event):
        # Called inside the transaction that also records the physical I/O diagnostic.
        now = self.clock().astimezone(timezone.utc).isoformat()
        db = self._database_connection()
        running = db.execute(
            """
            SELECT 1 FROM provider_request_sequences WHERE sequence_id=? AND request_key=?
              AND upstream_service_id=? AND owner_id=? AND capacity_scope=? AND started_at=?
              AND status='running' AND completed_at IS NULL AND final_physical_attempt_count IS NULL
        """,
            (sequence_id, *self._lease_identity(lease)),
        ).fetchone()
        if running is None or not self._live_generation(lease, now):
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.UPSTREAM_BUSY, retryable=False
            )
        return super()._start_attempt_event(sequence_id=sequence_id, lease=lease, **event)

    def _finish_retry_sequence_failure(self, *, sequence_id, completed_at, final_count, failure):
        # A late worker can learn that its zero-attempt sequence was already
        # closed by the next owner. Keep the recovered terminal row intact.
        if (
            final_count == 0
            and self._database_connection()
            .execute(
                "SELECT 1 FROM provider_request_sequences WHERE sequence_id=? AND status='failed' "
                "AND failure_outcome_kind='abandoned' AND final_physical_attempt_count=0 "
                "AND failure_error_code IN (?, 'coordinator_lease_expired')",
                (sequence_id, _ZERO_RECOVERED),
            )
            .fetchone()
        ):
            return
        super()._finish_retry_sequence_failure(
            sequence_id=sequence_id,
            completed_at=completed_at,
            final_count=final_count,
            failure=failure,
        )

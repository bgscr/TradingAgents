"""Independent CURRENT raw-response cache; coverage means announcement windows.

This cache cannot establish report-period completeness or historical PIT evidence.
Callers validate provider semantics before returning bytes from ``fetch`` and filter
rows to each returned chunk's announcement window, including split old responses.
Only the supplied SQLite path is used; no transport, credentials or SDK is loaded.
"""

import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

DateRange = tuple[date, date]


@dataclass(frozen=True)
class StatementCacheScope:
    """Non-secret opaque identities; transport identity should be a destination digest."""

    account_scope: str
    instrument_identity_revision: str
    endpoint_id: str
    transport_identity: str
    normalizer_version: str

    def __post_init__(self) -> None:
        for value in (
            self.account_scope,
            self.instrument_identity_revision,
            self.endpoint_id,
            self.transport_identity,
            self.normalizer_version,
        ):
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value
            ):
                raise ValueError("cache scope requires bounded non-secret opaque identifiers")

    @property
    def key(self) -> str:
        identity = [
            self.account_scope,
            self.instrument_identity_revision,
            self.endpoint_id,
            self.transport_identity,
            self.normalizer_version,
        ]
        return sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class StatementCacheChunk:
    range_start: date
    range_end: date
    payload: bytes
    checked_at: datetime
    payload_sha256: str


@dataclass(frozen=True)
class StatementCacheResult:
    """Validated current selections, including retained stale chunks after failure.

    All ranges are inclusive announcement dates. Missing means no valid bytes;
    stale means valid retained bytes outside the age policy. Fetched ranges can
    include live-only bytes when cache_failure reports persistence degradation.
    Reuse never changes checked_at. Failure is transient and never persisted;
    cache_failure contains only a fixed diagnostic code, never exception text.
    """

    chunks: tuple[StatementCacheChunk, ...]
    fetched_ranges: tuple[DateRange, ...]
    reused_ranges: tuple[DateRange, ...]
    missing_ranges: tuple[DateRange, ...] = ()
    stale_ranges: tuple[DateRange, ...] = ()
    failure: Exception | None = None
    failure_range: DateRange | None = None
    cache_failure: str | None = None


def _gaps(start: date, end: date, covered: list[DateRange]) -> list[DateRange]:
    cursor = start.toordinal()
    last = end.toordinal()
    gaps = []
    for left, right in sorted(covered):
        lo, hi = max(cursor, left.toordinal()), min(last, right.toordinal())
        if hi < cursor or lo > last:
            continue
        if cursor < lo:
            gaps.append((date.fromordinal(cursor), date.fromordinal(lo - 1)))
        cursor = max(cursor, hi + 1)
    if cursor <= last:
        gaps.append((date.fromordinal(cursor), end))
    return gaps


def _validate_payload(payload: bytes) -> None:
    if not isinstance(payload, bytes):
        raise ValueError("response must be JSON bytes")

    def reject_constant(_value: str) -> None:
        raise ValueError("response contains a non-JSON number")

    decoded = json.loads(payload.decode("utf-8"), parse_constant=reject_constant)
    if not isinstance(decoded, (dict, list)):
        raise ValueError("response must be a JSON object or array")


class CurrentStatementCache:
    """Cache validated response bytes in a dedicated, explicitly supplied file.

    ``checked_at`` is the caller's aware acquisition time, never a reuse time.
    ``fetch`` must return a complete response for its inclusive date window.
    """

    def __init__(self, path: str | Path, max_age: timedelta = timedelta(hours=24), *, read_only=False):
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")
        self.path = Path(path)
        self.max_age = max_age
        self.read_only = read_only

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            # Immutable prevents sidecar creation too. Refuse a live WAL rather
            # than silently ignore its uncheckpointed observations.
            if Path(str(self.path) + "-wal").exists():
                raise sqlite3.OperationalError("read-only cache has a WAL")
            connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5
            )
            connection.execute("PRAGMA query_only = ON")
            return connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY,
                    scope TEXT NOT NULL,
                    range_start TEXT NOT NULL,
                    range_end TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    payload_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_windows (
                    scope TEXT NOT NULL,
                    range_start TEXT NOT NULL,
                    range_end TEXT NOT NULL,
                    observation_id INTEGER NOT NULL REFERENCES observations(id),
                    PRIMARY KEY (scope, range_start)
                );
            """)
            return connection
        except Exception:
            connection.close()
            raise

    def _load(
        self,
        scope: str,
        start: date,
        end: date,
    ) -> tuple[list[StatementCacheChunk], str | None]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT a.range_start, a.range_end, o.payload, o.checked_at,
                       o.payload_sha256, o.scope, o.range_start, o.range_end
                FROM active_windows a LEFT JOIN observations o ON o.id=a.observation_id
                WHERE a.scope=? AND a.range_end>=? AND a.range_start<=?
                ORDER BY a.range_start
            """,
                (scope, start.isoformat(), end.isoformat()),
            ).fetchall()
        chunks = []
        cache_failure = None
        for left, right, payload, checked, digest, origin_scope, origin_start, origin_end in rows:
            try:
                first, last = date.fromisoformat(left), date.fromisoformat(right)
                observed = datetime.fromisoformat(checked)
                _validate_payload(payload)
                if (
                    sha256(payload).hexdigest() != digest
                    or origin_scope != scope
                    or not date.fromisoformat(origin_start)
                    <= first
                    <= last
                    <= date.fromisoformat(origin_end)
                    or observed.utcoffset() is None
                ):
                    raise ValueError("invalid cached observation")
                chunk = StatementCacheChunk(
                    max(start, first), min(end, last), payload, observed, digest
                )
                if chunks and chunks[-1].range_end >= chunk.range_start:
                    return [], "cache_corrupt"
                chunks.append(chunk)
            except (TypeError, ValueError, OverflowError):
                cache_failure = "cache_corrupt"
        return chunks, cache_failure

    def _store(self, scope: str, chunk: StatementCacheChunk) -> None:
        if self.read_only:
            raise RuntimeError("read-only statement cache cannot store")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            overlapping = connection.execute(
                """
                SELECT range_start, range_end, observation_id FROM active_windows
                WHERE scope=? AND range_end>=? AND range_start<=?
            """,
                (scope, chunk.range_start.isoformat(), chunk.range_end.isoformat()),
            ).fetchall()
            connection.execute(
                """
                DELETE FROM active_windows
                WHERE scope=? AND range_end>=? AND range_start<=?
            """,
                (scope, chunk.range_start.isoformat(), chunk.range_end.isoformat()),
            )
            for left, right, observation_id in overlapping:
                if left < chunk.range_start.isoformat():
                    connection.execute(
                        "INSERT INTO active_windows VALUES (?,?,?,?)",
                        (
                            scope,
                            left,
                            (chunk.range_start - timedelta(days=1)).isoformat(),
                            observation_id,
                        ),
                    )
                if right > chunk.range_end.isoformat():
                    connection.execute(
                        "INSERT INTO active_windows VALUES (?,?,?,?)",
                        (
                            scope,
                            (chunk.range_end + timedelta(days=1)).isoformat(),
                            right,
                            observation_id,
                        ),
                    )
            row = connection.execute(
                """
                INSERT INTO observations
                (scope,range_start,range_end,checked_at,payload,payload_sha256)
                VALUES (?,?,?,?,?,?)
            """,
                (
                    scope,
                    chunk.range_start.isoformat(),
                    chunk.range_end.isoformat(),
                    chunk.checked_at.isoformat(),
                    chunk.payload,
                    chunk.payload_sha256,
                ),
            )
            connection.execute(
                "INSERT INTO active_windows VALUES (?,?,?,?)",
                (
                    scope,
                    chunk.range_start.isoformat(),
                    chunk.range_end.isoformat(),
                    row.lastrowid,
                ),
            )

    def read_through(
        self,
        scope: StatementCacheScope,
        range_start: date,
        range_end: date,
        now: datetime,
        fetch: Callable[[date, date], bytes],
        refresh: bool = False,
    ) -> StatementCacheResult:
        if self.read_only:
            # An analysis date or caller's reconstruction clock is never now.
            now = datetime.now(timezone.utc)
        if type(range_start) is not date or type(range_end) is not date or range_start > range_end:
            raise ValueError("range must be ordered inclusive dates")
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")
        try:
            chunks, cache_failure = self._load(scope.key, range_start, range_end)
        except (sqlite3.Error, OSError):
            chunks, cache_failure = [], "cache_read_failed"
        reused = [
            (chunk.range_start, chunk.range_end)
            for chunk in chunks
            if not refresh and timedelta(0) <= now - chunk.checked_at < self.max_age
        ]
        fetched = []
        failure = None
        failure_range = None
        ranges = [(range_start, range_end)] if refresh else _gaps(range_start, range_end, reused)
        if self.read_only and (refresh or cache_failure or ranges):
            from tradingagents.analysis_budget import AnalysisBudgetStopped, active_budget

            reason = (
                "financial_cache_refresh_conflict" if refresh else
                "financial_cache_unreadable" if cache_failure else
                "financial_cache_stale" if any(
                    not timedelta(0) <= now - c.checked_at < self.max_age for c in chunks
                ) else "financial_cache_missing"
            )
            if active_budget() is not None:
                active_budget().stop(reason)
            raise AnalysisBudgetStopped(reason)
        for start, end in ranges:
            try:
                payload = fetch(start, end)
                _validate_payload(payload)
            except Exception as error:
                failure, failure_range = error, (start, end)
                break
            chunk = StatementCacheChunk(start, end, payload, now, sha256(payload).hexdigest())
            try:
                self._store(scope.key, chunk)
            except (sqlite3.Error, OSError):
                cache_failure = cache_failure or "cache_write_failed"
            remaining = []
            for old in chunks:
                if old.range_end < start or old.range_start > end:
                    remaining.append(old)
                    continue
                if old.range_start < start:
                    remaining.append(replace(old, range_end=start - timedelta(days=1)))
                if old.range_end > end:
                    remaining.append(replace(old, range_start=end + timedelta(days=1)))
            chunks = sorted([*remaining, chunk], key=lambda c: c.range_start)
            fetched.append((start, end))
        missing = _gaps(range_start, range_end, [(c.range_start, c.range_end) for c in chunks])
        stale = tuple(
            (c.range_start, c.range_end)
            for c in chunks
            if not timedelta(0) <= now - c.checked_at < self.max_age
        )
        return StatementCacheResult(
            tuple(chunks),
            tuple(fetched),
            tuple(reused),
            tuple(missing),
            stale,
            failure,
            failure_range,
            cache_failure,
        )

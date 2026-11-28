"""Read-only reuse of validated advisory observations for retrospective sessions."""
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import sqlite3

from tradingagents.market_history.config import DataUsageMode
from tradingagents.market_history.coordinator import upstream_service_identity_for_provider
from tradingagents.market_history.result_codec import decode_coordinated_result, encode_coordinated_result


def retrospective_observation(store_config, *, request_key: str, provider: str, operation: str,
                              as_of: date, receipt_time: Callable[[object], str | None], with_receipt=False,
                              accept_attempt: Callable[[object, datetime], bool] | None = None):
    """Read a completed, single-send advisory projection for current retrospective use."""

    if store_config.data_usage_mode is not DataUsageMode.PERSONAL_RESEARCH or (provider, operation) not in {
        ("tushare", "margin_detail"),
        ("akshare", "financial-fundamentals:stock_individual_fund_flow.powershell"),
    }:
        return None
    upstream, service = upstream_service_identity_for_provider(provider)
    # Choose the latest sequence before examining eligibility. A newer
    # incomplete or rejected request must not expose an older success.
    uri = store_config.database_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute("""
            WITH latest AS (
                SELECT * FROM provider_request_sequences
                WHERE request_key=? AND upstream_service_id=? AND capacity_scope='market-snapshot'
                ORDER BY julianday(started_at) IS NULL DESC, julianday(started_at) DESC LIMIT 2
            )
            SELECT s.result_payload, s.result_sha256, s.started_at, a.attempted_at, s.completed_at, s.sequence_id
            FROM latest s JOIN provider_request_attempts a ON a.sequence_id=s.sequence_id
            WHERE s.status='succeeded' AND s.final_physical_attempt_count=1
              AND julianday(s.started_at)=(SELECT max(julianday(started_at)) FROM latest)
              AND NOT EXISTS (SELECT 1 FROM latest WHERE julianday(started_at) IS NULL)
              AND (SELECT count(*) FROM latest WHERE julianday(started_at)=julianday(s.started_at))=1
              AND s.failure_outcome IS NULL AND s.failure_outcome_kind IS NULL
              AND s.failure_status_code IS NULL AND s.failure_error_code IS NULL
              AND s.failure_retryable IS NULL AND s.failure_retry_after_seconds IS NULL
              AND length(s.result_payload) BETWEEN 1 AND 32768
              AND a.attempt_index=1 AND a.final_physical_attempt_count=1
              AND a.owner_id=s.owner_id AND a.request_key=s.request_key
              AND a.upstream_service_id=s.upstream_service_id AND a.capacity_scope=s.capacity_scope
              AND a.upstream_service_name=? AND a.operation=?
              AND a.outcome='available' AND a.terminal_outcome='available'
              AND a.terminal_outcome_kind='available' AND a.retryable=0 AND a.error_code IS NULL
              AND (a.status_code IS NULL OR a.status_code=200)
              AND a.retry_after_seconds IS NULL AND a.cooldown_changed=0 AND a.cooldown_until IS NULL
              AND (SELECT count(*) FROM provider_request_attempts WHERE sequence_id=s.sequence_id)=1
            """, (request_key, upstream, service, operation)).fetchone()
    if row is None:
        return None
    payload, digest, started, attempted, completed, sequence = row
    if not isinstance(payload, bytes) or sha256(payload).hexdigest() != digest:
        return None
    try:
        value = decode_coordinated_result(payload, projection_only=True)
        # Reject non-canonical codec trees, extra keys and duplicate keys.
        if encode_coordinated_result(value) != payload:
            return None
        received = receipt_time(value)
        if not isinstance(received, str):
            return None
        times = [datetime.fromisoformat(t) for t in (started, attempted, received, completed)]
        if any(t.utcoffset() != timedelta(0) for t in times):
            return None
        if not times[0] <= times[1] <= times[2] <= times[3] <= datetime.now(timezone.utc):
            return None
        # This reader serves closed historical windows only. A fresh
        # intraday observation cannot become an end-of-day cache entry.
        if times[2].astimezone(timezone(timedelta(hours=8))).date() <= as_of:
            return None
        if accept_attempt is not None and not accept_attempt(value, times[1]):
            return None
        if with_receipt:
            return dict(sequence_id=sequence, request_key=request_key, result_sha256=digest,
                        attempted_at=attempted, completed_at=completed, physical_attempts=1)
        return value
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        return None


from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TypeVar

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import PITError, TushareRateLimitError
from tradingagents.picker.normalize import normalize_partition
from tradingagents.picker.pit_dates import parse_yyyymmdd, validate_date_range
from tradingagents.picker.pit_models import (
    Dataset,
    IngestionRunManifest,
    IngestionSummary,
    PartitionKey,
    PartitionRecord,
)
from tradingagents.picker.rate_limit import RetryPolicy, TokenBucketLimiter
from tradingagents.picker.tushare_provider import TushareProvider

_SECRET_RE = re.compile(
    r"(?i)(?:\bTUSHARE_TOKEN\b|\btoken\b|\bapi[_ -]?key\b|\bauthorization\b)"
    r"(\s*[:=]\s*|\s+)(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_MAX_PROVIDER_ATTEMPTS = 8
_Result = TypeVar("_Result")


def _effective_start(start: date) -> date:
    return start - timedelta(days=120)


def _partition_order(key: PartitionKey) -> tuple[str, str]:
    return key.dataset.value, key.partition


def plan_bootstrap_partitions(
    start_date: str, end_date: str
) -> list[PartitionKey]:
    start, end = validate_date_range(start_date, end_date)
    warmup_start = _effective_start(start)
    keys = [
        PartitionKey(Dataset.TRADE_CAL, str(year))
        for year in range(warmup_start.year, end.year + 1)
    ]
    keys.extend(
        [
            PartitionKey(Dataset.STOCK_BASIC, "current"),
            PartitionKey(Dataset.NAMECHANGE, "all"),
        ]
    )
    return sorted(keys, key=_partition_order)


def open_trade_dates(
    cache: PITCache, start_date: str, end_date: str
) -> list[str]:
    start, end = validate_date_range(start_date, end_date)
    warmup_start = _effective_start(start)
    lower = warmup_start.strftime("%Y%m%d")
    upper = end.strftime("%Y%m%d")
    dates: set[str] = set()

    for year in range(warmup_start.year, end.year + 1):
        key = PartitionKey(Dataset.TRADE_CAL, str(year))
        calendar = cache.load_frame(key)
        open_rows = calendar.loc[calendar["is_open"] == 1, "cal_date"]
        for value in open_rows.astype(str):
            if lower <= value <= upper:
                dates.add(value)

    return sorted(dates)


def plan_daily_partitions(open_dates: list[str]) -> list[PartitionKey]:
    dates = sorted(
        {
            parse_yyyymmdd(value, "open date").strftime("%Y%m%d")
            for value in open_dates
        }
    )
    keys = [
        PartitionKey(dataset, trade_date)
        for dataset in (Dataset.DAILY, Dataset.DAILY_BASIC, Dataset.SUSPEND_D)
        for trade_date in dates
    ]
    return sorted(keys, key=_partition_order)


def _sanitized_error(exc: BaseException) -> str:
    message = _SECRET_RE.sub("<redacted>", str(exc))
    return f"{type(exc).__name__}: {message}"


class PITIngestor:
    def __init__(
        self,
        provider: TushareProvider,
        cache: PITCache,
        limiter_for: Callable[[str], TokenBucketLimiter],
        retry_policy: RetryPolicy,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if retry_policy.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.provider = provider
        self.cache = cache
        self.limiter_for = limiter_for
        self.retry_policy = retry_policy
        self._max_attempts = min(retry_policy.max_attempts, _MAX_PROVIDER_ATTEMPTS)
        self.sleeper = sleeper
        self._active_manifest: IngestionRunManifest | None = None

    def probe(self) -> None:
        failure: Exception | None = None
        try:
            self.provider.probe(self._call_with_retry)
        except TushareRateLimitError as exc:
            failure = exc
        except Exception:
            failure = PITError("Tushare endpoint 'trade_cal' probe failed")

        if failure is not None:
            raise failure

    def ingest(
        self, start_date: str, end_date: str, refresh: bool = False
    ) -> IngestionSummary:
        start, end = validate_date_range(start_date, end_date)
        run_id = self._new_run_id()
        self._active_manifest = IngestionRunManifest(
            run_id=run_id,
            requested_start=start_date,
            requested_end=end_date,
            effective_start=_effective_start(start).strftime("%Y%m%d"),
            effective_end=end.strftime("%Y%m%d"),
            created_at=self._utc_now(),
            partitions=(),
            status="running",
        )
        self.cache.write_run_manifest(self._active_manifest)

        failure: Exception | None = None
        try:
            bootstrap = self._ingest_keys(
                plan_bootstrap_partitions(start_date, end_date), refresh=refresh
            )
            open_dates = open_trade_dates(self.cache, start_date, end_date)
            daily = self._ingest_keys(
                plan_daily_partitions(open_dates), refresh=refresh
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            self._finalize_run("interrupted", _sanitized_error(exc))
            raise
        except Exception as exc:
            self._finalize_run("failed", _sanitized_error(exc))
            failure = exc

        if failure is not None:
            raise failure

        self._finalize_run("complete")
        return IngestionSummary(
            completed=bootstrap.completed + daily.completed,
            skipped=bootstrap.skipped + daily.skipped,
            failed=bootstrap.failed + daily.failed,
            run_id=run_id,
        )

    def _ingest_keys(
        self, keys: Iterable[PartitionKey], refresh: bool = False
    ) -> IngestionSummary:
        completed = 0
        skipped = 0
        failed = 0

        for key in keys:
            if not refresh and self.cache.is_complete(key):
                skipped += 1
                self._record_partition(self.cache.records()[key.storage_key])
                continue

            self.cache.mark_pending(key)
            failure: Exception | None = None
            try:
                frame = self.provider.fetch(key, self._call_with_retry)
                raw_payload = frame.to_json(
                    orient="table", date_format="iso"
                ).encode("utf-8")
                normalized = normalize_partition(key.dataset, frame)
                record = self.cache.store_complete(
                    key,
                    raw_payload,
                    normalized,
                    schema_version="1",
                )
            except TushareRateLimitError as exc:
                self._record_partition(self.cache.records()[key.storage_key])
                failure = exc
            except (KeyboardInterrupt, SystemExit):
                self._record_partition(self.cache.records()[key.storage_key])
                raise
            except Exception as exc:
                failed += 1
                self.cache.mark_failed(key, _sanitized_error(exc))
                self._record_partition(self.cache.records()[key.storage_key])
                failure = PITError(
                    f"PIT ingestion failed for partition '{key.storage_key}'"
                )

            if failure is not None:
                raise failure

            completed += 1
            self._record_partition(record)

        return IngestionSummary(
            completed=completed,
            skipped=skipped,
            failed=failed,
        )

    def _call_with_retry(
        self, endpoint: str, callable: Callable[[], _Result]
    ) -> _Result:
        limiter = self.limiter_for(endpoint)
        final_retry_after: float | None = None
        for attempt in range(1, self._max_attempts + 1):
            limiter.acquire()
            try:
                return callable()
            except TushareRateLimitError as exc:
                limiter.penalize()
                if attempt == self._max_attempts:
                    final_retry_after = exc.retry_after
                    break
                self.sleeper(self.retry_policy.delay(attempt, exc.retry_after))
        raise TushareRateLimitError(
            f"Tushare endpoint '{endpoint}' rate limit exceeded",
            retry_after=final_retry_after,
        )

    def _record_partition(self, record: PartitionRecord) -> None:
        if self._active_manifest is None:
            return
        self._active_manifest = replace(
            self._active_manifest,
            partitions=(*self._active_manifest.partitions, record),
        )
        self.cache.write_run_manifest(self._active_manifest)

    def _finalize_run(self, status: str, error: str | None = None) -> None:
        if self._active_manifest is None:
            return
        finalized = replace(self._active_manifest, status=status, error=error)
        self.cache.write_run_manifest(finalized)
        self._active_manifest = None

    @staticmethod
    def _new_run_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{timestamp}-{secrets.token_hex(4)}"

    @staticmethod
    def _utc_now() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

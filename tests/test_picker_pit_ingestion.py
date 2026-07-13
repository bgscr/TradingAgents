import json
import re

import pandas as pd
import pytest

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import PITSchemaError, TushareRateLimitError
from tradingagents.picker.ingestion import (
    PITIngestor,
    open_trade_dates,
    plan_bootstrap_partitions,
    plan_daily_partitions,
)
from tradingagents.picker.pit_models import Dataset, PartitionKey, PartitionStatus
from tradingagents.picker.rate_limit import RetryPolicy


class FakeProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def fetch(self, key):
        self.calls.append(key)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeProbeProvider:
    def __init__(self, results, events=None):
        self.results = list(results)
        self.events = events

    def probe(self):
        if self.events is not None:
            self.events.append("probe")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class NoWaitLimiter:
    def __init__(self, events=None):
        self.acquires = 0
        self.penalties = 0
        self.events = events

    def acquire(self):
        self.acquires += 1
        if self.events is not None:
            self.events.append("acquire")

    def penalize(self):
        self.penalties += 1
        if self.events is not None:
            self.events.append("penalize")


class RecordingCache(PITCache):
    def __init__(self, root):
        super().__init__(root)
        self.run_writes = []

    def write_run_manifest(self, manifest):
        self.run_writes.append(manifest)
        super().write_run_manifest(manifest)


def frame(code="000001.SZ"):
    return pd.DataFrame(
        {
            "ts_code": [code],
            "trade_date": ["20260710"],
            "close": [10.0],
            "amount": [100.0],
        }
    )


def calendar_frame(*dates):
    return pd.DataFrame(
        {
            "cal_date": list(dates),
            "is_open": [1] * len(dates),
        }
    )


def identity_normalizer(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.picker.ingestion.normalize_partition",
        lambda dataset, value: value,
    )


def load_only_run(cache):
    paths = list((cache.root / "runs").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_partition_planning_uses_only_cached_calendar(tmp_path):
    cache = PITCache(tmp_path)
    calendar_key = PartitionKey(Dataset.TRADE_CAL, "2026")
    calendar = pd.DataFrame(
        {
            "cal_date": ["20260709", "20260710", "20260711"],
            "is_open": [1, 1, 0],
        }
    )
    cache.store_complete(calendar_key, b"[]", calendar, "1")

    bootstrap = plan_bootstrap_partitions("20260709", "20260711")

    assert PartitionKey(Dataset.NAMECHANGE, "all") in bootstrap
    dates = open_trade_dates(cache, "20260709", "20260711")
    assert dates == ["20260709", "20260710"]
    assert plan_daily_partitions(dates)[0].partition == "20260709"


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        ("2026-07-10", "20260710"),
        ("2026071", "20260710"),
        ("20260229", "20260710"),
        ("20260710T000000", "20260710"),
        ("20260711", "20260710"),
    ],
)
def test_partition_planning_rejects_non_date_only_or_reversed_ranges(
    start_date, end_date
):
    with pytest.raises(ValueError):
        plan_bootstrap_partitions(start_date, end_date)


def test_bootstrap_uses_exact_120_calendar_day_warmup_and_stable_order():
    keys = plan_bootstrap_partitions("20260101", "20260102")

    assert keys == [
        PartitionKey(Dataset.NAMECHANGE, "all"),
        PartitionKey(Dataset.STOCK_BASIC, "current"),
        PartitionKey(Dataset.TRADE_CAL, "2025"),
        PartitionKey(Dataset.TRADE_CAL, "2026"),
    ]


def test_daily_partition_order_is_dataset_then_date_and_deduplicates_dates():
    keys = plan_daily_partitions(["20260710", "20260709", "20260710"])

    assert keys == [
        PartitionKey(Dataset.DAILY, "20260709"),
        PartitionKey(Dataset.DAILY, "20260710"),
        PartitionKey(Dataset.DAILY_BASIC, "20260709"),
        PartitionKey(Dataset.DAILY_BASIC, "20260710"),
        PartitionKey(Dataset.SUSPEND_D, "20260709"),
        PartitionKey(Dataset.SUSPEND_D, "20260710"),
    ]


def test_open_trade_dates_reads_every_warmup_year_and_filters_inclusively(tmp_path):
    cache = PITCache(tmp_path)
    cache.store_complete(
        PartitionKey(Dataset.TRADE_CAL, "2025"),
        b"[]",
        pd.DataFrame(
            {
                "cal_date": ["20250902", "20250903", "20251231"],
                "is_open": [1, 1, 1],
            }
        ),
        "1",
    )
    cache.store_complete(
        PartitionKey(Dataset.TRADE_CAL, "2026"),
        b"[]",
        pd.DataFrame(
            {
                "cal_date": ["20260101", "20260102"],
                "is_open": [1, 1],
            }
        ),
        "1",
    )

    assert open_trade_dates(cache, "20260101", "20260101") == [
        "20250903",
        "20251231",
        "20260101",
    ]


def test_open_trade_dates_fails_closed_when_calendar_checksum_is_invalid(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.TRADE_CAL, "2026")
    cache.store_complete(key, b"[]", calendar_frame("20260710"), "1")
    cache.normalized_path(key).write_bytes(b"tampered")

    with pytest.raises(PITSchemaError, match="failed verification"):
        open_trade_dates(cache, "20260710", "20260710")


def test_completed_partition_is_skipped_on_resume(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    original = cache.store_complete(key, b"[]", frame(), "1")
    provider = FakeProvider([])
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(
        provider,
        cache,
        lambda endpoint: limiter,
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    summary = ingestor._ingest_keys([key])

    assert summary.skipped == 1
    assert provider.calls == []
    assert cache.records()[key.storage_key] == original


def test_corrupt_complete_partition_is_not_skipped(tmp_path, monkeypatch):
    identity_normalizer(monkeypatch)
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache.store_complete(key, b"[]", frame(), "1")
    cache.raw_path(key).write_bytes(b"corrupt")
    provider = FakeProvider([frame("000002.SZ")])
    limiter = NoWaitLimiter()

    summary = PITIngestor(
        provider,
        cache,
        lambda endpoint: limiter,
        RetryPolicy(),
        sleeper=lambda _: None,
    )._ingest_keys([key])

    assert summary.completed == 1
    assert provider.calls == [key]


def test_refresh_fetches_new_content_addressed_version(tmp_path, monkeypatch):
    identity_normalizer(monkeypatch)
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    original = cache.store_complete(key, b'[{"close": 9}]', frame(), "1")
    provider = FakeProvider([frame("000002.SZ")])
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(
        provider,
        cache,
        lambda endpoint: limiter,
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    summary = ingestor._ingest_keys([key], refresh=True)

    refreshed = cache.records()[key.storage_key]
    assert summary.completed == 1
    assert provider.calls == [key]
    assert refreshed.raw_sha256 != original.raw_sha256
    assert refreshed.normalized_sha256 != original.normalized_sha256
    assert cache.root.joinpath(original.raw_path).is_file()
    assert cache.root.joinpath(original.normalized_path).is_file()


def test_limiter_acquire_is_immediately_before_every_provider_call(
    tmp_path, monkeypatch
):
    identity_normalizer(monkeypatch)
    events = []
    key = PartitionKey(Dataset.DAILY, "20260710")

    class EventProvider(FakeProvider):
        def fetch(self, key):
            events.append("fetch")
            return super().fetch(key)

    limiter = NoWaitLimiter(events)
    ingestor = PITIngestor(
        EventProvider([TushareRateLimitError("429"), frame()]),
        PITCache(tmp_path),
        lambda endpoint: limiter,
        RetryPolicy(jitter=lambda low, high: 0.0),
        sleeper=lambda seconds: events.append(("sleep", seconds)),
    )

    ingestor._ingest_keys([key])

    assert events == [
        "acquire",
        "fetch",
        "penalize",
        ("sleep", 1.0),
        "acquire",
        "fetch",
    ]


def test_throttle_then_success_uses_provider_delay_and_completes(
    tmp_path, monkeypatch
):
    identity_normalizer(monkeypatch)
    key = PartitionKey(Dataset.DAILY, "20260710")
    provider = FakeProvider([TushareRateLimitError("429", retry_after=3.0), frame()])
    limiter = NoWaitLimiter()
    sleeps = []
    ingestor = PITIngestor(
        provider,
        PITCache(tmp_path),
        lambda endpoint: limiter,
        RetryPolicy(),
        sleeper=sleeps.append,
    )

    summary = ingestor._ingest_keys([key])

    assert summary.completed == 1
    assert sleeps == [3.0]
    assert limiter.penalties == 1
    assert limiter.acquires == 2


def test_throttle_without_provider_delay_uses_policy_backoff(tmp_path, monkeypatch):
    identity_normalizer(monkeypatch)
    key = PartitionKey(Dataset.DAILY, "20260710")
    provider = FakeProvider(
        [TushareRateLimitError("429"), TushareRateLimitError("429"), frame()]
    )
    limiter = NoWaitLimiter()
    sleeps = []
    ingestor = PITIngestor(
        provider,
        PITCache(tmp_path),
        lambda endpoint: limiter,
        RetryPolicy(jitter=lambda low, high: 0.0),
        sleeper=sleeps.append,
    )

    ingestor._ingest_keys([key])

    assert sleeps == [1.0, 2.0]
    assert limiter.acquires == 3
    assert limiter.penalties == 2


def test_eighth_throttle_leaves_pending_and_exits(tmp_path):
    key = PartitionKey(Dataset.DAILY, "20260710")
    provider = FakeProvider([TushareRateLimitError("429")] * 8)
    cache = PITCache(tmp_path)
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(
        provider,
        cache,
        lambda endpoint: limiter,
        RetryPolicy(max_attempts=8, jitter=lambda low, high: 0.0),
        sleeper=lambda _: None,
    )

    with pytest.raises(TushareRateLimitError):
        ingestor._ingest_keys([key])

    assert len(provider.calls) == 8
    assert limiter.acquires == 8
    assert limiter.penalties == 8
    assert cache.records()[key.storage_key].status is PartitionStatus.PENDING


def test_non_throttle_error_marks_failed_without_leaking_token(tmp_path):
    key = PartitionKey(Dataset.DAILY, "20260710")
    provider = FakeProvider([RuntimeError("request token=super-secret failed")])
    cache = PITCache(tmp_path)
    ingestor = PITIngestor(
        provider,
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    with pytest.raises(RuntimeError, match="super-secret"):
        ingestor._ingest_keys([key])

    record = cache.records()[key.storage_key]
    assert record.status is PartitionStatus.FAILED
    assert record.error.startswith("RuntimeError:")
    assert "super-secret" not in record.error
    assert "token=<redacted>" in record.error


def test_non_throttle_error_redacts_bearer_credential(tmp_path):
    key = PartitionKey(Dataset.DAILY, "20260710")
    provider = FakeProvider(
        [RuntimeError("Authorization: Bearer highly-sensitive-value")]
    )
    cache = PITCache(tmp_path)
    ingestor = PITIngestor(
        provider,
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    with pytest.raises(RuntimeError, match="highly-sensitive-value"):
        ingestor._ingest_keys([key])

    error = cache.records()[key.storage_key].error
    assert "highly-sensitive-value" not in error
    assert "Authorization: <redacted>" in error


def test_raw_table_json_is_captured_before_normalization(tmp_path, monkeypatch):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    raw_frame = frame()

    def mutate_during_normalization(dataset, value):
        value.loc[0, "close"] = 99.0
        return value

    monkeypatch.setattr(
        "tradingagents.picker.ingestion.normalize_partition",
        mutate_during_normalization,
    )
    ingestor = PITIngestor(
        FakeProvider([raw_frame]),
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    ingestor._ingest_keys([key])

    payload = json.loads(cache.raw_path(key).read_text(encoding="utf-8"))
    assert payload["schema"]["primaryKey"] == ["index"]
    assert payload["data"][0]["close"] == 10.0
    assert cache.load_frame(key).loc[0, "close"] == 99.0


def test_ingest_writes_ordered_running_updates_and_complete_manifest(
    tmp_path, monkeypatch
):
    identity_normalizer(monkeypatch)
    cache = RecordingCache(tmp_path)
    provider = FakeProvider(
        [
            frame(),
            frame(),
            calendar_frame("20260710"),
            frame(),
            frame(),
            frame(),
        ]
    )
    ingestor = PITIngestor(
        provider,
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    summary = ingestor.ingest("20260710", "20260710")

    expected_keys = plan_bootstrap_partitions(
        "20260710", "20260710"
    ) + plan_daily_partitions(["20260710"])
    assert provider.calls == expected_keys
    assert summary.completed == 6
    assert summary.skipped == 0
    assert summary.failed == 0
    assert summary.run_id is not None
    assert [write.status for write in cache.run_writes] == [
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "complete",
    ]
    assert [len(write.partitions) for write in cache.run_writes] == list(range(7)) + [6]
    assert [record.key for record in cache.run_writes[-1].partitions] == expected_keys
    assert all(
        record == cache.records()[record.key.storage_key]
        for record in cache.run_writes[-1].partitions
    )
    stored = load_only_run(cache)
    assert stored["requested_start"] == "20260710"
    assert stored["requested_end"] == "20260710"
    assert stored["effective_start"] == "20260312"
    assert stored["effective_end"] == "20260710"
    assert stored["status"] == "complete"


def test_all_skipped_reruns_create_distinct_complete_manifests(tmp_path):
    cache = PITCache(tmp_path)
    for key in plan_bootstrap_partitions("20260710", "20260710"):
        value = calendar_frame("20260710") if key.dataset is Dataset.TRADE_CAL else frame()
        cache.store_complete(key, b"[]", value, "1")
    for key in plan_daily_partitions(["20260710"]):
        cache.store_complete(key, b"[]", frame(), "1")
    provider = FakeProvider([])
    ingestor = PITIngestor(
        provider,
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    first = ingestor.ingest("20260710", "20260710")
    second = ingestor.ingest("20260710", "20260710")

    assert first.skipped == second.skipped == 6
    assert first.run_id != second.run_id
    assert re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{8}", first.run_id)
    manifests = list((cache.root / "runs").glob("*.json"))
    assert len(manifests) == 2
    assert {json.loads(path.read_text())["status"] for path in manifests} == {
        "complete"
    }


def test_ordinary_failure_finalizes_failed_run_with_sanitized_error(tmp_path):
    cache = PITCache(tmp_path)
    ingestor = PITIngestor(
        FakeProvider([RuntimeError("token=super-secret")]),
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    with pytest.raises(RuntimeError):
        ingestor.ingest("20260710", "20260710")

    manifest = load_only_run(cache)
    assert manifest["status"] == "failed"
    assert manifest["partitions"][0]["status"] == "failed"
    assert manifest["error"].startswith("RuntimeError:")
    assert "super-secret" not in manifest["error"]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(2)])
def test_interrupt_finalizes_run_and_reraises_with_current_partition_pending(
    tmp_path, interruption
):
    cache = PITCache(tmp_path)
    ingestor = PITIngestor(
        FakeProvider([interruption]),
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    with pytest.raises(type(interruption)):
        ingestor.ingest("20260710", "20260710")

    manifest = load_only_run(cache)
    assert manifest["status"] == "interrupted"
    assert manifest["partitions"][0]["status"] == "pending"
    assert cache.records()["namechange/all"].status is PartitionStatus.PENDING


def test_probe_uses_trade_cal_limiter_and_same_retry_path(tmp_path):
    events = []
    provider = FakeProbeProvider(
        [TushareRateLimitError("429", retry_after=2.5), None], events
    )
    limiter = NoWaitLimiter(events)
    requested_endpoints = []

    def limiter_for(endpoint):
        requested_endpoints.append(endpoint)
        return limiter

    ingestor = PITIngestor(
        provider,
        PITCache(tmp_path),
        limiter_for,
        RetryPolicy(),
        sleeper=lambda seconds: events.append(("sleep", seconds)),
    )

    ingestor.probe()

    assert requested_endpoints == ["trade_cal"]
    assert events == [
        "acquire",
        "probe",
        "penalize",
        ("sleep", 2.5),
        "acquire",
        "probe",
    ]

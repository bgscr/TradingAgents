import json
import re
import traceback

import pandas as pd
import pytest

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import (
    PITConfigurationError,
    PITError,
    PITSchemaError,
    TushareRateLimitError,
)
from tradingagents.picker.ingestion import (
    PITIngestor,
    open_trade_dates,
    plan_bootstrap_partitions,
    plan_daily_partitions,
)
from tradingagents.picker.pit_models import Dataset, PartitionKey, PartitionStatus
from tradingagents.picker.rate_limit import RetryPolicy
from tradingagents.picker.tushare_provider import TushareProvider


class FakeProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def fetch(self, key, request_executor=None):
        def request():
            self.calls.append(key)
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        if request_executor is None:
            return request()
        return request_executor(key.dataset.value, request)


class FakeProbeProvider:
    def __init__(self, results, events=None):
        self.results = list(results)
        self.events = events

    def probe(self, request_executor=None):
        def request():
            if self.events is not None:
                self.events.append("probe")
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        if request_executor is None:
            return request()
        return request_executor(Dataset.TRADE_CAL.value, request)


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
    with pytest.raises(PITConfigurationError):
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
        def fetch(self, key, request_executor=None):
            if request_executor is None:
                events.append("fetch")
                return super().fetch(key)

            def recording_executor(endpoint, request):
                return request_executor(
                    endpoint, lambda: events.append("fetch") or request()
                )

            return super().fetch(key, recording_executor)

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


class SDKThrottle(RuntimeError):
    status_code = 429
    retry_after = 2.5


class FakeSDKClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def query(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def test_ingestor_paces_each_pagination_page_and_retries_only_throttled_page(
    tmp_path, monkeypatch
):
    identity_normalizer(monkeypatch)
    first_page = pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ"]})
    final_page = pd.DataFrame({"ts_code": ["000003.SZ"]})
    client = FakeSDKClient([first_page, SDKThrottle("429"), final_page])
    provider = TushareProvider(client, page_size=2)
    limiter = NoWaitLimiter()
    sleeps = []
    ingestor = PITIngestor(
        provider,
        PITCache(tmp_path),
        lambda endpoint: limiter,
        RetryPolicy(jitter=lambda low, high: 0.0),
        sleeper=sleeps.append,
    )

    summary = ingestor._ingest_keys(
        [PartitionKey(Dataset.DAILY, "20260710")]
    )

    assert summary.completed == 1
    assert [call[1]["offset"] for call in client.calls] == [0, 2, 2]
    assert limiter.acquires == len(client.calls) == 3
    assert limiter.penalties == 1
    assert sleeps == [2.5]


def test_ingestor_paces_all_stock_status_queries_and_probe(tmp_path, monkeypatch):
    identity_normalizer(monkeypatch)
    client = FakeSDKClient(
        [
            pd.DataFrame({"cal_date": ["20260101"]}),
            pd.DataFrame({"ts_code": ["L"]}),
            pd.DataFrame({"ts_code": ["D"]}),
            pd.DataFrame({"ts_code": ["P"]}),
        ]
    )
    provider = TushareProvider(client)
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(
        provider,
        PITCache(tmp_path),
        lambda endpoint: limiter,
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    ingestor.probe()
    summary = ingestor._ingest_keys(
        [PartitionKey(Dataset.STOCK_BASIC, "current")]
    )

    assert summary.completed == 1
    assert limiter.acquires == len(client.calls) == 4
    assert [call[0] for call in client.calls] == [
        "trade_cal",
        "stock_basic",
        "stock_basic",
        "stock_basic",
    ]
    assert [call[1].get("list_status") for call in client.calls[1:]] == [
        "L",
        "D",
        "P",
    ]


def test_stock_status_retry_does_not_replay_successful_status(tmp_path, monkeypatch):
    identity_normalizer(monkeypatch)
    client = FakeSDKClient(
        [
            pd.DataFrame({"ts_code": ["L"]}),
            SDKThrottle("429"),
            pd.DataFrame({"ts_code": ["D"]}),
            pd.DataFrame({"ts_code": ["P"]}),
        ]
    )
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(
        TushareProvider(client),
        PITCache(tmp_path),
        lambda endpoint: limiter,
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    ingestor._ingest_keys([PartitionKey(Dataset.STOCK_BASIC, "current")])

    assert [call[1]["list_status"] for call in client.calls] == ["L", "D", "D", "P"]
    assert limiter.acquires == 4
    assert limiter.penalties == 1


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

    with pytest.raises(TushareRateLimitError) as caught:
        ingestor._ingest_keys([key])

    assert len(provider.calls) == 8
    assert limiter.acquires == 8
    assert limiter.penalties == 8
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
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

    with pytest.raises(PITError, match="partition 'daily/20260710'") as caught:
        ingestor._ingest_keys([key])

    record = cache.records()[key.storage_key]
    assert record.status is PartitionStatus.FAILED
    assert record.error.startswith("RuntimeError:")
    assert "super-secret" not in record.error
    assert "token" not in record.error.casefold()
    formatted = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert "super-secret" not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


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

    with pytest.raises(PITError, match="partition 'daily/20260710'") as caught:
        ingestor._ingest_keys([key])

    error = cache.records()[key.storage_key].error
    assert "highly-sensitive-value" not in error
    assert "authorization" not in error.casefold()
    formatted = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert "highly-sensitive-value" not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


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
    marker = "TUSHARE_TOKEN"
    secret = "exact-super-secret-value"
    cache = PITCache(tmp_path)
    ingestor = PITIngestor(
        FakeProvider([RuntimeError(f"{marker}={secret}")]),
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    with pytest.raises(PITError, match="partition 'namechange/all'") as caught:
        ingestor.ingest("20260710", "20260710")

    manifest = load_only_run(cache)
    partition_error = cache.records()["namechange/all"].error
    assert manifest["status"] == "failed"
    assert manifest["partitions"][0]["status"] == "failed"
    persisted = json.dumps(
        {"partition_error": partition_error, "run_manifest": manifest}
    )
    formatted = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    for leaked in (marker, secret):
        assert leaked not in persisted
        assert leaked not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_public_ingest_final_throttle_is_sanitized_typed_and_pending(tmp_path):
    marker = "TUSHARE_TOKEN"
    secret = "throttle-super-secret-value"
    throttles = [
        TushareRateLimitError(f"{marker}={secret}", retry_after=4.5)
        for _ in range(8)
    ]
    cache = PITCache(tmp_path)
    ingestor = PITIngestor(
        FakeProvider(throttles),
        cache,
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(max_attempts=8),
        sleeper=lambda _: None,
    )

    with pytest.raises(TushareRateLimitError) as caught:
        ingestor.ingest("20260710", "20260710")

    assert caught.value.retry_after == 4.5
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    persisted = json.dumps(
        {
            "partition": cache.records()["namechange/all"].error,
            "run": load_only_run(cache),
        }
    )
    formatted = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    for leaked in (marker, secret):
        assert leaked not in persisted
        assert leaked not in formatted
    assert cache.records()["namechange/all"].status is PartitionStatus.PENDING


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


def test_probe_replaces_raw_failure_without_secret_context(tmp_path):
    marker = "TUSHARE_TOKEN"
    secret = "probe-super-secret-value"
    ingestor = PITIngestor(
        FakeProbeProvider([RuntimeError(f"{marker}={secret}")]),
        PITCache(tmp_path),
        lambda endpoint: NoWaitLimiter(),
        RetryPolicy(),
        sleeper=lambda _: None,
    )

    with pytest.raises(PITError, match="endpoint 'trade_cal'") as caught:
        ingestor.probe()

    formatted = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    for leaked in (marker, secret):
        assert leaked not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_injected_retry_policy_is_hard_capped_at_eight_calls(tmp_path):
    marker = "TUSHARE_TOKEN"
    secret = "ninth-attempt-secret"
    provider = FakeProbeProvider(
        [
            TushareRateLimitError(
                f"{marker}={secret}", retry_after=float(attempt)
            )
            for attempt in range(1, 10)
        ]
    )
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(
        provider,
        PITCache(tmp_path),
        lambda endpoint: limiter,
        RetryPolicy(max_attempts=9),
        sleeper=lambda _: None,
    )

    with pytest.raises(TushareRateLimitError) as caught:
        ingestor.probe()

    assert len(provider.results) == 1
    assert limiter.acquires == 8
    assert limiter.penalties == 8
    assert caught.value.retry_after == 8.0
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    formatted = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert marker not in formatted
    assert secret not in formatted


def test_non_positive_retry_attempt_count_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="max_attempts must be positive"):
        PITIngestor(
            FakeProbeProvider([]),
            PITCache(tmp_path),
            lambda endpoint: NoWaitLimiter(),
            RetryPolicy(max_attempts=0),
            sleeper=lambda _: None,
        )

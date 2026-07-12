# PIT Data Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Tushare-backed, paced, resumable, immutable point-in-time data foundation that reconstructs an A-share universe for a requested historical date without changing the existing keyless live picker.

**Architecture:** Add a focused `tradingagents.picker` package whose provider adapter lazily imports Tushare, whose ingestion service writes checksum-pinned raw JSON and normalized Parquet partitions, and whose snapshot builder joins only records effective by the requested cutoff. Phase 1 stops at a typed PIT snapshot and CLI; ranking, execution simulation, corporate-action accounting, and walk-forward evaluation are separate phases.

**Tech Stack:** Python 3.10+, pandas 2.3+, Typer 0.21+, Tushare 1.4.29+ (optional), PyArrow 16.1+ (optional), pytest 8+, Ruff 0.15+

## Global Constraints

- Preserve `ak_pick_a_stock.py`, the TradingAgents graph, and all existing keyless AKShare/BaoStock behavior.
- Tushare and PyArrow must remain optional under `pip install ".[pit]"`; importing `tradingagents` or running the current picker must not import either package.
- Read the token only from `TUSHARE_TOKEN`; never log, serialize, or include it in exception text.
- Use `TRADINGAGENTS_CACHE_DIR/pit/tushare` as the default root; an explicit CLI/library cache path takes precedence.
- Use one network worker and 40 calls per minute by default. `TUSHARE_<ENDPOINT>_CALLS_PER_MINUTE` overrides `TUSHARE_CALLS_PER_MINUTE`, which overrides 40.
- Retry throttling at most eight attempts, honor provider retry delay when present, otherwise use exponential backoff with jitter, and persist the partition as pending before exiting non-zero.
- Query full-market date partitions, never one request per stock.
- Store raw SDK responses as UTF-8 JSON and normalized frames as content-addressed Parquet versions. Writes, cache-index updates, and per-run manifests must be atomic.
- A completed partition is reusable only when both files exist and their SHA-256 checksums match the manifest.
- The canonical hard-filter field is `free_float_market_cap_cny = free_share * close * 10_000`; never alias it to `circ_mv`.
- Phase 1 datasets are `trade_cal`, `stock_basic`, `daily`, `daily_basic`, `namechange`, and `suspend_d`. Money flow, price limits, dividends, industry history, ranking, and execution are outside this plan.
- PIT comparisons use China calendar dates in `YYYYMMDD` form and never depend on the host timezone.
- Backfill automatically includes 120 calendar days before the requested start date for calendars and daily partitions; manifests retain the requested range and effective warm-up range separately.
- A snapshot date passes coverage only when at least 95% of reconstructed active securities have mandatory rows or an explicit suspension record.
- Use test-first Red/Green cycles and commit after every task.
- Run all shell commands through `rtk` as required by `RTK.md`.

## File Map

- Modify `pyproject.toml`: add the `pit` optional dependency group and `tradingagents-pit` console script.
- Modify `.github/workflows/ci.yml`: install `dev,pit` for tests while preserving the bare-install smoke job.
- Modify `.env.example`: document token, cache, and pacing variables without a real credential.
- Create `tradingagents/picker/__init__.py`: package exports only; no optional SDK imports.
- Create `tradingagents/picker/errors.py`: PIT-specific configuration, schema, coverage, and Tushare vendor errors.
- Create `tradingagents/picker/pit_config.py`: environment/argument precedence and endpoint pacing.
- Create `tradingagents/picker/pit_models.py`: datasets, partition keys/status, cache records, ingestion-run manifests, summary, and snapshot result types.
- Create `tradingagents/picker/rate_limit.py`: injected-clock token bucket and retry delay policy.
- Create `tradingagents/picker/cache.py`: atomic raw/Parquet storage, checksums, manifest, and resume checks.
- Create `tradingagents/picker/tushare_provider.py`: lazy SDK creation, endpoint catalog, capability checks, and pagination.
- Create `tradingagents/picker/normalize.py`: schema/date/unit normalization and canonical market-cap validation.
- Create `tradingagents/picker/ingestion.py`: partition planning, pacing, retry, normalization, persistence, and resume.
- Create `tradingagents/picker/snapshot.py`: as-of universe joins, ST/suspension state, coverage, and base eligibility reasons.
- Create `tradingagents/picker/cli.py`: `backfill` and `snapshot` Typer commands.
- Create `docs/pit-data-foundation.md`: install, configuration, cache, resume, and CLI reference.
- Create focused tests under `tests/test_picker_pit_*.py` plus one credential-gated integration test.

---

### Task 1: Optional dependency, configuration, and error contracts

**Files:**
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `.env.example`
- Create: `tradingagents/picker/__init__.py`
- Create: `tradingagents/picker/errors.py`
- Create: `tradingagents/picker/pit_config.py`
- Test: `tests/test_picker_pit_config.py`

**Interfaces:**
- Consumes: `TRADINGAGENTS_CACHE_DIR`, `TUSHARE_TOKEN`, `TUSHARE_CALLS_PER_MINUTE`, and optional endpoint-specific pacing variables.
- Produces: `PITConfig.from_env(cache_dir: Path | None = None, calls_per_minute: int | None = None) -> PITConfig` and `PITConfig.rate_for(endpoint: str) -> int`.
- Produces: `PITConfigurationError`, `PITSchemaError`, `PITCoverageError`, `TushareNotConfiguredError`, and `TushareRateLimitError`.

- [ ] **Step 1: Write failing configuration tests**

```python
# tests/test_picker_pit_config.py
from pathlib import Path

import pytest

from tradingagents.picker.errors import PITConfigurationError
from tradingagents.picker.pit_config import PITConfig


def test_config_uses_explicit_values_before_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")
    monkeypatch.setenv("TUSHARE_CALLS_PER_MINUTE", "99")
    config = PITConfig.from_env(cache_dir=tmp_path, calls_per_minute=12)
    assert config.token == "env-token"
    assert config.cache_dir == tmp_path.resolve()
    assert config.calls_per_minute == 12


def test_config_defaults_under_tradingagents_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "token")
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("TUSHARE_CALLS_PER_MINUTE", raising=False)
    config = PITConfig.from_env()
    assert config.cache_dir == (tmp_path / "pit" / "tushare").resolve()
    assert config.calls_per_minute == 40
    assert config.max_attempts == 8


def test_endpoint_rate_overrides_global(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "token")
    monkeypatch.setenv("TUSHARE_CALLS_PER_MINUTE", "40")
    monkeypatch.setenv("TUSHARE_DAILY_BASIC_CALLS_PER_MINUTE", "20")
    config = PITConfig.from_env(cache_dir=tmp_path)
    assert config.rate_for("daily_basic") == 20
    assert config.rate_for("daily") == 40


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_rate_is_rejected(monkeypatch, tmp_path, value):
    monkeypatch.setenv("TUSHARE_TOKEN", "token")
    monkeypatch.setenv("TUSHARE_CALLS_PER_MINUTE", value)
    with pytest.raises(PITConfigurationError, match="positive integer"):
        PITConfig.from_env(cache_dir=tmp_path)
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `rtk pytest -q tests/test_picker_pit_config.py`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'tradingagents.picker'`.

- [ ] **Step 3: Add the optional dependencies and console script**

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
pit = [
    "tushare>=1.4.29,<2",
    "pyarrow>=16.1,<26",
]

[project.scripts]
tradingagents = "cli.main:app"
tradingagents-pit = "tradingagents.picker.cli:app"
```

Preserve the existing `build`, `dev`, and `bedrock` groups; add `pit` without replacing them.

Change only the test job's editable install in `.github/workflows/ci.yml`:

```yaml
- name: Install (with dev and PIT extras)
  run: |
    python -m pip install --upgrade pip
    pip install -e ".[dev,pit]"
```

Keep `smoke-install` on `pip install .`; that job proves the package and current CLI import without Tushare or PyArrow.

- [ ] **Step 4: Implement errors and configuration**

```python
# tradingagents/picker/errors.py
from tradingagents.dataflows.errors import (
    VendorNotConfiguredError,
    VendorRateLimitError,
)


class PITError(Exception):
    """Base error for the point-in-time data foundation."""


class PITConfigurationError(PITError, ValueError):
    """Invalid local PIT configuration."""


class PITSchemaError(PITError):
    """A provider partition violates its normalized schema."""


class PITCoverageError(PITError):
    """A snapshot or fold does not meet the required data coverage."""


class TushareNotConfiguredError(VendorNotConfiguredError, PITError):
    """The PIT extra or TUSHARE_TOKEN is unavailable."""


class TushareRateLimitError(VendorRateLimitError, PITError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after
```

```python
# tradingagents/picker/pit_config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import PITConfigurationError


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise PITConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise PITConfigurationError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class PITConfig:
    token: str | None
    cache_dir: Path
    calls_per_minute: int
    endpoint_rates: dict[str, int]
    max_attempts: int = 8

    @classmethod
    def from_env(
        cls,
        cache_dir: Path | None = None,
        calls_per_minute: int | None = None,
    ) -> "PITConfig":
        root = Path(os.environ.get("TRADINGAGENTS_CACHE_DIR", Path.home() / ".tradingagents" / "cache"))
        resolved_cache = (cache_dir or root / "pit" / "tushare").expanduser().resolve()
        global_rate = (
            _positive_int(str(calls_per_minute), "--calls-per-minute")
            if calls_per_minute is not None
            else _positive_int(
                os.environ.get("TUSHARE_CALLS_PER_MINUTE", "40"),
                "TUSHARE_CALLS_PER_MINUTE",
            )
        )
        endpoint_rates = {}
        prefix = "TUSHARE_"
        suffix = "_CALLS_PER_MINUTE"
        for name, raw in os.environ.items():
            if name.startswith(prefix) and name.endswith(suffix) and name != "TUSHARE_CALLS_PER_MINUTE":
                endpoint = name[len(prefix) : -len(suffix)].lower()
                endpoint_rates[endpoint] = _positive_int(raw, name)
        return cls(
            token=os.environ.get("TUSHARE_TOKEN") or None,
            cache_dir=resolved_cache,
            calls_per_minute=global_rate,
            endpoint_rates=endpoint_rates,
        )

    def rate_for(self, endpoint: str) -> int:
        return self.endpoint_rates.get(endpoint.lower(), self.calls_per_minute)
```

Keep `tradingagents/picker/__init__.py` free of Tushare/PyArrow imports:

```python
"""Point-in-time data foundation for the A-share picker."""

from .pit_config import PITConfig

__all__ = ["PITConfig"]
```

- [ ] **Step 5: Document environment keys without secrets**

Append to `.env.example`:

```dotenv
# Optional PIT backtesting data (install with: pip install ".[pit]")
TUSHARE_TOKEN=
TUSHARE_CALLS_PER_MINUTE=40
# Optional endpoint override, for example:
# TUSHARE_DAILY_BASIC_CALLS_PER_MINUTE=20
```

- [ ] **Step 6: Run tests and lint, then commit**

Run: `rtk pytest -q tests/test_picker_pit_config.py`

Expected: `6 passed`.

Run: `rtk ruff check tradingagents/picker tests/test_picker_pit_config.py`

Expected: no findings.

```powershell
rtk git add pyproject.toml .github/workflows/ci.yml .env.example tradingagents/picker tests/test_picker_pit_config.py
rtk git commit -m "feat: add PIT configuration contracts"
```

---

### Task 2: Typed dataset and partition models

**Files:**
- Create: `tradingagents/picker/pit_models.py`
- Test: `tests/test_picker_pit_models.py`

**Interfaces:**
- Consumes: only standard-library types.
- Produces: `Dataset`, `PartitionStatus`, `PartitionKey`, `PartitionRecord`, `Manifest`, `IngestionRunManifest`, `IngestionSummary`, and `PITSnapshot`.
- `PartitionKey.storage_key` is the sole cache-relative identifier consumed by Task 4.

- [ ] **Step 1: Write failing model tests**

```python
# tests/test_picker_pit_models.py
from tradingagents.picker.pit_models import (
    Dataset,
    IngestionRunManifest,
    Manifest,
    PartitionKey,
    PartitionRecord,
    PartitionStatus,
)


def test_partition_key_has_stable_safe_storage_key():
    key = PartitionKey(Dataset.DAILY_BASIC, "20260710")
    assert key.storage_key == "daily_basic/20260710"


def test_manifest_round_trip_preserves_partition_record():
    key = PartitionKey(Dataset.DAILY, "20260710")
    record = PartitionRecord(
        key=key,
        status=PartitionStatus.COMPLETE,
        raw_sha256="a" * 64,
        normalized_sha256="b" * 64,
        row_count=2,
        fetched_at="2026-07-12T09:00:00Z",
        schema_version="1",
        error=None,
        raw_path="raw/daily/20260710/a.json",
        normalized_path="normalized/daily/20260710/b.parquet",
    )
    restored = Manifest.from_dict(Manifest(partitions={key.storage_key: record}).to_dict())
    assert restored.partitions[key.storage_key] == record


def test_ingestion_run_manifest_pins_partition_versions():
    record = PartitionRecord(
        key=PartitionKey(Dataset.DAILY, "20260710"),
        status=PartitionStatus.COMPLETE,
        raw_sha256="a" * 64,
        normalized_sha256="b" * 64,
        raw_path="raw/daily/20260710/a.json",
        normalized_path="normalized/daily/20260710/b.parquet",
    )
    manifest = IngestionRunManifest(
        run_id="20260712T090000Z-deadbeef",
        requested_start="20260710",
        requested_end="20260710",
        effective_start="20260312",
        effective_end="20260710",
        created_at="2026-07-12T09:00:00Z",
        partitions=(record,),
        status="complete",
        error=None,
    )
    assert IngestionRunManifest.from_dict(manifest.to_dict()) == manifest


def test_partition_key_rejects_unsafe_partition():
    try:
        PartitionKey(Dataset.DAILY, "../escape")
    except ValueError as exc:
        assert "partition" in str(exc)
    else:
        raise AssertionError("unsafe partition was accepted")
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `rtk pytest -q tests/test_picker_pit_models.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'tradingagents.picker.pit_models'`.

- [ ] **Step 3: Implement immutable typed models**

Use string enums and frozen dataclasses. Define exactly these datasets for Phase 1:

```python
class Dataset(str, Enum):
    TRADE_CAL = "trade_cal"
    STOCK_BASIC = "stock_basic"
    DAILY = "daily"
    DAILY_BASIC = "daily_basic"
    NAMECHANGE = "namechange"
    SUSPEND_D = "suspend_d"


class PartitionStatus(str, Enum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"
```

Implement `PartitionKey` with a full-match validation pattern of `[A-Za-z0-9_-]+` and the exact property:

```python
_PARTITION_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class PartitionKey:
    dataset: Dataset
    partition: str

    def __post_init__(self) -> None:
        if not _PARTITION_RE.fullmatch(self.partition):
            raise ValueError(f"unsafe partition: {self.partition!r}")

    @property
    def storage_key(self) -> str:
        return f"{self.dataset.value}/{self.partition}"
```

Define `PartitionRecord` and `Manifest` with these exact fields. `Manifest.to_dict()` and `Manifest.from_dict()` must serialize enum values as strings and reconstruct nested keys/records:

```python
@dataclass(frozen=True)
class PartitionRecord:
    key: PartitionKey
    status: PartitionStatus
    raw_sha256: str | None = None
    normalized_sha256: str | None = None
    row_count: int = 0
    fetched_at: str | None = None
    schema_version: str = "1"
    error: str | None = None
    raw_path: str | None = None
    normalized_path: str | None = None


@dataclass(frozen=True)
class Manifest:
    partitions: dict[str, PartitionRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "partitions": {
                storage_key: {
                    "dataset": record.key.dataset.value,
                    "partition": record.key.partition,
                    "status": record.status.value,
                    "raw_sha256": record.raw_sha256,
                    "normalized_sha256": record.normalized_sha256,
                    "row_count": record.row_count,
                    "fetched_at": record.fetched_at,
                    "schema_version": record.schema_version,
                    "error": record.error,
                    "raw_path": record.raw_path,
                    "normalized_path": record.normalized_path,
                }
                for storage_key, record in sorted(self.partitions.items())
            }
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "Manifest":
        records = {}
        for storage_key, raw in dict(value.get("partitions", {})).items():
            key = PartitionKey(Dataset(raw["dataset"]), raw["partition"])
            records[storage_key] = PartitionRecord(
                key=key,
                status=PartitionStatus(raw["status"]),
                raw_sha256=raw.get("raw_sha256"),
                normalized_sha256=raw.get("normalized_sha256"),
                row_count=int(raw.get("row_count", 0)),
                fetched_at=raw.get("fetched_at"),
                schema_version=str(raw.get("schema_version", "1")),
                error=raw.get("error"),
                raw_path=raw.get("raw_path"),
                normalized_path=raw.get("normalized_path"),
            )
        return cls(partitions=records)
```

The two serialization method bodies must explicitly map every field above; do not use `pickle`, `asdict()` on enums, or dynamic object hooks. Add:

```python
@dataclass(frozen=True)
class IngestionSummary:
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    run_id: str | None = None


@dataclass(frozen=True)
class IngestionRunManifest:
    run_id: str
    requested_start: str
    requested_end: str
    effective_start: str
    effective_end: str
    created_at: str
    partitions: tuple[PartitionRecord, ...]
    status: str
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"running", "complete", "failed", "interrupted"}:
            raise ValueError(f"invalid run status: {self.status!r}")

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "effective_start": self.effective_start,
            "effective_end": self.effective_end,
            "created_at": self.created_at,
            "status": self.status,
            "error": self.error,
            "partitions": [
                Manifest({record.key.storage_key: record}).to_dict()["partitions"][record.key.storage_key]
                for record in self.partitions
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "IngestionRunManifest":
        records = []
        for raw in value["partitions"]:
            storage_key = f'{raw["dataset"]}/{raw["partition"]}'
            record = Manifest.from_dict({"partitions": {storage_key: raw}}).partitions[storage_key]
            records.append(record)
        return cls(
            run_id=str(value["run_id"]),
            requested_start=str(value["requested_start"]),
            requested_end=str(value["requested_end"]),
            effective_start=str(value["effective_start"]),
            effective_end=str(value["effective_end"]),
            created_at=str(value["created_at"]),
            partitions=tuple(records),
            status=str(value["status"]),
            error=value.get("error"),
        )


@dataclass(frozen=True)
class PITSnapshot:
    as_of: str
    universe: pd.DataFrame
    coverage: float
    warnings: tuple[str, ...] = ()
```

Import pandas only in `pit_models.py`; do not import Tushare or PyArrow.

- [ ] **Step 4: Run tests and commit**

Run: `rtk pytest -q tests/test_picker_pit_models.py`

Expected: `4 passed`.

```powershell
rtk git add tradingagents/picker/pit_models.py tests/test_picker_pit_models.py
rtk git commit -m "feat: define PIT partition models"
```

---

### Task 3: Token-bucket pacing and retry policy

**Files:**
- Create: `tradingagents/picker/rate_limit.py`
- Test: `tests/test_picker_rate_limit.py`

**Interfaces:**
- Consumes: positive calls-per-minute from `PITConfig.rate_for()`.
- Produces: `TokenBucketLimiter.acquire() -> None`, `TokenBucketLimiter.penalize() -> None`, and `RetryPolicy.delay(attempt: int, retry_after: float | None) -> float`.

- [ ] **Step 1: Write failing deterministic tests with an injected clock**

```python
# tests/test_picker_rate_limit.py
import pytest

from tradingagents.picker.rate_limit import RetryPolicy


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_limiter_paces_calls_without_real_sleep():
    clock = FakeClock()
    limiter = TokenBucketLimiter(60, clock=clock.monotonic, sleeper=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    assert clock.sleeps == [pytest.approx(1.0)]


def test_penalize_halves_effective_rate():
    clock = FakeClock()
    limiter = TokenBucketLimiter(40, clock=clock.monotonic, sleeper=clock.sleep)
    limiter.penalize()
    assert limiter.calls_per_minute == 20


def test_retry_policy_honors_provider_delay():
    policy = RetryPolicy(max_attempts=8, jitter=lambda low, high: 0.0)
    assert policy.delay(attempt=3, retry_after=12.0) == 12.0


def test_retry_policy_caps_exponential_delay():
    policy = RetryPolicy(base_delay=1.0, max_delay=60.0, jitter=lambda low, high: 0.0)
    assert policy.delay(attempt=20, retry_after=None) == 60.0
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `rtk pytest -q tests/test_picker_rate_limit.py`

Expected: FAIL because `tradingagents.picker.rate_limit` does not exist.

- [ ] **Step 3: Implement the minimal pacing classes**

`TokenBucketLimiter` starts with one immediately available call. After each call, the next token becomes available at `60 / calls_per_minute` seconds. `penalize()` halves the integer rate with a floor of one and recalculates the interval. Reject non-positive rates with `ValueError`.

`RetryPolicy` is a frozen dataclass with `max_attempts=8`, `base_delay=1.0`, `max_delay=60.0`, and injected `jitter`. Without a provider delay, return:

```python
min(max_delay, base_delay * (2 ** (attempt - 1))) + jitter(0.0, 0.25)
```

- [ ] **Step 4: Run tests, lint, and commit**

Run: `rtk pytest -q tests/test_picker_rate_limit.py`

Expected: `4 passed`.

```powershell
rtk git add tradingagents/picker/rate_limit.py tests/test_picker_rate_limit.py
rtk git commit -m "feat: pace PIT provider requests"
```

---

### Task 4: Immutable partition cache and manifest resume checks

**Files:**
- Create: `tradingagents/picker/cache.py`
- Test: `tests/test_picker_pit_cache.py`

**Interfaces:**
- Consumes: `PartitionKey`, `PartitionRecord`, `PartitionStatus`, and pandas frames.
- Produces: `PITCache.mark_pending()`, `PITCache.store_complete()`, `PITCache.mark_failed()`, `PITCache.is_complete()`, `PITCache.load_frame()`, `PITCache.records()`, and `PITCache.write_run_manifest()`.

- [ ] **Step 1: Write failing cache tests**

```python
# tests/test_picker_pit_cache.py
from dataclasses import replace

import pandas as pd
import pytest

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import PITSchemaError
from tradingagents.picker.pit_models import (
    Dataset,
    IngestionRunManifest,
    PartitionKey,
    PartitionStatus,
)


def test_store_complete_round_trips_and_verifies_checksums(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY_BASIC, "20260710")
    frame = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260710"]})
    cache.store_complete(key, frame.to_json(orient="records").encode(), frame, "1")
    assert cache.is_complete(key)
    pd.testing.assert_frame_equal(cache.load_frame(key), frame)


def test_corrupt_file_is_not_resumable(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    frame = pd.DataFrame({"ts_code": ["000001.SZ"], "close": [10.0]})
    cache.store_complete(key, b"[]", frame, "1")
    cache.normalized_path(key).write_bytes(b"corrupt")
    assert not cache.is_complete(key)


def test_pending_and_failed_status_survive_reload(tmp_path):
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache = PITCache(tmp_path)
    cache.mark_pending(key)
    assert PITCache(tmp_path).records()[key.storage_key].status is PartitionStatus.PENDING
    cache.mark_failed(key, "throttled")
    assert PITCache(tmp_path).records()[key.storage_key].error == "throttled"


def test_new_content_creates_new_version_without_removing_old_files(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    first = cache.store_complete(key, b'[{"v": 1}]', pd.DataFrame({"v": [1]}), "1")
    second = cache.store_complete(key, b'[{"v": 2}]', pd.DataFrame({"v": [2]}), "1")
    assert first.raw_path != second.raw_path
    assert (tmp_path / first.raw_path).exists()
    assert (tmp_path / first.normalized_path).exists()
    assert cache.records()[key.storage_key] == second


def test_completed_run_manifest_is_immutable(tmp_path):
    cache = PITCache(tmp_path)
    manifest = IngestionRunManifest(
        run_id="run-1", requested_start="20260710", requested_end="20260710",
        effective_start="20260312", effective_end="20260710",
        created_at="2026-07-12T09:00:00Z", partitions=(), status="complete",
    )
    cache.write_run_manifest(manifest)
    with pytest.raises(PITSchemaError, match="immutable"):
        cache.write_run_manifest(replace(manifest, error="changed"))
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `rtk pytest -q tests/test_picker_pit_cache.py`

Expected: FAIL because `tradingagents.picker.cache` does not exist.

- [ ] **Step 3: Implement atomic storage**

Use this on-disk layout:

```text
<cache-root>/
  manifest.json
  raw/<dataset>/<partition>/<sha256>.json
  normalized/<dataset>/<partition>/<sha256>.parquet
  runs/<run-id>.json
```

Implement a private atomic writer that creates a temporary file in the target directory, flushes and `os.fsync()`s it, then calls `os.replace()`. For Parquet, write to a temporary sibling path with `frame.to_parquet(index=False)`, fsync the file, and replace it.

`store_complete()` must:

1. write raw and normalized temporary files;
2. compute SHA-256 before choosing each content-addressed final path;
3. move each file to its hash path only when that exact version is absent;
4. create a complete `PartitionRecord` containing both checksums, relative
   paths, row count,
   schema version, and a UTC `fetched_at` ending in `Z`;
5. atomically update `manifest.json` to point at the newest record; and
6. leave old content-addressed files intact and no temporary files after success.

`is_complete()` returns false unless status is complete, both files exist, and both checksums match. `load_frame()` raises `PITSchemaError` when verification fails.

`load_frame(key, normalized_sha256=None)` loads the latest record when no hash
is supplied and the pinned version when a hash is supplied.
`write_run_manifest()` writes `IngestionRunManifest` to `runs/<run-id>.json`
atomically. It may replace the same run only while the stored status is
`running`; a `complete`, `failed`, or `interrupted` run manifest is immutable.

Expose the exact public methods `__init__(root: Path)`, `raw_path(key)`,
`normalized_path(key)`, `records()`, `mark_pending(key)`,
`mark_failed(key, error)`, `store_complete(key, raw_payload, frame,
schema_version)`, `is_complete(key)`, `load_frame(key,
normalized_sha256=None)`, and `write_run_manifest(manifest)`. Return a
`PartitionRecord` from `store_complete()` and a defensive copy of the manifest
mapping from `records()`.

- [ ] **Step 4: Run tests and commit**

Run: `rtk pytest -q tests/test_picker_pit_cache.py`

Expected: `5 passed`.

```powershell
rtk git add tradingagents/picker/cache.py tests/test_picker_pit_cache.py
rtk git commit -m "feat: cache immutable PIT partitions"
```

---

### Task 5: Lazy Tushare provider, endpoint catalog, and pagination

**Files:**
- Create: `tradingagents/picker/tushare_provider.py`
- Test: `tests/test_tushare_pit_provider.py`

**Interfaces:**
- Consumes: `PITConfig.token`, Phase 1 `Dataset`, and `PartitionKey`.
- Produces: `TushareProvider.create(config, client=None) -> TushareProvider`, `probe() -> None`, and `fetch(key: PartitionKey) -> pd.DataFrame`.

- [ ] **Step 1: Write failing provider tests with a fake SDK client**

```python
# tests/test_tushare_pit_provider.py
import pandas as pd
import pytest

from tradingagents.picker.errors import TushareNotConfiguredError, TushareRateLimitError
from tradingagents.picker.pit_config import PITConfig
from tradingagents.picker.pit_models import Dataset, PartitionKey
from tradingagents.picker.tushare_provider import TushareProvider


class FakePro:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def query(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def config(tmp_path, token="token"):
    return PITConfig(token, tmp_path, 40, {}, 8)


def test_missing_token_fails_without_importing_sdk(tmp_path):
    with pytest.raises(TushareNotConfiguredError, match="TUSHARE_TOKEN"):
        TushareProvider.create(config(tmp_path, token=None), client=None)


def test_daily_basic_uses_trade_date_and_requested_fields(tmp_path):
    client = FakePro([pd.DataFrame({"ts_code": ["000001.SZ"]})])
    provider = TushareProvider.create(config(tmp_path), client=client)
    provider.fetch(PartitionKey(Dataset.DAILY_BASIC, "20260710"))
    endpoint, kwargs = client.calls[0]
    assert endpoint == "daily_basic"
    assert kwargs["trade_date"] == "20260710"
    assert "free_share" in kwargs["fields"]


def test_full_page_is_followed_by_next_offset(tmp_path):
    first = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(2)]})
    second = pd.DataFrame({"ts_code": ["999999.SZ"]})
    client = FakePro([first, second])
    provider = TushareProvider.create(config(tmp_path), client=client, page_size=2)
    out = provider.fetch(PartitionKey(Dataset.DAILY, "20260710"))
    assert len(out) == 3
    assert [call[1]["offset"] for call in client.calls] == [0, 2]


def test_trade_calendar_year_uses_explicit_date_range(tmp_path):
    client = FakePro([pd.DataFrame({
        "cal_date": ["20260709", "20260710", "20260711"],
        "is_open": [1, 1, 0],
    })])
    provider = TushareProvider.create(config(tmp_path), client=client)
    out = provider.fetch(PartitionKey(Dataset.TRADE_CAL, "2026"))
    assert len(out) == 3
    assert client.calls[0][0] == "trade_cal"
    assert client.calls[0][1]["start_date"] == "20260101"
    assert client.calls[0][1]["end_date"] == "20261231"


@pytest.mark.parametrize("message", ["Too Many Requests", "每分钟最多访问该接口"])
def test_throttle_messages_become_typed_error(tmp_path, message):
    client = FakePro([RuntimeError(message)])
    provider = TushareProvider.create(config(tmp_path), client=client)
    with pytest.raises(TushareRateLimitError):
        provider.fetch(PartitionKey(Dataset.DAILY, "20260710"))
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `rtk pytest -q tests/test_tushare_pit_provider.py`

Expected: FAIL because `tradingagents.picker.tushare_provider` does not exist.

- [ ] **Step 3: Implement lazy SDK creation and endpoint specs**

Create an `EndpointSpec` frozen dataclass with `api_name`, `date_arg`, and `fields`. Use these exact Phase 1 fields:

```python
ENDPOINTS = {
    Dataset.TRADE_CAL: EndpointSpec("trade_cal", None, "exchange,cal_date,is_open,pretrade_date"),
    Dataset.STOCK_BASIC: EndpointSpec(
        "stock_basic", None,
        "ts_code,symbol,name,area,industry,market,list_status,list_date,delist_date,is_hs",
    ),
    Dataset.DAILY: EndpointSpec(
        "daily", "trade_date",
        "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
    ),
    Dataset.DAILY_BASIC: EndpointSpec(
        "daily_basic", "trade_date",
        "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,"
        "total_share,float_share,free_share,total_mv,circ_mv",
    ),
    Dataset.NAMECHANGE: EndpointSpec(
        "namechange", None,
        "ts_code,name,start_date,end_date,ann_date,change_reason",
    ),
    Dataset.SUSPEND_D: EndpointSpec(
        "suspend_d", "suspend_date",
        "ts_code,suspend_date,resume_date,ann_date,suspend_reason,reason_type",
    ),
}
```

`TushareProvider.create()` must check the token before importing. With no injected client, use `importlib.import_module("tushare")` and `module.pro_api(config.token)`. Convert `ImportError` to a `TushareNotConfiguredError` that says `pip install ".[pit]"` but never includes the token.

For date datasets, pass the partition through the endpoint's date argument. For `trade_cal`, interpret a partition like `2026` as `start_date=20260101`, `end_date=20261231`, `exchange="SSE"`. For `stock_basic/current`, query `list_status` values `L`, `D`, and `P`, then concatenate and de-duplicate by all returned fields. For `namechange/all`, query the complete endpoint without a date filter and rely on pagination; a requested backfill range must not discard a name interval that began earlier.

Page through `client.query()` with `limit=page_size` and `offset`. Stop only when a page has fewer rows than `page_size`. De-duplicate the concatenated frame and raise `PITSchemaError` if a full page repeats without adding rows.

Classify throttling by exception status `429` or case-insensitive message fragments `too many requests`, `rate limit`, `频次`, and `每分钟`. Preserve an available numeric `retry_after` attribute.

`probe()` fetches one `trade_cal` row for the current calendar year and raises an endpoint-named `PITConfigurationError` if the token lacks permission.

- [ ] **Step 4: Run tests and commit**

Run: `rtk pytest -q tests/test_tushare_pit_provider.py`

Expected: `6 passed`.

```powershell
rtk git add tradingagents/picker/tushare_provider.py tests/test_tushare_pit_provider.py
rtk git commit -m "feat: add Tushare PIT provider"
```

---

### Task 6: Dataset normalization and canonical market-cap schema

**Files:**
- Create: `tradingagents/picker/normalize.py`
- Test: `tests/test_picker_pit_normalize.py`

**Interfaces:**
- Consumes: raw provider frames and `Dataset`.
- Produces: `normalize_partition(dataset: Dataset, frame: pd.DataFrame) -> pd.DataFrame` with normalized dates, CNY units, canonical market-cap fields, and stable sorting.

- [ ] **Step 1: Write failing normalization tests**

```python
# tests/test_picker_pit_normalize.py
import pandas as pd
import pytest

from tradingagents.picker.errors import PITSchemaError
from tradingagents.picker.normalize import normalize_partition
from tradingagents.picker.pit_models import Dataset


def daily_basic_row(**overrides):
    row = {
        "ts_code": "000001.SZ",
        "trade_date": "20260710",
        "close": 10.0,
        "total_share": 120_000.0,
        "float_share": 100_000.0,
        "free_share": 40_000.0,
        "circ_mv": 1_000_000.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_daily_basic_exposes_distinct_canonical_market_caps():
    out = normalize_partition(Dataset.DAILY_BASIC, daily_basic_row())
    assert out.loc[0, "circ_market_cap_cny"] == 10_000_000_000.0
    assert out.loc[0, "free_float_market_cap_cny"] == 4_000_000_000.0


@pytest.mark.parametrize(
    "field,value",
    [("free_share", 100_001.0), ("float_share", 120_001.0)],
)
def test_invalid_share_order_fails_closed(field, value):
    with pytest.raises(PITSchemaError, match="share count"):
        normalize_partition(Dataset.DAILY_BASIC, daily_basic_row(**{field: value}))


def test_circ_mv_reconciliation_uses_declared_units():
    with pytest.raises(PITSchemaError, match="circ_market_cap_cny"):
        normalize_partition(Dataset.DAILY_BASIC, daily_basic_row(circ_mv=800_000.0))


def test_daily_amount_is_converted_from_thousand_cny():
    raw = pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20260710"],
        "open": [9.8], "high": [10.2], "low": [9.7], "close": [10.0],
        "pre_close": [9.9], "vol": [1_000.0], "amount": [123.0],
    })
    out = normalize_partition(Dataset.DAILY, raw)
    assert out.loc[0, "amount_cny"] == 123_000.0


def test_empty_suspend_partition_is_valid():
    out = normalize_partition(Dataset.SUSPEND_D, pd.DataFrame())
    assert out.empty
    assert list(out.columns) == [
        "ts_code", "suspend_date", "resume_date", "ann_date",
        "suspend_reason", "reason_type",
    ]
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `rtk pytest -q tests/test_picker_pit_normalize.py`

Expected: FAIL because `tradingagents.picker.normalize` does not exist.

- [ ] **Step 3: Implement schema validation and normalization**

Define required columns per Phase 1 dataset. Convert date columns to zero-padded `YYYYMMDD` strings and reject unparseable values rather than using the host timezone. Convert numeric columns with `pd.to_numeric(errors="coerce")` and fail rows missing required values.

An empty `suspend_d` response is a valid no-events partition: return an empty
frame with the declared normalized columns. Empty `trade_cal`, `stock_basic`,
`daily`, or `daily_basic` partitions are schema/data failures for their
requested open-date/static contracts.

For `daily_basic` calculate:

```python
out["circ_market_cap_cny"] = out["circ_mv"] * 10_000.0
out["free_float_market_cap_cny"] = out["free_share"] * out["close"] * 10_000.0
derived_circ = out["float_share"] * out["close"] * 10_000.0
tolerance = pd.concat(
    [pd.Series(10_000.0, index=out.index), out["circ_market_cap_cny"].abs() * 0.001],
    axis=1,
).max(axis=1)
```

Reject rows where share ordering is invalid or `(derived_circ - circ_market_cap_cny).abs() > tolerance`. Preserve raw share/cap fields as provenance.

For `daily`, add `amount_cny = amount * 1_000.0` and preserve raw `amount`. Sort every normalized frame by its date column and `ts_code`, reset the index, and de-duplicate on the natural key.

- [ ] **Step 4: Run tests and commit**

Run: `rtk pytest -q tests/test_picker_pit_normalize.py`

Expected: `6 passed`.

```powershell
rtk git add tradingagents/picker/normalize.py tests/test_picker_pit_normalize.py
rtk git commit -m "feat: normalize PIT market data"
```

---

### Task 7: Resumable ingestion orchestration

**Files:**
- Create: `tradingagents/picker/ingestion.py`
- Test: `tests/test_picker_pit_ingestion.py`

**Interfaces:**
- Consumes: `PITConfig`, `TushareProvider`, `PITCache`, an endpoint limiter factory, `RetryPolicy`, and `normalize_partition()`.
- Produces: `PITIngestor.ingest(start_date: str, end_date: str, refresh: bool = False) -> IngestionSummary` and an atomically finalized `IngestionRunManifest`.
- Produces: `PITIngestor.probe() -> None`, using the same limiter and retry path as ingestion.
- Produces: `plan_bootstrap_partitions(start_date, end_date) -> list[PartitionKey]`, `open_trade_dates(cache, start_date, end_date) -> list[str]`, and `plan_daily_partitions(open_dates) -> list[PartitionKey]`.

- [ ] **Step 1: Write failing orchestration tests**

```python
# tests/test_picker_pit_ingestion.py
import pandas as pd
import pytest

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import TushareRateLimitError
from tradingagents.picker.ingestion import (
    PITIngestor,
    open_trade_dates,
    plan_bootstrap_partitions,
    plan_daily_partitions,
)
from tradingagents.picker.pit_models import Dataset, PartitionKey
from tradingagents.picker.rate_limit import RetryPolicy, TokenBucketLimiter


class FakeProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def fetch(self, key):
        self.calls.append(key)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class NoWaitLimiter:
    def __init__(self):
        self.acquires = 0
        self.penalties = 0

    def acquire(self):
        self.acquires += 1

    def penalize(self):
        self.penalties += 1


def frame(code="000001.SZ"):
    return pd.DataFrame({"ts_code": [code], "trade_date": ["20260710"], "close": [10.0], "amount": [100.0]})


def test_partition_planning_uses_only_cached_calendar(tmp_path):
    cache = PITCache(tmp_path)
    calendar_key = PartitionKey(Dataset.TRADE_CAL, "2026")
    calendar = pd.DataFrame({
        "cal_date": ["20260709", "20260710", "20260711"],
        "is_open": [1, 1, 0],
    })
    cache.store_complete(calendar_key, b"[]", calendar, "1")
    bootstrap = plan_bootstrap_partitions("20260709", "20260711")
    assert PartitionKey(Dataset.NAMECHANGE, "all") in bootstrap
    dates = open_trade_dates(cache, "20260709", "20260711")
    assert dates == ["20260709", "20260710"]
    assert plan_daily_partitions(dates)[0].partition == "20260709"


def test_completed_partition_is_skipped_on_resume(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache.store_complete(key, b"[]", frame(), "1")
    provider = FakeProvider([])
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(provider, cache, lambda endpoint: limiter, RetryPolicy(), sleeper=lambda _: None)
    summary = ingestor._ingest_keys([key])
    assert summary.skipped == 1
    assert provider.calls == []


def test_refresh_fetches_new_content_addressed_version(tmp_path, monkeypatch):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache.store_complete(key, b'[{"close": 9}]', frame(), "1")
    provider = FakeProvider([frame("000002.SZ")])
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(provider, cache, lambda endpoint: limiter, RetryPolicy(), sleeper=lambda _: None)
    monkeypatch.setattr("tradingagents.picker.ingestion.normalize_partition", lambda dataset, value: value)
    summary = ingestor._ingest_keys([key], refresh=True)
    assert summary.completed == 1
    assert provider.calls == [key]


def test_throttle_then_success_penalizes_and_completes(tmp_path, monkeypatch):
    key = PartitionKey(Dataset.DAILY, "20260710")
    provider = FakeProvider([TushareRateLimitError("429", retry_after=3.0), frame()])
    limiter = NoWaitLimiter()
    sleeps = []
    ingestor = PITIngestor(provider, PITCache(tmp_path), lambda endpoint: limiter, RetryPolicy(), sleeper=sleeps.append)
    monkeypatch.setattr("tradingagents.picker.ingestion.normalize_partition", lambda dataset, value: value)
    summary = ingestor._ingest_keys([key])
    assert summary.completed == 1
    assert sleeps == [3.0]
    assert limiter.penalties == 1


def test_eighth_throttle_leaves_pending_and_exits(tmp_path):
    key = PartitionKey(Dataset.DAILY, "20260710")
    provider = FakeProvider([TushareRateLimitError("429")] * 8)
    cache = PITCache(tmp_path)
    limiter = NoWaitLimiter()
    ingestor = PITIngestor(provider, cache, lambda endpoint: limiter, RetryPolicy(max_attempts=8), sleeper=lambda _: None)
    with pytest.raises(TushareRateLimitError):
        ingestor._ingest_keys([key])
    assert cache.records()[key.storage_key].status.value == "pending"
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `rtk pytest -q tests/test_picker_pit_ingestion.py`

Expected: FAIL because `tradingagents.picker.ingestion` does not exist.

- [ ] **Step 3: Implement deterministic two-stage partition planning**

Validate start/end as `YYYYMMDD` and require `start_date <= end_date`.
Compute `warmup_start` as `start_date - 120 calendar days` using date-only
arithmetic. `plan_bootstrap_partitions()` returns:

1. one `trade_cal/<year>` partition for every year touched by `warmup_start`
   through `end_date`;
2. one `stock_basic/current` partition;
3. one `namechange/all` partition.

After those partitions are complete, `open_trade_dates()` reads normalized
calendar partitions from `PITCache`, filters `is_open == 1` and the inclusive
effective warm-up range, and performs no network call.
`plan_daily_partitions()` then
returns `daily`, `daily_basic`, and `suspend_d` keys for each open date. Sort
each stage by dataset enum value and partition string so repeated runs produce
the same manifest order.

Use the exact signatures `plan_bootstrap_partitions(start_date: str, end_date:
str) -> list[PartitionKey]`, `open_trade_dates(cache: PITCache, start_date: str,
end_date: str) -> list[str]`, and `plan_daily_partitions(open_dates: list[str])
-> list[PartitionKey]`.

- [ ] **Step 4: Implement retry, raw serialization, and resume**

Create a private `_ingest_keys(keys, refresh=False)` helper and apply the following actions to
every incomplete key:

1. skip a checksum-verified complete key unless `refresh=True`, otherwise call
   `cache.mark_pending(key)`;
2. resolve `limiter = limiter_for(key.dataset.value)` and call
   `limiter.acquire()` immediately before each provider call;
3. fetch the frame;
4. serialize the raw frame with `frame.to_json(orient="table", date_format="iso").encode("utf-8")`;
5. normalize it;
6. call `cache.store_complete(key, raw_bytes, normalized, schema_version="1")`; and
7. increment the immutable summary counters.

Catch only `TushareRateLimitError` for retry. Call `limiter.penalize()`, sleep for `RetryPolicy.delay()`, and retry until `max_attempts`. On final throttle, leave the manifest status pending and re-raise. For non-throttle exceptions call `cache.mark_failed(key, f"{type(exc).__name__}: {exc}")` and re-raise. Never include the token in the message.

The `PITIngestor` constructor accepts `(provider, cache, limiter_for,
retry_policy, sleeper=time.sleep)`. Its `_ingest_keys(keys, refresh=False)` method is the
network/storage test seam, and its private `_call_with_retry(endpoint,
callable)` centralizes pacing, adaptive penalty, and eight-attempt retry.
`probe()` invokes `provider.probe` through that helper under the `trade_cal`
limiter.
`ingest(start_date, end_date, refresh=False)` first ingests
bootstrap partitions, derives open dates from the verified cache, then ingests
daily partitions and returns one combined `IngestionSummary`. Implement the method
bodies from the seven ordered actions above. At the outer `ingest()` boundary,
catch `KeyboardInterrupt` and `SystemExit` only long enough to finalize the run
manifest as `interrupted`, then immediately re-raise; the current partition
stays pending and completed partitions remain reusable.

At ingestion start, create a collision-resistant run ID from UTC timestamp and
eight random hexadecimal characters, then atomically write a `running`
`IngestionRunManifest` containing requested/effective ranges. After each key,
append the exact reused or new `PartitionRecord` and update the running
manifest. On success finalize it as `complete`; on an ordinary exception
finalize it as `failed` with the exception type/message; on user/process
interruption finalize it as `interrupted`. Return that run ID in
`IngestionSummary.run_id`. A rerun creates a new run manifest even when every
partition is skipped.

- [ ] **Step 5: Run tests and commit**

Run: `rtk pytest -q tests/test_picker_pit_ingestion.py`

Expected: `5 passed`.

```powershell
rtk git add tradingagents/picker/ingestion.py tests/test_picker_pit_ingestion.py
rtk git commit -m "feat: resume PIT ingestion by partition"
```

---

### Task 8: PIT universe and eligibility snapshot

**Files:**
- Create: `tradingagents/picker/snapshot.py`
- Test: `tests/test_picker_pit_snapshot.py`

**Interfaces:**
- Consumes: complete normalized cache partitions for the target date, covered years/months, and trailing 20 open sessions.
- Produces: `build_snapshot(cache: PITCache, as_of: str, minimum_coverage: float = 0.95) -> PITSnapshot`.
- Output universe columns include `ts_code`, `name`, `list_date`, `delist_date`, `close`, `amount_cny`, `avg_amount_20d_cny`, `free_float_market_cap_cny`, `is_st`, `is_suspended`, `listing_age_sessions`, `eligible`, and `ineligibility_reasons`.

- [ ] **Step 1: Write failing PIT snapshot tests**

```python
# tests/test_picker_pit_snapshot.py
import pandas as pd
import pytest

from tradingagents.picker.errors import PITCoverageError
from tradingagents.picker.snapshot import build_snapshot_from_frames


def base_frames():
    return {
        "trade_cal": pd.DataFrame({"cal_date": ["20260708", "20260709", "20260710"], "is_open": [1, 1, 1]}),
        "stock_basic": pd.DataFrame({
            "ts_code": ["000001.SZ", "600001.SH", "000002.SZ"],
            "name": ["平安银行", "未来退市样本", "停牌样本"],
            "list_date": ["19910403", "20000101", "20000101"],
            "delist_date": ["", "20270101", ""],
        }),
        "namechange": pd.DataFrame({
            "ts_code": ["600001.SH"], "name": ["ST未来"],
            "start_date": ["20260701"], "end_date": ["20260731"],
        }),
        "daily": pd.DataFrame({
            "ts_code": ["000001.SZ", "600001.SH"], "trade_date": ["20260710", "20260710"],
            "close": [10.0, 8.0], "amount_cny": [400_000_000.0, 500_000_000.0],
        }),
        "daily_basic": pd.DataFrame({
            "ts_code": ["000001.SZ", "600001.SH"], "trade_date": ["20260710", "20260710"],
            "free_float_market_cap_cny": [2_000_000_000.0, 2_000_000_000.0],
        }),
        "suspend_d": pd.DataFrame({"ts_code": ["000002.SZ"], "suspend_date": ["20260710"]}),
        "trailing_amounts": pd.DataFrame({
            "ts_code": ["000001.SZ", "600001.SH"],
            "avg_amount_20d_cny": [400_000_000.0, 500_000_000.0],
        }),
    }


def test_snapshot_keeps_future_delisted_stock_but_marks_effective_st():
    snapshot = build_snapshot_from_frames(base_frames(), "20260710", minimum_coverage=0.95)
    row = snapshot.universe.set_index("ts_code").loc["600001.SH"]
    assert row["is_st"]
    assert not row["eligible"]
    assert "st" in row["ineligibility_reasons"]


def test_explicit_suspension_counts_as_covered_but_ineligible():
    snapshot = build_snapshot_from_frames(base_frames(), "20260710", minimum_coverage=0.95)
    row = snapshot.universe.set_index("ts_code").loc["000002.SZ"]
    assert row["is_suspended"]
    assert "suspended" in row["ineligibility_reasons"]
    assert snapshot.coverage == 1.0


def test_unexplained_missing_rows_fail_coverage():
    frames = base_frames()
    frames["suspend_d"] = frames["suspend_d"].iloc[0:0]
    with pytest.raises(PITCoverageError, match="coverage"):
        build_snapshot_from_frames(frames, "20260710", minimum_coverage=0.95)


def test_future_namechange_does_not_mark_st():
    frames = base_frames()
    frames["namechange"]["start_date"] = "20260711"
    snapshot = build_snapshot_from_frames(frames, "20260710", minimum_coverage=0.95)
    assert not snapshot.universe.set_index("ts_code").loc["600001.SH", "is_st"]
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `rtk pytest -q tests/test_picker_pit_snapshot.py`

Expected: FAIL because `tradingagents.picker.snapshot` does not exist.

- [ ] **Step 3: Implement pure frame-based snapshot construction**

`build_snapshot_from_frames()` must:

1. select stocks with `list_date <= as_of` and blank `delist_date` or `delist_date > as_of`;
2. mark ST only when the name-change interval contains `as_of` inclusively and the effective name contains `ST` case-insensitively;
3. join same-day daily and daily-basic rows;
4. treat an explicit same-day suspension record as covered but ineligible;
5. treat an active stock missing both daily data and a suspension record as uncovered;
6. compute `listing_age_sessions` from open calendar sessions on or after list date;
7. apply exact base reasons: `listing_age`, `st`, `price`, `free_float_market_cap`, `liquidity`, `suspended`, and `missing_critical_data`; and
8. set `eligible` only when the semicolon-separated `ineligibility_reasons` string is empty.

Treat blank `end_date` in `namechange` as open-ended. The output `name` is the latest name whose effective interval contains `as_of`; use `stock_basic.name` only when no effective historical name exists. This prevents a future/current company name from leaking into the historical snapshot.

Use the exact thresholds: 60 sessions, close greater than RMB 3, derived free-float market cap at least RMB 500 million, and trailing average amount greater than RMB 300 million.

Expose `build_snapshot_from_frames(frames: dict[str, pd.DataFrame], as_of:
str, minimum_coverage: float = 0.95) -> PITSnapshot` and
`build_snapshot(cache: PITCache, as_of: str, minimum_coverage: float = 0.95)
-> PITSnapshot`. Both functions follow the eight ordered join/eligibility rules
above and do not read the network or host clock.

- [ ] **Step 4: Implement cache-backed snapshot loading**

`build_snapshot()` resolves the target and preceding `trade_cal/<year>`
partitions needed to obtain at least 60 prior open sessions,
`stock_basic/current`, `namechange/all`, same-day partitions, and the latest 20
open `daily` partitions ending at `as_of`. It computes `avg_amount_20d_cny` by
symbol, passes frames into the pure builder, and raises `PITSchemaError` naming
any missing partition. If the cache cannot supply 60 open sessions and 20
daily lookback sessions, the error instructs the operator to backfill an
earlier start date; the builder must not guess listing age or liquidity.

- [ ] **Step 5: Run tests and commit**

Run: `rtk pytest -q tests/test_picker_pit_snapshot.py`

Expected: `4 passed`.

```powershell
rtk git add tradingagents/picker/snapshot.py tests/test_picker_pit_snapshot.py
rtk git commit -m "feat: reconstruct PIT A-share snapshots"
```

---

### Task 9: CLI, documentation, live integration marker, and Phase 1 verification

**Files:**
- Create: `tradingagents/picker/cli.py`
- Create: `tests/test_picker_pit_cli.py`
- Create: `tests/test_tushare_pit_live.py`
- Create: `docs/pit-data-foundation.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all Phase 1 library interfaces.
- Produces: `tradingagents-pit backfill` and `tradingagents-pit snapshot` commands.

- [ ] **Step 1: Write failing CLI tests**

```python
# tests/test_picker_pit_cli.py
from typer.testing import CliRunner

from tradingagents.picker import cli


runner = CliRunner()


def test_backfill_reports_summary_without_printing_token(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token")
    monkeypatch.setattr(cli, "run_backfill", lambda **kwargs: (2, 3, 0, "run-1"))
    result = runner.invoke(
        cli.app,
        ["backfill", "--start-date", "20260709", "--end-date", "20260710", "--cache-dir", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert "completed=2 skipped=3 failed=0 run_id=run-1" in result.stdout
    assert "secret-token" not in result.stdout


def test_snapshot_prints_machine_readable_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli,
        "snapshot_summary",
        lambda **kwargs: {"as_of": "20260710", "active": 5000, "eligible": 1200, "coverage": 0.99},
    )
    result = runner.invoke(cli.app, ["snapshot", "--date", "20260710", "--cache-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert '"coverage": 0.99' in result.stdout
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `rtk pytest -q tests/test_picker_pit_cli.py`

Expected: FAIL because `tradingagents.picker.cli` does not exist.

- [ ] **Step 3: Implement the Typer commands**

Create `app = typer.Typer(name="tradingagents-pit", no_args_is_help=True)`. `backfill` accepts required `--start-date`/`--end-date`, optional `--cache-dir`, and optional `--calls-per-minute`. It builds `PITConfig`, `TushareProvider`, `PITCache`, one `TokenBucketLimiter` per endpoint using `config.rate_for(endpoint)`, and a limiter-factory closure for `PITIngestor`; then prints `completed=<n> skipped=<n> failed=<n>`.

`snapshot` accepts required `--date`, optional `--cache-dir`, calls `build_snapshot()`, and prints sorted JSON containing `as_of`, active row count, eligible row count, coverage, and warnings. Convert `PITError` into `typer.BadParameter` or a concise red error and exit code 1. Never print the token.

Use these implementations so the console entry point and tests remain stable:

```python
from typing import Annotated

app = typer.Typer(name="tradingagents-pit", no_args_is_help=True)


def run_backfill(
    *, start_date: str, end_date: str, cache_dir: Path | None,
    calls_per_minute: int | None, refresh: bool,
) -> tuple[int, int, int, str]:
    config = PITConfig.from_env(cache_dir, calls_per_minute)
    provider = TushareProvider.create(config)
    cache = PITCache(config.cache_dir)
    limiters: dict[str, TokenBucketLimiter] = {}

    def limiter_for(endpoint: str) -> TokenBucketLimiter:
        return limiters.setdefault(endpoint, TokenBucketLimiter(config.rate_for(endpoint)))

    ingestor = PITIngestor(provider, cache, limiter_for, RetryPolicy())
    ingestor.probe()
    summary = ingestor.ingest(start_date, end_date, refresh=refresh)
    return summary.completed, summary.skipped, summary.failed, summary.run_id or ""


def snapshot_summary(*, date: str, cache_dir: Path | None) -> dict[str, object]:
    config = PITConfig.from_env(cache_dir)
    result = build_snapshot(PITCache(config.cache_dir), date)
    return {
        "as_of": result.as_of,
        "active": len(result.universe),
        "eligible": int(result.universe["eligible"].sum()),
        "coverage": result.coverage,
        "warnings": list(result.warnings),
    }


@app.command()
def backfill(
    start_date: Annotated[str, typer.Option("--start-date")],
    end_date: Annotated[str, typer.Option("--end-date")],
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir")] = None,
    calls_per_minute: Annotated[int | None, typer.Option("--calls-per-minute")] = None,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
) -> None:
    try:
        completed, skipped, failed, run_id = run_backfill(
            start_date=start_date,
            end_date=end_date,
            cache_dir=cache_dir,
            calls_per_minute=calls_per_minute,
            refresh=refresh,
        )
    except PITError as exc:
        typer.echo(f"PIT backfill failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"completed={completed} skipped={skipped} failed={failed} run_id={run_id}"
    )


@app.command()
def snapshot(
    date: Annotated[str, typer.Option("--date")],
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir")] = None,
) -> None:
    try:
        summary = snapshot_summary(date=date, cache_dir=cache_dir)
    except PITError as exc:
        typer.echo(f"PIT snapshot failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(summary, sort_keys=True))
```

- [ ] **Step 4: Add the credential-gated integration test**

```python
# tests/test_tushare_pit_live.py
import os

import pytest

from tradingagents.picker.pit_config import PITConfig
from tradingagents.picker.tushare_provider import TushareProvider


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("TUSHARE_TOKEN"), reason="TUSHARE_TOKEN not set")
def test_tushare_trade_calendar_probe(tmp_path):
    provider = TushareProvider.create(PITConfig.from_env(cache_dir=tmp_path))
    provider.probe()
```

- [ ] **Step 5: Write operator documentation**

`docs/pit-data-foundation.md` must document:

- `pip install ".[pit]"`;
- `TUSHARE_TOKEN`, pacing precedence, and the default 40 calls/minute;
- `tradingagents-pit backfill --start-date 20210101 --end-date 20260710`;
- atomic partition layout and manifest checksums;
- content-addressed partition versions, per-run manifests, and `--refresh`;
- how interruption/throttling resume works;
- the automatic 120-calendar-day warm-up and the requested/effective ranges;
- the exact `circ_market_cap_cny` versus `free_float_market_cap_cny` formulas;
- the six Phase 1 datasets and explicit exclusions for later phases;
- validated coverage and fail-closed rules; and
- a warning not to redistribute cached provider data.

Add a short README section linking to this document; do not expand the main installation flow with mandatory Tushare steps.

- [ ] **Step 6: Run focused Phase 1 verification**

Run:

```powershell
rtk pytest -q tests/test_picker_pit_config.py tests/test_picker_pit_models.py tests/test_picker_rate_limit.py tests/test_picker_pit_cache.py tests/test_tushare_pit_provider.py tests/test_picker_pit_normalize.py tests/test_picker_pit_ingestion.py tests/test_picker_pit_snapshot.py tests/test_picker_pit_cli.py
```

Expected: all Phase 1 tests pass with no network access.

Run: `rtk pytest -q tests/test_ak_pick_a_stock.py tests/test_pyinstaller_entrypoints.py tests/test_windows_launchers.py`

Expected: all legacy picker/portable-entry tests pass.

Run: `rtk ruff check tradingagents/picker tests/test_picker_pit_*.py tests/test_tushare_pit_provider.py tests/test_tushare_pit_live.py`

Expected: no findings in the Phase 1 scope.

Run: `rtk proxy git diff --check`

Expected: no whitespace errors.

- [ ] **Step 7: Commit the complete Phase 1 interface**

```powershell
rtk git add tradingagents/picker tests docs/pit-data-foundation.md README.md pyproject.toml .env.example
rtk git commit -m "feat: expose PIT data foundation CLI"
```

## Final Review Gates

### Spec compliance

- Tushare remains optional and lazy-loaded.
- No per-stock historical request loop exists.
- Pacing, eight-attempt retry, adaptive penalty, atomic completion, checksum verification, and resume are covered by deterministic tests.
- `circ_mv` and derived true free-float market cap remain distinct across normalization and snapshots.
- Listed, delisted, ST, and suspended cases are reconstructed as of the requested date.
- Missing critical rows fail closed and 95% coverage is enforced.
- No ranking, execution, rights-issue, dividend, or walk-forward behavior enters Phase 1.

### Code quality

- Optional SDK imports occur only inside provider creation.
- Dataframe schemas and units are validated at the normalization boundary.
- Network, storage, normalization, and snapshot responsibilities stay in separate modules.
- Tests use fake clocks, providers, and temporary directories; only the explicitly marked integration test can reach Tushare.
- Error messages identify dataset/partition without exposing credentials.
- The worktree is clean after the final commit.

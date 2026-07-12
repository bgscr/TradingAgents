import hashlib
import json
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
    raw_payload = frame.to_json(orient="records").encode()

    record = cache.store_complete(key, raw_payload, frame, "1")

    assert cache.is_complete(key)
    assert record.raw_sha256 == hashlib.sha256(raw_payload).hexdigest()
    assert record.normalized_sha256 == hashlib.sha256(
        (tmp_path / record.normalized_path).read_bytes()
    ).hexdigest()
    assert (tmp_path / record.raw_path).name == f"{record.raw_sha256}.json"
    assert (tmp_path / record.normalized_path).name == f"{record.normalized_sha256}.parquet"
    assert record.fetched_at.endswith("Z")
    pd.testing.assert_frame_equal(cache.load_frame(key), frame)


@pytest.mark.parametrize("kind", ["raw", "normalized"])
def test_corrupt_file_is_not_resumable(tmp_path, kind):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    frame = pd.DataFrame({"ts_code": ["000001.SZ"], "close": [10.0]})
    cache.store_complete(key, b"[]", frame, "1")

    getattr(cache, f"{kind}_path")(key).write_bytes(b"corrupt")

    assert not cache.is_complete(key)
    with pytest.raises(PITSchemaError, match=kind):
        cache.load_frame(key)


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
    assert first.normalized_path != second.normalized_path
    assert (tmp_path / first.raw_path).exists()
    assert (tmp_path / first.normalized_path).exists()
    assert cache.records()[key.storage_key] == second
    pd.testing.assert_frame_equal(
        cache.load_frame(key, first.normalized_sha256), pd.DataFrame({"v": [1]})
    )
    pd.testing.assert_frame_equal(cache.load_frame(key), pd.DataFrame({"v": [2]}))


def test_records_returns_a_defensive_mapping(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache.mark_pending(key)

    returned = cache.records()
    returned.clear()

    assert key.storage_key in cache.records()


def test_completed_run_manifest_is_immutable(tmp_path):
    cache = PITCache(tmp_path)
    manifest = IngestionRunManifest(
        run_id="run-1",
        requested_start="20260710",
        requested_end="20260710",
        effective_start="20260312",
        effective_end="20260710",
        created_at="2026-07-12T09:00:00Z",
        partitions=(),
        status="complete",
    )
    cache.write_run_manifest(manifest)
    with pytest.raises(PITSchemaError, match="immutable"):
        cache.write_run_manifest(replace(manifest, error="changed"))


@pytest.mark.parametrize("terminal_status", ["complete", "failed", "interrupted"])
def test_running_run_manifest_can_be_finalized_only_once(tmp_path, terminal_status):
    cache = PITCache(tmp_path)
    running = IngestionRunManifest(
        run_id="run-1",
        requested_start="20260710",
        requested_end="20260710",
        effective_start="20260312",
        effective_end="20260710",
        created_at="2026-07-12T09:00:00Z",
        partitions=(),
        status="running",
    )
    terminal = replace(running, status=terminal_status)

    cache.write_run_manifest(running)
    cache.write_run_manifest(terminal)

    with pytest.raises(PITSchemaError, match="immutable"):
        cache.write_run_manifest(terminal)


def test_atomic_write_failure_leaves_no_temporary_files_or_record(tmp_path, monkeypatch):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("tradingagents.picker.cache.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated"):
        cache.mark_pending(key)

    assert cache.records() == {}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_unknown_or_unsafe_pinned_version_is_rejected(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache.store_complete(key, b"[]", pd.DataFrame({"v": [1]}), "1")

    with pytest.raises(PITSchemaError, match="pinned normalized version"):
        cache.load_frame(key, "a" * 64)
    with pytest.raises(PITSchemaError, match="SHA-256"):
        cache.load_frame(key, "../manifest")


def test_malformed_manifest_fields_fail_as_schema_errors(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache.mark_pending(key)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_record = manifest["partitions"][key.storage_key]
    raw_record.update(
        status="complete",
        raw_path=17,
        raw_sha256=17,
        normalized_path="normalized/daily/20260710/missing.parquet",
        normalized_sha256="a" * 64,
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    malformed = PITCache(tmp_path)

    assert not malformed.is_complete(key)
    with pytest.raises(PITSchemaError, match="failed verification"):
        malformed.load_frame(key)

import hashlib
import json
import os
import sqlite3
from dataclasses import replace

import pandas as pd
import pytest

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import PITSchemaError
from tradingagents.picker.pit_models import (
    Dataset,
    IngestionRunManifest,
    PartitionKey,
    PartitionRecord,
    PartitionStatus,
)


def test_partition_state_uses_sqlite_and_reopens_without_mutable_json(tmp_path):
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache = PITCache(tmp_path)

    cache.mark_pending(key)

    assert (tmp_path / "state.sqlite3").is_file()
    assert not (tmp_path / "manifest.json").exists()
    reopened = PITCache(tmp_path)
    assert reopened.records()[key.storage_key].status is PartitionStatus.PENDING


def test_legacy_json_manifest_migrates_once_then_sqlite_is_authoritative(tmp_path):
    key = PartitionKey(Dataset.DAILY, "20260710")
    legacy_manifest = {
        "partitions": {
            key.storage_key: {
                "dataset": key.dataset.value,
                "partition": key.partition,
                "status": "pending",
                "raw_sha256": None,
                "normalized_sha256": None,
                "row_count": 0,
                "fetched_at": None,
                "schema_version": "1",
                "error": None,
                "raw_path": None,
                "normalized_path": None,
            }
        }
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps(legacy_manifest), encoding="utf-8"
    )

    migrated = PITCache(tmp_path)
    assert migrated.records()[key.storage_key].status is PartitionStatus.PENDING
    migrated.mark_failed(key, "new database state")

    reopened = PITCache(tmp_path)
    record = reopened.records()[key.storage_key]
    assert record.status is PartitionStatus.FAILED
    assert record.error == "new database state"


def test_interrupted_run_reopens_from_sqlite_and_emits_one_final_manifest(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    running = IngestionRunManifest(
        run_id="run-sqlite-resume",
        requested_start="20260710",
        requested_end="20260710",
        effective_start="20260312",
        effective_end="20260710",
        created_at="2026-07-17T00:00:00Z",
        partitions=(),
        status="running",
    )
    record = PartitionRecord(key=key, status=PartitionStatus.PENDING)

    cache.start_run(running)
    cache.record_run_partition(running.run_id, record)
    assert not (tmp_path / "runs" / f"{running.run_id}.json").exists()
    cache.close()

    reopened = PITCache(tmp_path)
    finalized = reopened.finalize_run(
        running.run_id, "interrupted", "KeyboardInterrupt: stopped"
    )

    assert finalized.status == "interrupted"
    assert finalized.partitions == (record,)
    final_path = tmp_path / "runs" / f"{running.run_id}.json"
    assert json.loads(final_path.read_text(encoding="utf-8"))["status"] == "interrupted"
    with pytest.raises(PITSchemaError, match="immutable"):
        reopened.finalize_run(running.run_id, "complete")


def test_final_manifest_write_can_retry_from_terminal_sqlite_state(
    monkeypatch, tmp_path
):
    cache = PITCache(tmp_path)
    running = IngestionRunManifest(
        run_id="run-final-retry",
        requested_start="20260710",
        requested_end="20260710",
        effective_start="20260312",
        effective_end="20260710",
        created_at="2026-07-17T00:00:00Z",
        partitions=(),
        status="running",
    )
    cache.start_run(running)
    atomic_write = cache._atomic_write_json

    def fail_once(path, value):
        raise OSError("final manifest write failed")

    monkeypatch.setattr(cache, "_atomic_write_json", fail_once)
    with pytest.raises(OSError, match="final manifest write failed"):
        cache.finalize_run(running.run_id, "complete")

    monkeypatch.setattr(cache, "_atomic_write_json", atomic_write)
    finalized = cache.finalize_run(running.run_id, "complete")

    assert finalized.status == "complete"
    assert (tmp_path / "runs" / "run-final-retry.json").is_file()


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


def test_ordinary_resume_uses_metadata_and_explicit_audit_hashes_payloads(
    monkeypatch, tmp_path
):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY_BASIC, "20260710")
    cache.store_complete(
        key,
        b'[{"ts_code":"000001.SZ"}]',
        pd.DataFrame({"ts_code": ["000001.SZ"]}),
        "1",
    )
    original_sha256_file = cache._sha256_file
    hashed_paths = []

    def counted_sha256(path):
        hashed_paths.append(path)
        return original_sha256_file(path)

    monkeypatch.setattr(cache, "_sha256_file", counted_sha256)

    assert cache.is_complete(key)
    assert hashed_paths == []
    assert cache.verify_integrity() == {}
    assert len(hashed_paths) == 2


@pytest.mark.parametrize("kind", ["raw", "normalized"])
def test_corrupt_file_requires_explicit_integrity_audit(tmp_path, kind):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    frame = pd.DataFrame({"ts_code": ["000001.SZ"], "close": [10.0]})
    cache.store_complete(key, b"[]", frame, "1")

    getattr(cache, f"{kind}_path")(key).write_bytes(b"corrupt")

    assert cache.is_complete(key)
    failures = cache.verify_integrity()
    assert "checksum mismatch" in "; ".join(failures[key.storage_key])
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
    assert not (tmp_path / "runs" / "run-1.json").exists()
    cache.write_run_manifest(terminal)

    with pytest.raises(PITSchemaError, match="immutable"):
        cache.write_run_manifest(terminal)


def test_sqlite_state_failure_leaves_no_partition_record(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache._connection.executescript(
        """
        CREATE TRIGGER fail_partition_insert
        BEFORE INSERT ON partition_records
        BEGIN
            SELECT RAISE(ABORT, 'simulated state failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="simulated state failure"):
        cache.mark_pending(key)

    assert cache.records() == {}
    cache.close()
    assert PITCache(tmp_path).records() == {}


def test_unknown_or_unsafe_pinned_version_is_rejected(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache.store_complete(key, b"[]", pd.DataFrame({"v": [1]}), "1")

    with pytest.raises(PITSchemaError, match="pinned normalized version"):
        cache.load_frame(key, "a" * 64)
    with pytest.raises(PITSchemaError, match="SHA-256"):
        cache.load_frame(key, "../manifest")


def test_malformed_sqlite_fields_fail_as_schema_errors(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    cache.mark_pending(key)
    cache._connection.execute(
        """
        UPDATE partition_records
        SET status = 'complete', raw_path = 17, raw_sha256 = 17,
            normalized_path = ?, normalized_sha256 = ?
        WHERE storage_key = ?
        """,
        (
            "normalized/daily/20260710/missing.parquet",
            "a" * 64,
            key.storage_key,
        ),
    )
    cache._connection.commit()
    cache.close()
    malformed = PITCache(tmp_path)

    assert not malformed.is_complete(key)
    with pytest.raises(PITSchemaError, match="failed verification"):
        malformed.load_frame(key)


def test_store_complete_rejects_corrupt_existing_hash_target(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    raw_payload = b'[{"v": 1}]'
    raw_sha256 = hashlib.sha256(raw_payload).hexdigest()
    target = tmp_path / "raw" / "daily" / "20260710" / f"{raw_sha256}.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt pre-existing content")

    with pytest.raises(PITSchemaError, match="checksum"):
        cache.store_complete(key, raw_payload, pd.DataFrame({"v": [1]}), "1")

    assert target.read_bytes() == b"corrupt pre-existing content"
    assert cache.records() == {}
    assert list((tmp_path / "normalized").rglob("*.parquet")) == []
    assert list(tmp_path.rglob("*.tmp")) == []


@pytest.mark.parametrize("raw_payload", [b"not-json", b'"\xff"'])
def test_store_complete_rejects_invalid_raw_json_without_writing(tmp_path, raw_payload):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")

    with pytest.raises(PITSchemaError, match="valid UTF-8 JSON"):
        cache.store_complete(key, raw_payload, pd.DataFrame({"v": [1]}), "1")

    assert cache.records() == {}
    assert list(tmp_path.rglob("*.json")) == []
    assert list(tmp_path.rglob("*.parquet")) == []
    assert list(tmp_path.rglob("*.tmp")) == []


def test_top_level_manifest_array_raises_schema_error(tmp_path):
    (tmp_path / "manifest.json").write_text("[]", encoding="utf-8")

    with pytest.raises(PITSchemaError, match="manifest.json is invalid"):
        PITCache(tmp_path)


def _cache_with_existing_version(tmp_path):
    cache = PITCache(tmp_path)
    key = PartitionKey(Dataset.DAILY, "20260710")
    record = cache.store_complete(
        key, b'[{"v": 1}]', pd.DataFrame({"v": [1]}), "1"
    )
    preserved = {
        tmp_path / record.raw_path: (tmp_path / record.raw_path).read_bytes(),
        tmp_path / record.normalized_path: (
            tmp_path / record.normalized_path
        ).read_bytes(),
    }
    return cache, key, record, preserved


def _assert_failed_store_preserved_existing_version(
    tmp_path, cache, key, record, preserved
):
    assert cache.records()[key.storage_key] == record
    assert PITCache(tmp_path).records()[key.storage_key] == record
    for path, expected_bytes in preserved.items():
        assert path.read_bytes() == expected_bytes
    assert set((tmp_path / "raw").rglob("*.json")) == {tmp_path / record.raw_path}
    assert set((tmp_path / "normalized").rglob("*.parquet")) == {
        tmp_path / record.normalized_path
    }
    assert list(tmp_path.rglob("*.tmp")) == []


def test_store_failure_after_raw_install_rolls_back_only_new_content(
    tmp_path, monkeypatch
):
    cache, key, record, preserved = _cache_with_existing_version(tmp_path)
    real_replace = os.replace
    replacements = 0

    def fail_normalized_install(source, target):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("normalized install failed")
        real_replace(source, target)

    monkeypatch.setattr("tradingagents.picker.cache.os.replace", fail_normalized_install)

    with pytest.raises(OSError, match="normalized install failed"):
        cache.store_complete(key, b'[{"v": 2}]', pd.DataFrame({"v": [2]}), "1")

    _assert_failed_store_preserved_existing_version(
        tmp_path, cache, key, record, preserved
    )


def test_store_failure_after_normalized_install_rolls_back_only_new_content(
    tmp_path, monkeypatch
):
    cache, key, record, preserved = _cache_with_existing_version(tmp_path)

    def fail_record_update(new_record):
        raise OSError("record update failed")

    monkeypatch.setattr(cache, "_replace_record", fail_record_update)

    with pytest.raises(OSError, match="record update failed"):
        cache.store_complete(key, b'[{"v": 2}]', pd.DataFrame({"v": [2]}), "1")

    _assert_failed_store_preserved_existing_version(
        tmp_path, cache, key, record, preserved
    )


def test_store_failure_during_sqlite_update_rolls_back_only_new_content(tmp_path):
    cache, key, record, preserved = _cache_with_existing_version(tmp_path)
    cache._connection.executescript(
        """
        CREATE TRIGGER fail_partition_update
        BEFORE UPDATE ON partition_records
        BEGIN
            SELECT RAISE(ABORT, 'state update failed');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="state update failed"):
        cache.store_complete(key, b'[{"v": 2}]', pd.DataFrame({"v": [2]}), "1")

    _assert_failed_store_preserved_existing_version(
        tmp_path, cache, key, record, preserved
    )

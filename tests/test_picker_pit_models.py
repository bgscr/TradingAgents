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

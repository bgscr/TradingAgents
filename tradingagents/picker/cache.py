from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from tradingagents.picker.errors import PITSchemaError
from tradingagents.picker.pit_models import (
    Dataset,
    IngestionRunManifest,
    Manifest,
    PartitionKey,
    PartitionRecord,
    PartitionStatus,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_STATE_DATABASE = "state.sqlite3"


class PITCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.root / "manifest.json"
        self._database_path = self.root / _STATE_DATABASE
        self._connection = sqlite3.connect(self._database_path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._initialize_database()
        self._migrate_legacy_manifest()
        self._records = self._read_partition_records()

    def close(self) -> None:
        """Close the mutable-state database connection."""
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None

    def __del__(self) -> None:
        self.close()

    def raw_path(self, key: PartitionKey) -> Path:
        record = self._record_for(key)
        if record.raw_path is None:
            raise PITSchemaError(f"cached partition {key.storage_key} has no raw path")
        return self._resolve_relative_path(record.raw_path)

    def normalized_path(self, key: PartitionKey) -> Path:
        record = self._record_for(key)
        if record.normalized_path is None:
            raise PITSchemaError(f"cached partition {key.storage_key} has no normalized path")
        return self._resolve_relative_path(record.normalized_path)

    def records(self) -> dict[str, PartitionRecord]:
        return dict(self._records)

    def mark_pending(self, key: PartitionKey) -> None:
        self._replace_record(PartitionRecord(key=key, status=PartitionStatus.PENDING))

    def mark_failed(self, key: PartitionKey, error: str) -> None:
        self._replace_record(
            PartitionRecord(key=key, status=PartitionStatus.FAILED, error=error)
        )

    def store_complete(
        self,
        key: PartitionKey,
        raw_payload: bytes,
        frame: pd.DataFrame,
        schema_version: str,
    ) -> PartitionRecord:
        self._validate_raw_json(raw_payload)
        raw_directory = self.root / "raw" / key.dataset.value / key.partition
        normalized_directory = (
            self.root / "normalized" / key.dataset.value / key.partition
        )
        raw_temp: Path | None = None
        normalized_temp: Path | None = None
        created_paths: list[Path] = []

        try:
            raw_temp = self._write_temporary_bytes(raw_directory, raw_payload, "raw")
            normalized_temp = self._write_temporary_parquet(normalized_directory, frame)

            raw_sha256 = self._sha256_file(raw_temp)
            normalized_sha256 = self._sha256_file(normalized_temp)
            raw_target = raw_directory / f"{raw_sha256}.json"
            normalized_target = normalized_directory / f"{normalized_sha256}.parquet"

            if self._install_version(raw_temp, raw_target):
                created_paths.append(raw_target)
            raw_temp = None
            if self._install_version(normalized_temp, normalized_target):
                created_paths.append(normalized_target)
            normalized_temp = None

            record = PartitionRecord(
                key=key,
                status=PartitionStatus.COMPLETE,
                raw_sha256=raw_sha256,
                normalized_sha256=normalized_sha256,
                row_count=len(frame),
                fetched_at=self._utc_now(),
                schema_version=str(schema_version),
                error=None,
                raw_path=raw_target.relative_to(self.root).as_posix(),
                normalized_path=normalized_target.relative_to(self.root).as_posix(),
            )
            self._replace_record(record)
            return record
        except BaseException:
            for path in reversed(created_paths):
                path.unlink(missing_ok=True)
            raise
        finally:
            if raw_temp is not None:
                raw_temp.unlink(missing_ok=True)
            if normalized_temp is not None:
                normalized_temp.unlink(missing_ok=True)

    def is_complete(self, key: PartitionKey) -> bool:
        record = self._records.get(key.storage_key)
        return record is not None and not self._metadata_failures(record)

    def verify_integrity(self) -> dict[str, tuple[str, ...]]:
        """Hash every cached payload and return failures keyed by partition."""
        failures = {}
        for storage_key, record in self._records.items():
            record_failures = self._verification_failures(record)
            if record_failures:
                failures[storage_key] = tuple(record_failures)
        return failures

    def load_frame(
        self, key: PartitionKey, normalized_sha256: str | None = None
    ) -> pd.DataFrame:
        if normalized_sha256 is not None:
            return self._load_pinned_frame(key, normalized_sha256)

        record = self._record_for(key)
        failures = self._verification_failures(record)
        if failures:
            details = "; ".join(failures)
            raise PITSchemaError(
                f"cached partition {key.storage_key} failed verification: {details}"
            )
        return self._read_parquet(self._resolve_relative_path(record.normalized_path), key)

    def write_run_manifest(self, manifest: IngestionRunManifest) -> None:
        """Compatibility wrapper over incremental SQLite run-state methods."""
        self._validate_run_id(manifest.run_id)
        stored_row = self._connection.execute(
            "SELECT status FROM ingestion_runs WHERE run_id = ?",
            (manifest.run_id,),
        ).fetchone()
        if stored_row is None:
            running = IngestionRunManifest(
                run_id=manifest.run_id,
                requested_start=manifest.requested_start,
                requested_end=manifest.requested_end,
                effective_start=manifest.effective_start,
                effective_end=manifest.effective_end,
                created_at=manifest.created_at,
                partitions=manifest.partitions,
                status="running",
            )
            self.start_run(running)
        else:
            if stored_row[0] != "running":
                raise PITSchemaError(
                    f"ingestion run manifest {manifest.run_id} is immutable "
                    f"after status {stored_row[0]}"
                )
            stored = self._read_run_manifest_from_database(manifest.run_id)
            static_fields_match = (
                stored.requested_start == manifest.requested_start
                and stored.requested_end == manifest.requested_end
                and stored.effective_start == manifest.effective_start
                and stored.effective_end == manifest.effective_end
                and stored.created_at == manifest.created_at
            )
            if not static_fields_match:
                raise PITSchemaError(
                    f"ingestion run {manifest.run_id} metadata cannot change"
                )
            stored_count = len(stored.partitions)
            if manifest.partitions[:stored_count] != stored.partitions:
                raise PITSchemaError(
                    f"ingestion run {manifest.run_id} partition history cannot change"
                )
            for record in manifest.partitions[stored_count:]:
                self.record_run_partition(manifest.run_id, record)

        if manifest.status != "running":
            self.finalize_run(manifest.run_id, manifest.status, manifest.error)

    def start_run(self, manifest: IngestionRunManifest) -> None:
        """Start a mutable ingestion run in SQLite without emitting JSON."""
        self._validate_run_id(manifest.run_id)
        if manifest.status != "running":
            raise PITSchemaError("a new ingestion run must have status running")
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO ingestion_runs (
                        run_id, requested_start, requested_end, effective_start,
                        effective_end, created_at, status, error, next_ordinal
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        manifest.run_id,
                        manifest.requested_start,
                        manifest.requested_end,
                        manifest.effective_start,
                        manifest.effective_end,
                        manifest.created_at,
                        manifest.error,
                        len(manifest.partitions),
                    ),
                )
                for ordinal, record in enumerate(manifest.partitions):
                    self._insert_run_partition(
                        manifest.run_id, ordinal, record
                    )
        except sqlite3.IntegrityError as exc:
            raise PITSchemaError(
                f"ingestion run {manifest.run_id} already exists or is invalid"
            ) from exc

    def record_run_partition(self, run_id: str, record: PartitionRecord) -> None:
        """Append one partition provenance record to a running ingestion run."""
        self._validate_run_id(run_id)
        with self._connection:
            row = self._connection.execute(
                """
                SELECT status, next_ordinal
                FROM ingestion_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise PITSchemaError(f"ingestion run {run_id} is missing")
            if row[0] != "running":
                raise PITSchemaError(
                    f"ingestion run {run_id} is immutable after status {row[0]}"
                )
            ordinal = int(row[1])
            try:
                self._insert_run_partition(run_id, ordinal, record)
            except sqlite3.IntegrityError as exc:
                raise PITSchemaError(
                    f"partition {record.key.storage_key} is already recorded "
                    f"for ingestion run {run_id}"
                ) from exc
            self._connection.execute(
                """
                UPDATE ingestion_runs
                SET next_ordinal = ?
                WHERE run_id = ?
                """,
                (ordinal + 1, run_id),
            )

    def finalize_run(
        self, run_id: str, status: str, error: str | None = None
    ) -> IngestionRunManifest:
        """Finalize mutable run state and emit its one immutable JSON manifest."""
        self._validate_run_id(run_id)
        if status not in {"complete", "failed", "interrupted"}:
            raise PITSchemaError(f"invalid terminal ingestion status: {status!r}")
        path = self.root / "runs" / f"{run_id}.json"
        with self._connection:
            row = self._connection.execute(
                "SELECT status, error FROM ingestion_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise PITSchemaError(f"ingestion run {run_id} is missing")
            if row[0] != "running":
                if row[0] == status and row[1] == error and not path.exists():
                    finalized = self._read_run_manifest_from_database(run_id)
                    self._atomic_write_json(path, finalized.to_dict())
                    return finalized
                raise PITSchemaError(
                    f"ingestion run {run_id} is immutable after status {row[0]}"
                )
            if path.exists():
                raise PITSchemaError(
                    f"ingestion run manifest {run_id} already exists and is immutable"
                )
            self._connection.execute(
                """
                UPDATE ingestion_runs
                SET status = ?, error = ?
                WHERE run_id = ?
                """,
                (status, error, run_id),
            )

        finalized = self._read_run_manifest_from_database(run_id)
        self._atomic_write_json(path, finalized.to_dict())
        return finalized

    def _record_for(self, key: PartitionKey) -> PartitionRecord:
        try:
            return self._records[key.storage_key]
        except KeyError as exc:
            raise PITSchemaError(f"cached partition {key.storage_key} is missing") from exc

    def _replace_record(self, record: PartitionRecord) -> None:
        values = self._partition_record_values(record)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO partition_records (
                    storage_key, dataset, partition_name, status,
                    raw_sha256, normalized_sha256, row_count, fetched_at,
                    schema_version, error, raw_path, normalized_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(storage_key) DO UPDATE SET
                    dataset = excluded.dataset,
                    partition_name = excluded.partition_name,
                    status = excluded.status,
                    raw_sha256 = excluded.raw_sha256,
                    normalized_sha256 = excluded.normalized_sha256,
                    row_count = excluded.row_count,
                    fetched_at = excluded.fetched_at,
                    schema_version = excluded.schema_version,
                    error = excluded.error,
                    raw_path = excluded.raw_path,
                    normalized_path = excluded.normalized_path
                """,
                values,
            )
        self._records[record.key.storage_key] = record

    @staticmethod
    def _partition_record_values(record: PartitionRecord) -> tuple[object, ...]:
        return (
            record.key.storage_key,
            record.key.dataset.value,
            record.key.partition,
            record.status.value,
            record.raw_sha256,
            record.normalized_sha256,
            record.row_count,
            record.fetched_at,
            record.schema_version,
            record.error,
            record.raw_path,
            record.normalized_path,
        )

    def _read_partition_records(self) -> dict[str, PartitionRecord]:
        try:
            rows = self._connection.execute(
                """
                SELECT storage_key, dataset, partition_name, status,
                       raw_sha256, normalized_sha256, row_count, fetched_at,
                       schema_version, error, raw_path, normalized_path
                FROM partition_records
                ORDER BY storage_key
                """
            ).fetchall()
            records = {}
            for row in rows:
                storage_key = row[0]
                key = PartitionKey(Dataset(row[1]), row[2])
                if storage_key != key.storage_key:
                    raise ValueError(
                        f"database key {storage_key!r} does not match its partition"
                    )
                records[storage_key] = PartitionRecord(
                    key=key,
                    status=PartitionStatus(row[3]),
                    raw_sha256=row[4],
                    normalized_sha256=row[5],
                    row_count=int(row[6]),
                    fetched_at=row[7],
                    schema_version=str(row[8]),
                    error=row[9],
                    raw_path=row[10],
                    normalized_path=row[11],
                )
            return records
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise PITSchemaError(
                f"cache state database {_STATE_DATABASE} is invalid "
                f"({type(exc).__name__})"
            ) from exc

    def _initialize_database(self) -> None:
        try:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS partition_records (
                        storage_key TEXT PRIMARY KEY,
                        dataset TEXT NOT NULL,
                        partition_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        raw_sha256 TEXT,
                        normalized_sha256 TEXT,
                        row_count INTEGER NOT NULL DEFAULT 0,
                        fetched_at TEXT,
                        schema_version TEXT NOT NULL,
                        error TEXT,
                        raw_path TEXT,
                        normalized_path TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_partition_records_status
                        ON partition_records(status);
                    CREATE TABLE IF NOT EXISTS cache_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS ingestion_runs (
                        run_id TEXT PRIMARY KEY,
                        requested_start TEXT NOT NULL,
                        requested_end TEXT NOT NULL,
                        effective_start TEXT NOT NULL,
                        effective_end TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error TEXT,
                        next_ordinal INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_ingestion_runs_status
                        ON ingestion_runs(status);
                    CREATE TABLE IF NOT EXISTS run_partitions (
                        run_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        storage_key TEXT NOT NULL,
                        dataset TEXT NOT NULL,
                        partition_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        raw_sha256 TEXT,
                        normalized_sha256 TEXT,
                        row_count INTEGER NOT NULL DEFAULT 0,
                        fetched_at TEXT,
                        schema_version TEXT NOT NULL,
                        error TEXT,
                        raw_path TEXT,
                        normalized_path TEXT,
                        PRIMARY KEY(run_id, ordinal),
                        UNIQUE(run_id, storage_key),
                        FOREIGN KEY(run_id) REFERENCES ingestion_runs(run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_run_partitions_run
                        ON run_partitions(run_id, ordinal);
                    """
                )
        except sqlite3.Error as exc:
            raise PITSchemaError(
                f"cache state database {_STATE_DATABASE} cannot be initialized"
            ) from exc

    def _migrate_legacy_manifest(self) -> None:
        migrated = self._connection.execute(
            "SELECT value FROM cache_metadata WHERE key = 'legacy_manifest_migrated'"
        ).fetchone()
        if migrated is not None or not self._manifest_path.exists():
            return

        existing_count = self._connection.execute(
            "SELECT COUNT(*) FROM partition_records"
        ).fetchone()[0]
        records: dict[str, PartitionRecord] = {}
        if existing_count == 0:
            try:
                raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
                records = dict(Manifest.from_dict(raw).partitions)
                for storage_key, record in records.items():
                    if storage_key != record.key.storage_key:
                        raise ValueError(
                            f"legacy manifest key {storage_key!r} does not match "
                            "its partition"
                        )
            except (
                AttributeError,
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                raise PITSchemaError(
                    f"legacy cache manifest {self._manifest_path.name} is invalid "
                    f"({type(exc).__name__})"
                ) from exc

        with self._connection:
            if records:
                self._connection.executemany(
                    """
                    INSERT INTO partition_records (
                        storage_key, dataset, partition_name, status,
                        raw_sha256, normalized_sha256, row_count, fetched_at,
                        schema_version, error, raw_path, normalized_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._partition_record_values(record)
                        for record in records.values()
                    ),
                )
            self._connection.execute(
                """
                INSERT INTO cache_metadata(key, value)
                VALUES ('legacy_manifest_migrated', '1')
                """
            )

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not _SAFE_RUN_ID_RE.fullmatch(run_id):
            raise PITSchemaError(f"unsafe ingestion run id: {run_id!r}")

    def _insert_run_partition(
        self, run_id: str, ordinal: int, record: PartitionRecord
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO run_partitions (
                run_id, ordinal, storage_key, dataset, partition_name, status,
                raw_sha256, normalized_sha256, row_count, fetched_at,
                schema_version, error, raw_path, normalized_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ordinal,
                *self._partition_record_values(record),
            ),
        )

    def _read_run_manifest_from_database(
        self, run_id: str
    ) -> IngestionRunManifest:
        row = self._connection.execute(
            """
            SELECT requested_start, requested_end, effective_start,
                   effective_end, created_at, status, error
            FROM ingestion_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise PITSchemaError(f"ingestion run {run_id} is missing")
        partition_rows = self._connection.execute(
            """
            SELECT storage_key, dataset, partition_name, status,
                   raw_sha256, normalized_sha256, row_count, fetched_at,
                   schema_version, error, raw_path, normalized_path
            FROM run_partitions
            WHERE run_id = ?
            ORDER BY ordinal
            """,
            (run_id,),
        ).fetchall()
        partitions = []
        for partition_row in partition_rows:
            key = PartitionKey(Dataset(partition_row[1]), partition_row[2])
            if partition_row[0] != key.storage_key:
                raise PITSchemaError(
                    f"run partition key {partition_row[0]!r} does not match "
                    "its partition"
                )
            partitions.append(
                PartitionRecord(
                    key=key,
                    status=PartitionStatus(partition_row[3]),
                    raw_sha256=partition_row[4],
                    normalized_sha256=partition_row[5],
                    row_count=int(partition_row[6]),
                    fetched_at=partition_row[7],
                    schema_version=str(partition_row[8]),
                    error=partition_row[9],
                    raw_path=partition_row[10],
                    normalized_path=partition_row[11],
                )
            )
        return IngestionRunManifest(
            run_id=run_id,
            requested_start=str(row[0]),
            requested_end=str(row[1]),
            effective_start=str(row[2]),
            effective_end=str(row[3]),
            created_at=str(row[4]),
            partitions=tuple(partitions),
            status=str(row[5]),
            error=row[6],
        )

    def _verification_failures(self, record: PartitionRecord) -> list[str]:
        failures = self._metadata_failures(record)
        fields = (
            ("raw", record.raw_path, record.raw_sha256),
            ("normalized", record.normalized_path, record.normalized_sha256),
        )
        for label, relative_path, expected_sha256 in fields:
            if not (
                isinstance(relative_path, str)
                and isinstance(expected_sha256, str)
                and _SHA256_RE.fullmatch(expected_sha256)
            ):
                continue
            try:
                path = self._resolve_relative_path(relative_path)
            except PITSchemaError:
                continue
            if not path.is_file():
                continue
            try:
                actual_sha256 = self._sha256_file(path)
            except OSError:
                if f"{label} file cannot be read" not in failures:
                    failures.append(f"{label} file cannot be read")
            else:
                if actual_sha256 != expected_sha256:
                    failures.append(f"{label} checksum mismatch")
        return failures

    def _metadata_failures(self, record: PartitionRecord) -> list[str]:
        failures = []
        if record.status is not PartitionStatus.COMPLETE:
            failures.append(f"status is {record.status.value}, not complete")

        fields = (
            ("raw", record.raw_path, record.raw_sha256),
            ("normalized", record.normalized_path, record.normalized_sha256),
        )
        for label, relative_path, expected_sha256 in fields:
            path = None
            if relative_path is None:
                failures.append(f"{label} path is missing")
            else:
                try:
                    path = self._resolve_relative_path(relative_path)
                except PITSchemaError:
                    failures.append(f"{label} path is unsafe")

            checksum_is_valid = bool(
                isinstance(expected_sha256, str)
                and _SHA256_RE.fullmatch(expected_sha256)
            )
            if not checksum_is_valid:
                failures.append(f"{label} checksum is missing or invalid")

            if path is None:
                continue
            if not path.is_file():
                failures.append(f"{label} file is missing")
        return failures

    def _load_pinned_frame(self, key: PartitionKey, normalized_sha256: str) -> pd.DataFrame:
        if not isinstance(normalized_sha256, str) or not _SHA256_RE.fullmatch(
            normalized_sha256
        ):
            raise PITSchemaError(
                f"pinned normalized version for {key.storage_key} is not a SHA-256"
            )
        path = (
            self.root
            / "normalized"
            / key.dataset.value
            / key.partition
            / f"{normalized_sha256}.parquet"
        )
        if not path.is_file():
            raise PITSchemaError(
                f"pinned normalized version {normalized_sha256} for "
                f"{key.storage_key} is missing"
            )
        try:
            actual_sha256 = self._sha256_file(path)
        except OSError as exc:
            raise PITSchemaError(
                f"pinned normalized version for {key.storage_key} cannot be read"
            ) from exc
        if actual_sha256 != normalized_sha256:
            raise PITSchemaError(
                f"pinned normalized version for {key.storage_key} failed checksum verification"
            )
        return self._read_parquet(path, key)

    def _read_parquet(self, path: Path, key: PartitionKey) -> pd.DataFrame:
        try:
            return pd.read_parquet(path)
        except Exception as exc:
            raise PITSchemaError(
                f"normalized Parquet for {key.storage_key} cannot be read "
                f"({type(exc).__name__})"
            ) from exc

    def _resolve_relative_path(self, value: str) -> Path:
        if not isinstance(value, str):
            raise PITSchemaError("cache manifest contains a non-text content path")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise PITSchemaError("cache manifest contains an unsafe content path")
        return self.root / relative

    @staticmethod
    def _utc_now() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validate_raw_json(raw_payload: bytes) -> None:
        try:
            decoded = raw_payload.decode("utf-8")
            json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PITSchemaError(
                "raw partition payload is not valid UTF-8 JSON"
            ) from exc

    @staticmethod
    def _new_temp_path(directory: Path, label: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=directory, prefix=f".{label}-", suffix=".tmp"
        )
        os.close(descriptor)
        return Path(name)

    @classmethod
    def _write_temporary_bytes(
        cls, directory: Path, payload: bytes, label: str
    ) -> Path:
        path = cls._new_temp_path(directory, label)
        try:
            with path.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @classmethod
    def _write_temporary_parquet(cls, directory: Path, frame: pd.DataFrame) -> Path:
        path = cls._new_temp_path(directory, "normalized")
        try:
            frame.to_parquet(path, index=False)
            with path.open("rb+") as handle:
                os.fsync(handle.fileno())
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @classmethod
    def _install_version(cls, source: Path, target: Path) -> bool:
        if target.exists():
            if not target.is_file():
                raise PITSchemaError(
                    f"content-addressed cache target {target.name} is not a file"
                )
            try:
                existing_sha256 = cls._sha256_file(target)
            except OSError as exc:
                raise PITSchemaError(
                    f"content-addressed cache target {target.name} cannot be verified"
                ) from exc
            if existing_sha256 != target.stem:
                raise PITSchemaError(
                    f"content-addressed cache target {target.name} has a checksum mismatch"
                )
            source.unlink()
            return False
        os.replace(source, target)
        return True

    @classmethod
    def _atomic_write_json(cls, path: Path, value: dict[str, object]) -> None:
        payload = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        temp = cls._write_temporary_bytes(path.parent, payload, path.stem)
        try:
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

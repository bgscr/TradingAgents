from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from tradingagents.picker.errors import PITSchemaError
from tradingagents.picker.pit_models import (
    IngestionRunManifest,
    Manifest,
    PartitionKey,
    PartitionRecord,
    PartitionStatus,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class PITCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.root / "manifest.json"
        self._records = self._read_partition_records()

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
        return record is not None and not self._verification_failures(record)

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
        if not _SAFE_RUN_ID_RE.fullmatch(manifest.run_id):
            raise PITSchemaError(f"unsafe ingestion run id: {manifest.run_id!r}")

        path = self.root / "runs" / f"{manifest.run_id}.json"
        if path.exists():
            stored = self._read_run_manifest(path)
            if stored.status != "running":
                raise PITSchemaError(
                    f"ingestion run manifest {manifest.run_id} is immutable "
                    f"after status {stored.status}"
                )

        self._atomic_write_json(path, manifest.to_dict())

    def _record_for(self, key: PartitionKey) -> PartitionRecord:
        try:
            return self._records[key.storage_key]
        except KeyError as exc:
            raise PITSchemaError(f"cached partition {key.storage_key} is missing") from exc

    def _replace_record(self, record: PartitionRecord) -> None:
        updated = dict(self._records)
        updated[record.key.storage_key] = record
        self._write_partition_records(updated)
        self._records = updated

    def _read_partition_records(self) -> dict[str, PartitionRecord]:
        if not self._manifest_path.exists():
            return {}
        try:
            raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            manifest = Manifest.from_dict(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PITSchemaError(
                f"cache manifest {self._manifest_path.name} is invalid ({type(exc).__name__})"
            ) from exc

        for storage_key, record in manifest.partitions.items():
            if storage_key != record.key.storage_key:
                raise PITSchemaError(
                    f"cache manifest key {storage_key!r} does not match its partition"
                )
        return dict(manifest.partitions)

    def _write_partition_records(self, records: dict[str, PartitionRecord]) -> None:
        self._atomic_write_json(
            self._manifest_path, Manifest(partitions=records).to_dict()
        )

    def _verification_failures(self, record: PartitionRecord) -> list[str]:
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
                continue
            if checksum_is_valid:
                try:
                    actual_sha256 = self._sha256_file(path)
                except OSError:
                    failures.append(f"{label} file cannot be read")
                else:
                    if actual_sha256 != expected_sha256:
                        failures.append(f"{label} checksum mismatch")
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

    def _read_run_manifest(self, path: Path) -> IngestionRunManifest:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return IngestionRunManifest.from_dict(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PITSchemaError(
                f"ingestion run manifest {path.name} is invalid ({type(exc).__name__})"
            ) from exc

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

    @staticmethod
    def _install_version(source: Path, target: Path) -> bool:
        if target.exists():
            if not target.is_file():
                raise PITSchemaError(
                    f"content-addressed cache target {target.name} is not a file"
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

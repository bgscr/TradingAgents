from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


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
    def from_dict(cls, value: dict[str, object]) -> Manifest:
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
                Manifest({record.key.storage_key: record}).to_dict()["partitions"][
                    record.key.storage_key
                ]
                for record in self.partitions
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> IngestionRunManifest:
        records = []
        for raw in value["partitions"]:
            storage_key = f'{raw["dataset"]}/{raw["partition"]}'
            record = Manifest.from_dict(
                {"partitions": {storage_key: raw}}
            ).partitions[storage_key]
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

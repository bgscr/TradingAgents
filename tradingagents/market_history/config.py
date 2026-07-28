from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class MarketHistoryMode(str, Enum):
    """Controls whether stored history can influence current analysis."""

    SHADOW = "shadow"
    AUTHORITATIVE = "authoritative"
    DISABLED = "disabled"


class DataUsageMode(str, Enum):
    """Operator-declared provider usage boundary."""

    PERSONAL_RESEARCH = "personal_research"
    PRODUCTION = "production"


def canonical_market_history_database_path(path: str | Path) -> Path:
    """Resolve every configuration alias to the one canonical database path."""

    return Path(path).expanduser().resolve(strict=False)


def payload_mutation_sidecar_path(database_path: str | Path) -> Path:
    """Derive the one runtime mutex database owned by a canonical store."""

    canonical_path = canonical_market_history_database_path(database_path)
    return canonical_path.with_name(
        f"{canonical_path.name}.payload-mutation.sqlite3"
    )


def provider_request_authority_config(
    config: MarketHistoryConfig,
) -> MarketHistoryConfig:
    """Derive the one coordinator authority used in normal and degraded modes."""

    database_path = config.database_path.with_name(
        f"{config.database_path.name}.provider-requests.sqlite3"
    )
    return MarketHistoryConfig(
        mode=config.mode,
        database_path=database_path,
        payload_root=database_path.with_name(f"{database_path.name}.payloads"),
        backup_root=database_path.with_name(f"{database_path.name}.backups"),
        data_usage_mode=config.data_usage_mode,
        yahoo_max_physical_attempts=config.yahoo_max_physical_attempts,
    )


@dataclass(frozen=True)
class MarketHistoryConfig:
    mode: MarketHistoryMode
    database_path: Path
    payload_root: Path
    backup_root: Path
    data_usage_mode: DataUsageMode
    yahoo_max_physical_attempts: int = 4

    def __post_init__(self) -> None:
        if (
            not isinstance(self.yahoo_max_physical_attempts, int)
            or isinstance(self.yahoo_max_physical_attempts, bool)
            or self.yahoo_max_physical_attempts < 1
        ):
            raise ValueError("Yahoo physical-attempt budget must be a positive integer")
        object.__setattr__(
            self,
            "database_path",
            canonical_market_history_database_path(self.database_path),
        )
        object.__setattr__(
            self,
            "payload_root",
            Path(self.payload_root).expanduser().resolve(strict=False),
        )
        object.__setattr__(
            self,
            "backup_root",
            Path(self.backup_root).expanduser().resolve(strict=False),
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> MarketHistoryConfig:
        """Validate the runtime mapping at the market-history boundary."""
        yahoo_max_physical_attempts = values.get("yahoo_max_physical_attempts", 4)
        if (
            not isinstance(yahoo_max_physical_attempts, int)
            or isinstance(yahoo_max_physical_attempts, bool)
            or yahoo_max_physical_attempts < 1
        ):
            raise ValueError("Yahoo physical-attempt budget must be a positive integer")
        return cls(
            mode=MarketHistoryMode(str(values["market_history_mode"])),
            database_path=canonical_market_history_database_path(
                str(values["market_history_database_path"])
            ),
            payload_root=Path(str(values["market_history_payload_root"])).expanduser(),
            backup_root=Path(str(values["market_history_backup_root"])).expanduser(),
            data_usage_mode=DataUsageMode(str(values["data_usage_mode"])),
            yahoo_max_physical_attempts=yahoo_max_physical_attempts,
        )

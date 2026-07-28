from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

import pandas as pd


class ProvenanceClass(str, Enum):
    RETROSPECTIVE_BACKFILL = "retrospective_backfill"
    OBSERVED_POINT_IN_TIME = "observed_point_in_time"


class SnapshotPurpose(str, Enum):
    CURRENT_ANALYSIS = "current_analysis"
    STRICT_REPLAY = "strict_replay"


class TradingStatus(str, Enum):
    TRADED = "traded"
    SUSPENDED = "suspended"


def _validate_revision_provenance(
    provider_available_at: datetime | None,
    provenance_class: ProvenanceClass | None,
) -> None:
    if provider_available_at is not None and (
        not isinstance(provider_available_at, datetime)
        or provider_available_at.tzinfo is None
    ):
        raise ValueError("provider availability must be a timezone-aware datetime")
    if provenance_class is not None and not isinstance(
        provenance_class,
        ProvenanceClass,
    ):
        raise TypeError("revision provenance must be a ProvenanceClass")


@dataclass(frozen=True)
class TradingStatusProvenance:
    provider: str
    provider_dataset_id: str
    session_date: date
    status: TradingStatus
    observed_at: datetime
    revision_id: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.provider_dataset_id:
            raise ValueError("trading-status provenance requires provider identity")
        if not isinstance(self.session_date, date):
            raise TypeError("trading-status provenance session_date must be a date")
        if not isinstance(self.status, TradingStatus):
            raise TypeError("trading-status provenance status must be a TradingStatus")
        if not isinstance(self.observed_at, datetime):
            raise TypeError("trading-status provenance observed_at must be a datetime")
        if self.observed_at.tzinfo is None:
            raise ValueError("trading-status provenance observed_at must be timezone-aware")


class StrictReplayUnavailable(RuntimeError):
    """Pinned Observed Point-in-Time History is unavailable for strict replay."""


class SnapshotIdentityCollisionError(RuntimeError):
    """An existing snapshot identity is bound to incompatible exact membership."""


class SnapshotPinCorruptionError(StrictReplayUnavailable):
    """An exact snapshot pin or one of its referenced revisions is corrupt."""


@dataclass(frozen=True)
class ProviderDatasetSpec:
    upstream_service_id: str
    upstream_service_name: str
    provider_dataset_id: str
    provider_name: str
    dataset_name: str
    adjustment_methodology: str
    strict_history_qualified: bool


@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: str
    canonical_symbol: str
    reference_market: str
    instrument_kind: str
    currency: str
    identity_revision: str


@dataclass(frozen=True)
class RawMarketObservation:
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    provider_available_at: datetime | None = None
    provenance_class: ProvenanceClass | None = None

    def __post_init__(self) -> None:
        _validate_revision_provenance(
            self.provider_available_at,
            self.provenance_class,
        )
        values = (self.open, self.high, self.low, self.close, self.volume)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise TypeError("raw market values must be finite Decimal instances")
        if self.volume < 0:
            raise ValueError("raw market volume must not be negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("raw market observation violates OHLC ordering")


@dataclass(frozen=True)
class TradingStatusObservation:
    session_date: date
    status: TradingStatus
    official_carried_close: Decimal | None = None
    volume: Decimal | None = None
    provider_available_at: datetime | None = None
    provenance_class: ProvenanceClass | None = None

    def __post_init__(self) -> None:
        _validate_revision_provenance(
            self.provider_available_at,
            self.provenance_class,
        )
        if self.status is TradingStatus.SUSPENDED and (
            self.official_carried_close is None or self.volume != Decimal("0")
        ):
            raise ValueError(
                "a suspension requires an official carried close and zero volume"
            )


@dataclass(frozen=True)
class AdjustmentFactorObservation:
    effective_date: date
    factor: Decimal
    provider_available_at: datetime | None = None
    provenance_class: ProvenanceClass | None = None

    def __post_init__(self) -> None:
        _validate_revision_provenance(
            self.provider_available_at,
            self.provenance_class,
        )
        if not isinstance(self.factor, Decimal) or not self.factor.is_finite():
            raise TypeError("adjustment factor must be a finite Decimal")
        if self.factor <= 0:
            raise ValueError("adjustment factor must be positive")


@dataclass(frozen=True)
class ProviderHistoryBundlePublication:
    provider: ProviderDatasetSpec
    instrument: InstrumentSpec
    requested_as_of: date
    retrieval_cutoff: datetime
    observed_at: datetime
    provenance_class: ProvenanceClass
    raw_payload: bytes
    observations: tuple[RawMarketObservation, ...]
    trading_statuses: tuple[TradingStatusObservation, ...]
    adjustment_factors: tuple[AdjustmentFactorObservation, ...]


@dataclass(frozen=True)
class PublishedHistoryBundle:
    bundle_revision_id: str
    ingestion_run_id: str
    observation_revision_ids: tuple[str, ...]
    trading_status_revision_ids: tuple[str, ...]
    factor_revision_ids: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotV2MembershipAudit:
    snapshot_id: str
    identity_version: str
    membership_digest: str
    calendar_revision_id: str
    requested_as_of: date
    observation_status_membership: tuple[tuple[str, str, str], ...]
    factor_revision_ids: tuple[str, ...]


@dataclass(frozen=True)
class HistoryBundleProvenanceAudit:
    bundle_revision_id: str
    requested_as_of: date
    as_of_cutoff: datetime
    provenance_class: ProvenanceClass
    observation_revision_ids: tuple[str, ...]
    trading_status_revision_ids: tuple[str, ...]
    factor_revision_ids: tuple[str, ...]
    retained_observation_revision_ids: tuple[str, ...]
    refreshed_observation_revision_ids: tuple[str, ...]
    retained_trading_status_revision_ids: tuple[str, ...]
    refreshed_trading_status_revision_ids: tuple[str, ...]
    retained_factor_revision_ids: tuple[str, ...]
    refreshed_factor_revision_ids: tuple[str, ...]
    snapshot_v2_memberships: tuple[SnapshotV2MembershipAudit, ...] = ()


@dataclass(frozen=True)
class ReconstructedMarketSnapshot:
    symbol: str
    frame: pd.DataFrame
    provider: str
    adjustment_basis: str
    requested_date: str
    effective_trading_date: str
    frame_sha256: str
    snapshot_id: str
    bundle_revision_id: str
    provenance_class: ProvenanceClass
    observation_revision_ids: tuple[str, ...]
    trading_status_revision_ids: tuple[str, ...]
    factor_revision_ids: tuple[str, ...]
    calendar_revision_id: str | None = None
    snapshot_id_version: str = "v1"
    pin_membership_digest: str | None = None
    manifest_json: str | None = None
    current_tradeability: str = "unknown"
    current_status_provenance: TradingStatusProvenance | None = None
    latest_traded_close: Decimal | None = None
    latest_traded_close_diagnostic: str | None = None
    carried_suspension_close: Decimal | None = None

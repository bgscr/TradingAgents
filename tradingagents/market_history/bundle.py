from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, NamedTuple

import pandas as pd

from tradingagents.evidence import stable_market_snapshot_id
from tradingagents.market_history.frames import normalized_frame_sha256
from tradingagents.market_history.models import (
    HistoryBundleProvenanceAudit,
    ProvenanceClass,
    ProviderHistoryBundlePublication,
    PublishedHistoryBundle,
    ReconstructedMarketSnapshot,
    SnapshotIdentityCollisionError,
    SnapshotPinCorruptionError,
    SnapshotPurpose,
    SnapshotV2MembershipAudit,
    StrictReplayUnavailable,
    TradingStatus,
    TradingStatusProvenance,
)
from tradingagents.market_history.revisions import RevisionKind, revision_identity
from tradingagents.market_history.snapshot_identity import (
    LEGACY_SNAPSHOT_ID_PATTERN,
    SNAPSHOT_V2_ID_PATTERN,
    snapshot_v2_identity,
)

SNAPSHOT_DERIVATION_VERSION = "mainland-qfq-v1"
SNAPSHOT_NORMALIZATION_VERSION = "normalized-frame-csv-v1"

if TYPE_CHECKING:
    from tradingagents.market_history.store import MarketHistoryStore


class _BundleRow(NamedTuple):
    provider_dataset_id: str
    provider_name: str
    instrument_id: str
    canonical_symbol: str
    retrieval_cutoff: str
    provenance_class: str
    observed_at: str
    upstream_service_id: str
    identity_revision: str
    reference_market: str
    instrument_kind: str
    currency: str


class _SnapshotPinParentRow(NamedTuple):
    bundle_revision_id: str
    instrument_id: str
    provider_dataset_id: str
    calendar_revision_id: str | None
    requested_as_of: str
    retrieval_cutoff: str
    adjustment_basis: str
    derivation_version: str
    frame_digest: str
    provenance_class: str
    history_store_degraded: int
    identity_version: str
    manifest_digest: str | None
    manifest_json: str | None
    normalization_version: str | None
    effective_trading_date: str | None
    history_rows: int | None


class _ObservationRevisionRow(NamedTuple):
    revision_id: str
    session_date: str
    open_value: str
    high_value: str
    low_value: str
    close_value: str
    volume_value: str


class _TradingStatusRevisionRow(NamedTuple):
    revision_id: str
    session_date: str
    trading_status: str
    official_carried_close_value: str | None
    volume_value: str | None


class _FactorRevisionRow(NamedTuple):
    revision_id: str
    effective_date: str
    factor_value: str


class _RevisionProvenanceInput(NamedTuple):
    provenance_class: ProvenanceClass
    provider_available_at: str | None
    first_observed_at: str


class _PinnedObservationRow(NamedTuple):
    ordinal: int
    pinned_session_date: str
    observation_revision_id: str
    trading_status_revision_id: str
    observation_session_date: str | None
    open_value: str | None
    high_value: str | None
    low_value: str | None
    close_value: str | None
    volume_value: str | None
    observation_payload_digest: str | None
    observation_provider_dataset_id: str | None
    observation_instrument_id: str | None
    status_session_date: str | None
    trading_status: str | None
    official_carried_close_value: str | None
    status_volume_value: str | None
    status_payload_digest: str | None
    status_provider_dataset_id: str | None
    status_instrument_id: str | None


class _PinnedFactorRow(NamedTuple):
    revision_id: str
    effective_date: str | None
    factor_value: str | None
    payload_digest: str | None
    provider_dataset_id: str | None
    instrument_id: str | None


class _ExpectedPinnedObservation(NamedTuple):
    snapshot_id: str
    ordinal: int
    session_date: str
    observation_revision_id: str
    trading_status_revision_id: str


class _ExpectedPinnedFactor(NamedTuple):
    snapshot_id: str
    factor_revision_id: str


@dataclass(frozen=True)
class _SnapshotObservationInput:
    observation_revision_id: str
    trading_status_revision_id: str
    session_date: str
    open_value: Decimal
    high_value: Decimal
    low_value: Decimal
    close_value: Decimal
    volume_value: Decimal
    trading_status: TradingStatus
    official_carried_close: Decimal | None
    status_volume: Decimal | None


@dataclass(frozen=True)
class _SnapshotFactorInput:
    revision_id: str
    effective_date: str
    factor_value: Decimal


@dataclass(frozen=True)
class _DerivedSnapshotMaterialization:
    frame: pd.DataFrame
    frame_digest: str
    effective_trading_date: str
    observation_revision_ids: tuple[str, ...]
    trading_status_revision_ids: tuple[str, ...]
    factor_revision_ids: tuple[str, ...]
    latest_status: TradingStatus
    latest_traded_close: Decimal | None


def _derive_qfq_materialization(
    observations: tuple[_SnapshotObservationInput, ...],
    factors: tuple[_SnapshotFactorInput, ...],
    *,
    error: Callable[[str], Exception],
) -> _DerivedSnapshotMaterialization:
    factor_dates = [item.effective_date for item in factors]
    rows: list[dict[str, object]] = []
    used_factor_ids: list[str] = []
    latest_traded_close: Decimal | None = None
    latest_status = TradingStatus.TRADED
    for observation in observations:
        factor_index = bisect_right(factor_dates, observation.session_date) - 1
        if factor_index < 0:
            raise error(
                f"snapshot has no adjustment factor for {observation.session_date}"
            )
        factor = factors[factor_index]
        latest_status = observation.trading_status
        if observation.trading_status is TradingStatus.SUSPENDED:
            if (
                observation.official_carried_close is None
                or observation.status_volume != Decimal("0")
            ):
                raise error(
                    "snapshot suspension status lacks its carried close or zero volume"
                )
            open_value = high_value = low_value = close_value = (
                observation.official_carried_close
            )
            volume = Decimal("0")
        else:
            open_value = observation.open_value
            high_value = observation.high_value
            low_value = observation.low_value
            close_value = observation.close_value
            volume = observation.volume_value
            latest_traded_close = close_value * factor.factor_value
        rows.append(
            {
                "Date": pd.Timestamp(observation.session_date),
                "Open": float(open_value * factor.factor_value),
                "High": float(high_value * factor.factor_value),
                "Low": float(low_value * factor.factor_value),
                "Close": float(close_value * factor.factor_value),
                "Volume": float(volume),
            }
        )
        if factor.revision_id not in used_factor_ids:
            used_factor_ids.append(factor.revision_id)
    frame = pd.DataFrame(
        rows,
        columns=["Date", "Open", "High", "Low", "Close", "Volume"],
    )
    return _DerivedSnapshotMaterialization(
        frame=frame,
        frame_digest=normalized_frame_sha256(frame),
        effective_trading_date=frame["Date"].max().strftime("%Y-%m-%d"),
        observation_revision_ids=tuple(
            item.observation_revision_id for item in observations
        ),
        trading_status_revision_ids=tuple(
            item.trading_status_revision_id for item in observations
        ),
        factor_revision_ids=tuple(used_factor_ids),
        latest_status=latest_status,
        latest_traded_close=latest_traded_close,
    )


def _build_reconstructed_snapshot(
    *,
    bundle: _BundleRow,
    materialization: _DerivedSnapshotMaterialization,
    requested_date: str,
    snapshot_id: str,
    bundle_revision_id: str,
    provenance_class: ProvenanceClass,
    calendar_revision_id: str | None,
    snapshot_id_version: str,
    pin_membership_digest: str | None,
    manifest_json: str | None,
    adjustment_basis: str = "qfq",
) -> ReconstructedMarketSnapshot:
    latest_traded_close = materialization.latest_traded_close
    latest_status = materialization.latest_status
    return ReconstructedMarketSnapshot(
        symbol=bundle.canonical_symbol,
        frame=materialization.frame,
        provider=bundle.provider_name,
        adjustment_basis=adjustment_basis,
        requested_date=requested_date,
        effective_trading_date=materialization.effective_trading_date,
        frame_sha256=materialization.frame_digest,
        snapshot_id=snapshot_id,
        bundle_revision_id=bundle_revision_id,
        provenance_class=provenance_class,
        observation_revision_ids=materialization.observation_revision_ids,
        trading_status_revision_ids=materialization.trading_status_revision_ids,
        factor_revision_ids=materialization.factor_revision_ids,
        calendar_revision_id=calendar_revision_id,
        snapshot_id_version=snapshot_id_version,
        pin_membership_digest=pin_membership_digest,
        manifest_json=manifest_json,
        current_tradeability=(
            "suspended" if latest_status is TradingStatus.SUSPENDED else "tradeable"
        ),
        current_status_provenance=TradingStatusProvenance(
            provider=bundle.provider_name,
            provider_dataset_id=bundle.provider_dataset_id,
            session_date=date.fromisoformat(materialization.effective_trading_date),
            status=latest_status,
            observed_at=datetime.fromisoformat(bundle.observed_at),
            revision_id=materialization.trading_status_revision_ids[-1],
        ),
        latest_traded_close=latest_traded_close,
        latest_traded_close_diagnostic=(
            None
            if latest_traded_close is not None
            else "no_genuinely_traded_close_in_retained_history"
        ),
        carried_suspension_close=(
            Decimal(str(materialization.frame.iloc[-1]["Close"]))
            if latest_status is TradingStatus.SUSPENDED
            else None
        ),
    )


def publish_history_bundle(
    store: MarketHistoryStore,
    publication: ProviderHistoryBundlePublication,
    *,
    prior_bundle_revision_id: str | None = None,
) -> PublishedHistoryBundle:
    publication = replace(
        publication,
        observations=tuple(
            sorted(publication.observations, key=lambda item: item.session_date)
        ),
        trading_statuses=tuple(
            sorted(publication.trading_statuses, key=lambda item: item.session_date)
        ),
        adjustment_factors=tuple(
            sorted(publication.adjustment_factors, key=lambda item: item.effective_date)
        ),
    )
    _validate_bundle(publication)
    artifact = store.install_payload(publication.raw_payload, media_type="application/json")
    observed_at = _utc_text(publication.observed_at)
    retrieval_cutoff = _utc_text(publication.retrieval_cutoff)
    observation_revisions = tuple(
        (
            revision_identity(
                RevisionKind.RAW_MARKET_OBSERVATION,
                {
                    "close": _decimal_text(item.close),
                    "high": _decimal_text(item.high),
                    "instrument_id": publication.instrument.instrument_id,
                    "low": _decimal_text(item.low),
                    "open": _decimal_text(item.open),
                    "payload_digest": artifact.digest,
                    "provider_dataset_id": publication.provider.provider_dataset_id,
                    "session_date": item.session_date.isoformat(),
                    "volume": _decimal_text(item.volume),
                },
            ),
            item,
        )
        for item in publication.observations
    )
    status_revisions = tuple(
        (
            revision_identity(
                RevisionKind.TRADING_STATUS,
                {
                    "instrument_id": publication.instrument.instrument_id,
                    "official_carried_close": (
                        _decimal_text(item.official_carried_close)
                        if item.official_carried_close is not None
                        else None
                    ),
                    "payload_digest": artifact.digest,
                    "provider_dataset_id": publication.provider.provider_dataset_id,
                    "session_date": item.session_date.isoformat(),
                    "status": item.status.value,
                    "volume": (
                        _decimal_text(item.volume) if item.volume is not None else None
                    ),
                },
            ),
            item,
        )
        for item in publication.trading_statuses
    )
    factor_revisions = tuple(
        (
            revision_identity(
                RevisionKind.ADJUSTMENT_FACTOR,
                {
                    "effective_date": item.effective_date.isoformat(),
                    "factor": _decimal_text(item.factor),
                    "instrument_id": publication.instrument.instrument_id,
                    "payload_digest": artifact.digest,
                    "provider_dataset_id": publication.provider.provider_dataset_id,
                },
            ),
            item,
        )
        for item in publication.adjustment_factors
    )
    connection = store._connection
    prior_observations, prior_statuses, prior_factors = _prior_bundle_memberships(
        connection,
        prior_bundle_revision_id,
        provider_dataset_id=publication.provider.provider_dataset_id,
        instrument_id=publication.instrument.instrument_id,
    )
    observation_ids_by_date = dict(prior_observations)
    observation_ids_by_date.update(
        (item.session_date.isoformat(), revision_id)
        for revision_id, item in observation_revisions
    )
    status_ids_by_date = dict(prior_statuses)
    status_ids_by_date.update(
        (item.session_date.isoformat(), revision_id)
        for revision_id, item in status_revisions
    )
    factor_ids_by_date = dict(prior_factors)
    factor_ids_by_date.update(
        (item.effective_date.isoformat(), revision_id)
        for revision_id, item in factor_revisions
    )
    complete_observation_ids = tuple(
        revision_id for _, revision_id in sorted(observation_ids_by_date.items())
    )
    complete_status_ids = tuple(
        revision_id for _, revision_id in sorted(status_ids_by_date.items())
    )
    complete_factor_ids = tuple(
        revision_id for _, revision_id in sorted(factor_ids_by_date.items())
    )
    operation = "incremental_refresh" if prior_bundle_revision_id else "seed"
    candidate_observation_provenance = {
        revision_id: _candidate_revision_provenance(
            effective_date=item.session_date,
            explicit_provenance=item.provenance_class,
            provider_available_at=item.provider_available_at,
            prior_dates=frozenset(prior_observations),
            publication=publication,
            incremental=prior_bundle_revision_id is not None,
        )
        for revision_id, item in observation_revisions
    }
    candidate_status_provenance = {
        revision_id: _candidate_revision_provenance(
            effective_date=item.session_date,
            explicit_provenance=item.provenance_class,
            provider_available_at=item.provider_available_at,
            prior_dates=frozenset(prior_statuses),
            publication=publication,
            incremental=prior_bundle_revision_id is not None,
        )
        for revision_id, item in status_revisions
    }
    candidate_factor_provenance = {
        revision_id: _candidate_revision_provenance(
            effective_date=item.effective_date,
            explicit_provenance=item.provenance_class,
            provider_available_at=item.provider_available_at,
            prior_dates=frozenset(prior_factors),
            publication=publication,
            incremental=prior_bundle_revision_id is not None,
        )
        for revision_id, item in factor_revisions
    }
    effective_bundle_provenance = _exact_membership_provenance(
        connection,
        observation_revision_ids=complete_observation_ids,
        trading_status_revision_ids=complete_status_ids,
        factor_revision_ids=complete_factor_ids,
        candidate_observation_provenance=candidate_observation_provenance,
        candidate_trading_status_provenance=candidate_status_provenance,
        candidate_factor_provenance=candidate_factor_provenance,
        as_of_cutoff=publication.retrieval_cutoff,
    )
    candidate_run_provenance = (
        ProvenanceClass.OBSERVED_POINT_IN_TIME
        if all(
            _revision_is_opit(metadata, publication.retrieval_cutoff)
            for metadata in (
                *candidate_observation_provenance.values(),
                *candidate_status_provenance.values(),
                *candidate_factor_provenance.values(),
            )
        )
        else ProvenanceClass.RETROSPECTIVE_BACKFILL
    )
    ingestion_run_id = revision_identity(
        RevisionKind.INGESTION_RUN,
        {
            "candidate_revision_provenance": _candidate_provenance_identity(
                ("observation", candidate_observation_provenance),
                ("trading_status", candidate_status_provenance),
                ("adjustment_factor", candidate_factor_provenance),
            ),
            "instrument_id": publication.instrument.instrument_id,
            "observed_at": observed_at,
            "operation": operation,
            "payload_digest": artifact.digest,
            "prior_bundle_revision_id": prior_bundle_revision_id,
            "provider_dataset_id": publication.provider.provider_dataset_id,
            "requested_as_of": publication.requested_as_of.isoformat(),
        },
    )
    bundle_revision_id = revision_identity(
        RevisionKind.HISTORY_BUNDLE,
        {
            "adjustment_factor_revisions": complete_factor_ids,
            "instrument_id": publication.instrument.instrument_id,
            "observation_revisions": complete_observation_ids,
            "provenance_class": effective_bundle_provenance.value,
            "provider_dataset_id": publication.provider.provider_dataset_id,
            "requested_as_of": publication.requested_as_of.isoformat(),
            "retrieval_cutoff": retrieval_cutoff,
            "trading_status_revisions": complete_status_ids,
        },
    )

    connection.execute("BEGIN IMMEDIATE")
    try:
        _register_bundle_entities(connection, publication, observed_at)
        connection.execute(
            "INSERT OR IGNORE INTO ingestion_runs "
            "(run_id, provider_dataset_id, instrument_id, operation, provenance_class, "
            "requested_start, requested_end, started_at, completed_at, status, payload_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?)",
            (
                ingestion_run_id,
                publication.provider.provider_dataset_id,
                publication.instrument.instrument_id,
                operation,
                candidate_run_provenance.value,
                publication.observations[0].session_date.isoformat(),
                publication.requested_as_of.isoformat(),
                observed_at,
                observed_at,
                artifact.digest,
            ),
        )
        for revision_id, item in observation_revisions:
            revision_provenance = candidate_observation_provenance[revision_id]
            connection.execute(
                "INSERT OR IGNORE INTO raw_market_observation_revisions "
                "(revision_id, provider_dataset_id, instrument_id, session_date, open_value, "
                "high_value, low_value, close_value, volume_value, provider_effective_at, "
                "provider_available_at, observed_at, first_observed_at, provenance_class, "
                "payload_digest, ingestion_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    publication.provider.provider_dataset_id,
                    publication.instrument.instrument_id,
                    item.session_date.isoformat(),
                    _decimal_text(item.open),
                    _decimal_text(item.high),
                    _decimal_text(item.low),
                    _decimal_text(item.close),
                    _decimal_text(item.volume),
                    item.session_date.isoformat(),
                    revision_provenance.provider_available_at,
                    observed_at,
                    revision_provenance.first_observed_at,
                    revision_provenance.provenance_class.value,
                    artifact.digest,
                    ingestion_run_id,
                ),
            )
        for revision_id, item in status_revisions:
            revision_provenance = candidate_status_provenance[revision_id]
            connection.execute(
                "INSERT OR IGNORE INTO trading_status_revisions "
                "(revision_id, provider_dataset_id, instrument_id, session_date, trading_status, "
                "official_carried_close_value, volume_value, provider_available_at, observed_at, "
                "first_observed_at, provenance_class, payload_digest, ingestion_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    publication.provider.provider_dataset_id,
                    publication.instrument.instrument_id,
                    item.session_date.isoformat(),
                    item.status.value,
                    (
                        _decimal_text(item.official_carried_close)
                        if item.official_carried_close is not None
                        else None
                    ),
                    _decimal_text(item.volume) if item.volume is not None else None,
                    revision_provenance.provider_available_at,
                    observed_at,
                    revision_provenance.first_observed_at,
                    revision_provenance.provenance_class.value,
                    artifact.digest,
                    ingestion_run_id,
                ),
            )
        for revision_id, item in factor_revisions:
            revision_provenance = candidate_factor_provenance[revision_id]
            connection.execute(
                "INSERT OR IGNORE INTO adjustment_factor_revisions "
                "(revision_id, provider_dataset_id, instrument_id, effective_date, factor_value, "
                "provider_available_at, observed_at, first_observed_at, provenance_class, "
                "payload_digest, ingestion_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    publication.provider.provider_dataset_id,
                    publication.instrument.instrument_id,
                    item.effective_date.isoformat(),
                    _decimal_text(item.factor),
                    revision_provenance.provider_available_at,
                    observed_at,
                    revision_provenance.first_observed_at,
                    revision_provenance.provenance_class.value,
                    artifact.digest,
                    ingestion_run_id,
                ),
            )
        connection.execute(
            "INSERT OR IGNORE INTO history_bundle_revisions "
            "(bundle_revision_id, provider_dataset_id, instrument_id, requested_as_of, "
            "retrieval_cutoff, provenance_class, observed_at, ingestion_run_id, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle_revision_id,
                publication.provider.provider_dataset_id,
                publication.instrument.instrument_id,
                publication.requested_as_of.isoformat(),
                retrieval_cutoff,
                effective_bundle_provenance.value,
                observed_at,
                ingestion_run_id,
                observed_at,
            ),
        )
        _insert_memberships(
            connection,
            bundle_revision_id,
            complete_observation_ids,
            complete_status_ids,
            complete_factor_ids,
        )
        _insert_membership_audit(
            connection,
            bundle_revision_id=bundle_revision_id,
            ingestion_run_id=ingestion_run_id,
            observation_revision_ids=complete_observation_ids,
            trading_status_revision_ids=complete_status_ids,
            factor_revision_ids=complete_factor_ids,
            candidate_observation_provenance=candidate_observation_provenance,
            candidate_trading_status_provenance=candidate_status_provenance,
            candidate_factor_provenance=candidate_factor_provenance,
        )
        connection.execute(
            "INSERT INTO instrument_provider_state "
            "(instrument_id, provider_dataset_id, seeded_at, latest_completed_session, "
            "last_refresh_at, lifecycle_state, last_ingestion_run_id) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?) "
            "ON CONFLICT(instrument_id, provider_dataset_id) DO UPDATE SET "
            "seeded_at = COALESCE(instrument_provider_state.seeded_at, excluded.seeded_at), "
            "latest_completed_session = excluded.latest_completed_session, "
            "last_refresh_at = excluded.last_refresh_at, lifecycle_state = 'active', "
            "last_ingestion_run_id = excluded.last_ingestion_run_id",
            (
                publication.instrument.instrument_id,
                publication.provider.provider_dataset_id,
                observed_at,
                publication.observations[-1].session_date.isoformat(),
                observed_at,
                ingestion_run_id,
            ),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return PublishedHistoryBundle(
        bundle_revision_id=bundle_revision_id,
        ingestion_run_id=ingestion_run_id,
        observation_revision_ids=complete_observation_ids,
        trading_status_revision_ids=complete_status_ids,
        factor_revision_ids=complete_factor_ids,
    )


def _candidate_revision_provenance(
    *,
    effective_date: date,
    explicit_provenance: ProvenanceClass | None,
    provider_available_at: datetime | None,
    prior_dates: frozenset[str],
    publication: ProviderHistoryBundlePublication,
    incremental: bool,
) -> _RevisionProvenanceInput:
    availability_proves_eligibility = (
        provider_available_at is not None
        and provider_available_at.astimezone(timezone.utc)
        <= publication.retrieval_cutoff.astimezone(timezone.utc)
    )
    effective_date_text = effective_date.isoformat()
    unseen_historical = (
        incremental
        and bool(prior_dates)
        and effective_date_text not in prior_dates
        and effective_date_text <= max(prior_dates)
    )
    if explicit_provenance is ProvenanceClass.RETROSPECTIVE_BACKFILL:
        provenance = explicit_provenance
    elif unseen_historical:
        provenance = (
            ProvenanceClass.OBSERVED_POINT_IN_TIME
            if availability_proves_eligibility
            else ProvenanceClass.RETROSPECTIVE_BACKFILL
        )
    elif incremental:
        provenance = (
            explicit_provenance or ProvenanceClass.OBSERVED_POINT_IN_TIME
        )
    else:
        provenance = (
            explicit_provenance
            or (
                ProvenanceClass.OBSERVED_POINT_IN_TIME
                if availability_proves_eligibility
                else publication.provenance_class
            )
        )
    return _RevisionProvenanceInput(
        provenance_class=provenance,
        provider_available_at=(
            _utc_text(provider_available_at)
            if provider_available_at is not None
            else None
        ),
        first_observed_at=_utc_text(publication.observed_at),
    )


def _candidate_provenance_identity(
    *memberships: tuple[str, dict[str, _RevisionProvenanceInput]],
) -> tuple[tuple[str, str, str, str | None, str], ...]:
    return tuple(
        sorted(
            (
                revision_kind,
                revision_id,
                metadata.provenance_class.value,
                metadata.provider_available_at,
                metadata.first_observed_at,
            )
            for revision_kind, candidates in memberships
            for revision_id, metadata in candidates.items()
        )
    )


def _revision_is_opit(
    metadata: _RevisionProvenanceInput,
    as_of_cutoff: datetime,
) -> bool:
    if metadata.provenance_class is not ProvenanceClass.OBSERVED_POINT_IN_TIME:
        return False
    cutoff_utc = as_of_cutoff.astimezone(timezone.utc)
    eligibility_timestamps = (
        metadata.provider_available_at,
        metadata.first_observed_at,
    )
    for eligibility_text in eligibility_timestamps:
        if eligibility_text is None:
            continue
        try:
            eligibility = datetime.fromisoformat(eligibility_text)
        except (TypeError, ValueError):
            continue
        if (
            eligibility.tzinfo is not None
            and eligibility.astimezone(timezone.utc) <= cutoff_utc
        ):
            return True
    return False


def _exact_membership_provenance(
    connection,
    *,
    observation_revision_ids: tuple[str, ...],
    trading_status_revision_ids: tuple[str, ...],
    factor_revision_ids: tuple[str, ...],
    candidate_observation_provenance: dict[str, _RevisionProvenanceInput],
    candidate_trading_status_provenance: dict[str, _RevisionProvenanceInput],
    candidate_factor_provenance: dict[str, _RevisionProvenanceInput],
    as_of_cutoff: datetime,
) -> ProvenanceClass:
    """Classify only the exact revisions selected into the merged bundle."""

    memberships = (
        (
            "observation",
            "raw_market_observation_revisions",
            observation_revision_ids,
            candidate_observation_provenance,
        ),
        (
            "trading_status",
            "trading_status_revisions",
            trading_status_revision_ids,
            candidate_trading_status_provenance,
        ),
        (
            "adjustment_factor",
            "adjustment_factor_revisions",
            factor_revision_ids,
            candidate_factor_provenance,
        ),
    )
    for revision_kind, table, revision_ids, candidate_provenance in memberships:
        placeholders = ",".join("?" for _ in revision_ids)
        evidence_by_revision: dict[str, list[_RevisionProvenanceInput]] = {
            revision_id: [] for revision_id in revision_ids
        }
        for row in connection.execute(
            f"SELECT revision_id, provenance_class, provider_available_at, "
            f"first_observed_at FROM {table} "
            f"WHERE revision_id IN ({placeholders})",
            revision_ids,
        ):
            evidence_by_revision[str(row[0])].append(
                _revision_provenance_input(row[1], row[2], row[3])
            )
        for row in connection.execute(
            "SELECT revision_id, asserted_provenance_class, provider_available_at, "
            "first_observed_at "
            "FROM history_bundle_publication_membership_audit "
            "WHERE revision_kind = ? AND membership_origin = 'refreshed' "
            f"AND revision_id IN ({placeholders})",
            (revision_kind, *revision_ids),
        ):
            evidence_by_revision[str(row[0])].append(
                _revision_provenance_input(row[1], row[2], row[3])
            )
        for revision_id in revision_ids:
            candidate = candidate_provenance.get(revision_id)
            if candidate is not None:
                evidence_by_revision[revision_id].append(candidate)
            evidence = evidence_by_revision[revision_id]
            if not evidence or any(
                not _revision_is_opit(metadata, as_of_cutoff)
                for metadata in evidence
            ):
                return ProvenanceClass.RETROSPECTIVE_BACKFILL
    return ProvenanceClass.OBSERVED_POINT_IN_TIME


def _revision_provenance_input(
    provenance_value: object,
    provider_available_at: object,
    first_observed_at: object,
) -> _RevisionProvenanceInput:
    try:
        provenance = ProvenanceClass(str(provenance_value))
    except ValueError:
        provenance = ProvenanceClass.RETROSPECTIVE_BACKFILL
    return _RevisionProvenanceInput(
        provenance_class=provenance,
        provider_available_at=(
            str(provider_available_at)
            if provider_available_at is not None
            else None
        ),
        first_observed_at=str(first_observed_at),
    )


def read_history_bundle_provenance_audit(
    store: MarketHistoryStore,
    bundle_revision_id: str,
) -> HistoryBundleProvenanceAudit:
    """Read deterministic retained/refreshed lineage for one published bundle."""

    connection = store._connection
    bundle = connection.execute(
        "SELECT requested_as_of, retrieval_cutoff, provenance_class, ingestion_run_id "
        "FROM history_bundle_revisions WHERE bundle_revision_id = ?",
        (bundle_revision_id,),
    ).fetchone()
    if bundle is None:
        raise KeyError(f"unknown history bundle {bundle_revision_id}")
    ingestion_run_id = str(bundle[3])

    def memberships(
        revision_kind: str,
        membership_table: str,
        revision_table: str,
        order_column: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        rows = tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                f"SELECT r.revision_id, r.ingestion_run_id FROM {membership_table} AS m "
                f"JOIN {revision_table} AS r ON r.revision_id = m.revision_id "
                "WHERE m.bundle_revision_id = ? "
                f"ORDER BY r.{order_column}, r.revision_id",
                (bundle_revision_id,),
            )
        )
        exact = tuple(revision_id for revision_id, _ in rows)
        audited_origins = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT revision_id, membership_origin "
                "FROM history_bundle_publication_membership_audit "
                "WHERE bundle_revision_id = ? AND ingestion_run_id = ? "
                "AND revision_kind = ?",
                (bundle_revision_id, ingestion_run_id, revision_kind),
            )
        }
        if audited_origins and set(audited_origins) != set(exact):
            raise RuntimeError(
                "history bundle provenance audit membership is incomplete"
            )
        origins = audited_origins or {
            revision_id: (
                "refreshed"
                if revision_run_id == ingestion_run_id
                else "retained"
            )
            for revision_id, revision_run_id in rows
        }
        retained = tuple(
            revision_id for revision_id in exact if origins[revision_id] == "retained"
        )
        refreshed = tuple(
            revision_id for revision_id in exact if origins[revision_id] == "refreshed"
        )
        return exact, retained, refreshed

    observations, retained_observations, refreshed_observations = memberships(
        "observation",
        "history_bundle_observation_revisions",
        "raw_market_observation_revisions",
        "session_date",
    )
    statuses, retained_statuses, refreshed_statuses = memberships(
        "trading_status",
        "history_bundle_trading_status_revisions",
        "trading_status_revisions",
        "session_date",
    )
    factors, retained_factors, refreshed_factors = memberships(
        "adjustment_factor",
        "history_bundle_adjustment_factor_revisions",
        "adjustment_factor_revisions",
        "effective_date",
    )
    snapshot_v2_memberships = _read_snapshot_v2_membership_audits(
        connection,
        bundle_revision_id,
    )
    return HistoryBundleProvenanceAudit(
        bundle_revision_id=bundle_revision_id,
        requested_as_of=date.fromisoformat(str(bundle[0])),
        as_of_cutoff=datetime.fromisoformat(str(bundle[1])),
        provenance_class=ProvenanceClass(str(bundle[2])),
        observation_revision_ids=observations,
        trading_status_revision_ids=statuses,
        factor_revision_ids=factors,
        retained_observation_revision_ids=retained_observations,
        refreshed_observation_revision_ids=refreshed_observations,
        retained_trading_status_revision_ids=retained_statuses,
        refreshed_trading_status_revision_ids=refreshed_statuses,
        retained_factor_revision_ids=retained_factors,
        refreshed_factor_revision_ids=refreshed_factors,
        snapshot_v2_memberships=snapshot_v2_memberships,
    )


def _read_snapshot_v2_membership_audits(
    connection,
    bundle_revision_id: str,
) -> tuple[SnapshotV2MembershipAudit, ...]:
    audits = []
    for pin in connection.execute(
        "SELECT snapshot_id, identity_version, manifest_digest, "
        "calendar_revision_id, requested_as_of FROM snapshot_pins "
        "WHERE bundle_revision_id = ? AND identity_version = 'v2' "
        "ORDER BY snapshot_id",
        (bundle_revision_id,),
    ):
        if pin[2] is None or pin[3] is None:
            raise RuntimeError("snapshot:v2 provenance audit metadata is incomplete")
        snapshot_id = str(pin[0])
        observation_status_membership = tuple(
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT session_date, observation_revision_id, "
                "trading_status_revision_id FROM snapshot_observation_pins "
                "WHERE snapshot_id = ? ORDER BY ordinal",
                (snapshot_id,),
            )
        )
        factor_revision_ids = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT factor_revision_id FROM snapshot_factor_pins "
                "WHERE snapshot_id = ? ORDER BY factor_revision_id",
                (snapshot_id,),
            )
        )
        if not observation_status_membership or not factor_revision_ids:
            raise RuntimeError("snapshot:v2 provenance audit membership is incomplete")
        audits.append(
            SnapshotV2MembershipAudit(
                snapshot_id=snapshot_id,
                identity_version=str(pin[1]),
                membership_digest=str(pin[2]),
                calendar_revision_id=str(pin[3]),
                requested_as_of=date.fromisoformat(str(pin[4])),
                observation_status_membership=observation_status_membership,
                factor_revision_ids=factor_revision_ids,
            )
        )
    return tuple(audits)


def reconstruct_snapshot(
    store: MarketHistoryStore,
    bundle_revision_id: str,
    *,
    requested_date: date,
    purpose: SnapshotPurpose,
    replay_as_of: datetime | None,
    pin: bool,
) -> ReconstructedMarketSnapshot:
    connection = store._connection
    bundle_record = connection.execute(
        "SELECT b.provider_dataset_id, d.provider_name, b.instrument_id, "
        "i.canonical_symbol, b.retrieval_cutoff, b.provenance_class, b.observed_at, "
        "d.upstream_service_id, i.identity_revision, i.reference_market, "
        "i.instrument_kind, i.currency "
        "FROM history_bundle_revisions AS b "
        "JOIN provider_datasets AS d ON d.provider_dataset_id = b.provider_dataset_id "
        "JOIN instruments AS i ON i.instrument_id = b.instrument_id "
        "WHERE b.bundle_revision_id = ?",
        (bundle_revision_id,),
    ).fetchone()
    if bundle_record is None:
        raise KeyError(f"unknown history bundle {bundle_revision_id}")
    bundle = _BundleRow(*(str(value) for value in bundle_record))
    provenance = ProvenanceClass(bundle.provenance_class)
    if purpose is SnapshotPurpose.STRICT_REPLAY:
        if replay_as_of is None or replay_as_of.tzinfo is None:
            raise StrictReplayUnavailable(
                "strict replay requires a timezone-aware replay as-of timestamp"
            )
        retrieval_cutoff = datetime.fromisoformat(bundle.retrieval_cutoff)
        if retrieval_cutoff > replay_as_of:
            raise StrictReplayUnavailable(
                "history bundle retrieval cutoff is after the replay as-of timestamp"
            )
        observed_at = datetime.fromisoformat(bundle.observed_at)
        if observed_at > replay_as_of:
            raise StrictReplayUnavailable(
                "history bundle was observed after the replay as-of timestamp"
            )
        if provenance is not ProvenanceClass.OBSERVED_POINT_IN_TIME:
            raise StrictReplayUnavailable(
                "strict replay requires Observed Point-in-Time History; "
                "retrospective backfill is ineligible"
            )
    observation_rows = tuple(
        _ObservationRevisionRow(*(str(value) for value in row))
        for row in connection.execute(
            "SELECT o.revision_id, o.session_date, o.open_value, o.high_value, "
            "o.low_value, o.close_value, o.volume_value "
            "FROM history_bundle_observation_revisions AS m "
            "JOIN raw_market_observation_revisions AS o ON o.revision_id = m.revision_id "
            "WHERE m.bundle_revision_id = ? AND o.session_date <= ? ORDER BY o.session_date",
            (bundle_revision_id, requested_date.isoformat()),
        )
    )
    if not observation_rows:
        raise StrictReplayUnavailable("history bundle has no observations by the requested date")
    statuses: dict[str, _TradingStatusRevisionRow] = {}
    for row in connection.execute(
        "SELECT s.revision_id, s.session_date, s.trading_status, "
        "s.official_carried_close_value, s.volume_value "
        "FROM history_bundle_trading_status_revisions AS m "
        "JOIN trading_status_revisions AS s ON s.revision_id = m.revision_id "
        "WHERE m.bundle_revision_id = ?",
        (bundle_revision_id,),
    ):
        status = _TradingStatusRevisionRow(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]) if row[3] is not None else None,
            str(row[4]) if row[4] is not None else None,
        )
        statuses[status.session_date] = status
    factors = tuple(
        _FactorRevisionRow(*(str(value) for value in row))
        for row in connection.execute(
            "SELECT f.revision_id, f.effective_date, f.factor_value "
            "FROM history_bundle_adjustment_factor_revisions AS m "
            "JOIN adjustment_factor_revisions AS f ON f.revision_id = m.revision_id "
            "WHERE m.bundle_revision_id = ? ORDER BY f.effective_date",
            (bundle_revision_id,),
        )
    )
    calendar_query = (
        "SELECT calendar_revision_id FROM market_session_calendars "
        "WHERE reference_market = ?"
    )
    calendar_parameters: tuple[object, ...] = (bundle.reference_market,)
    if purpose is SnapshotPurpose.STRICT_REPLAY:
        calendar_query += " AND observed_at <= ?"
        calendar_parameters += (replay_as_of.isoformat(),)
    calendar_query += " ORDER BY observed_at DESC, calendar_revision_id DESC LIMIT 1"
    calendar = connection.execute(calendar_query, calendar_parameters).fetchone()
    if calendar is None:
        raise StrictReplayUnavailable(
            "snapshot reconstruction requires an applicable Market Session Calendar"
        )
    calendar_revision_id = str(calendar[0])
    observation_inputs: list[_SnapshotObservationInput] = []
    for observation in observation_rows:
        status_row = statuses.get(observation.session_date)
        if status_row is None:
            raise StrictReplayUnavailable(
                f"history bundle has no trading status for {observation.session_date}"
            )
        observation_inputs.append(
            _SnapshotObservationInput(
                observation_revision_id=observation.revision_id,
                trading_status_revision_id=status_row.revision_id,
                session_date=observation.session_date,
                open_value=Decimal(observation.open_value),
                high_value=Decimal(observation.high_value),
                low_value=Decimal(observation.low_value),
                close_value=Decimal(observation.close_value),
                volume_value=Decimal(observation.volume_value),
                trading_status=TradingStatus(status_row.trading_status),
                official_carried_close=(
                    Decimal(status_row.official_carried_close_value)
                    if status_row.official_carried_close_value is not None
                    else None
                ),
                status_volume=(
                    Decimal(status_row.volume_value)
                    if status_row.volume_value is not None
                    else None
                ),
            )
        )
    materialization = _derive_qfq_materialization(
        tuple(observation_inputs),
        tuple(
            _SnapshotFactorInput(
                revision_id=item.revision_id,
                effective_date=item.effective_date,
                factor_value=Decimal(item.factor_value),
            )
            for item in factors
        ),
        error=StrictReplayUnavailable,
    )
    identity = snapshot_v2_identity(
        instrument_id=bundle.instrument_id,
        canonical_symbol=bundle.canonical_symbol,
        identity_revision=bundle.identity_revision,
        reference_market=bundle.reference_market,
        instrument_kind=bundle.instrument_kind,
        currency=bundle.currency,
        provider_dataset_id=bundle.provider_dataset_id,
        provider_name=bundle.provider_name,
        upstream_service_id=bundle.upstream_service_id,
        requested_date=requested_date.isoformat(),
        effective_trading_date=materialization.effective_trading_date,
        adjustment_basis="qfq",
        frame_digest=materialization.frame_digest,
        history_rows=len(materialization.frame),
        derivation_version=SNAPSHOT_DERIVATION_VERSION,
        normalization_version=SNAPSHOT_NORMALIZATION_VERSION,
        observation_membership=tuple(
            (
                observation.session_date,
                observation.observation_revision_id,
                observation.trading_status_revision_id,
                observation.trading_status.value,
            )
            for observation in observation_inputs
        ),
        factor_revision_ids=materialization.factor_revision_ids,
        calendar_revision_id=calendar_revision_id,
        provenance_class=provenance.value,
        bundle_revision_id=bundle_revision_id,
        retrieval_cutoff=bundle.retrieval_cutoff,
        bundle_observed_at=bundle.observed_at,
    )
    result = _build_reconstructed_snapshot(
        bundle=bundle,
        materialization=materialization,
        requested_date=requested_date.isoformat(),
        snapshot_id=identity.snapshot_id,
        bundle_revision_id=bundle_revision_id,
        provenance_class=provenance,
        calendar_revision_id=calendar_revision_id,
        snapshot_id_version="v2",
        pin_membership_digest=identity.membership_digest,
        manifest_json=identity.manifest_json,
    )
    if pin:
        _pin_snapshot(
            store,
            result,
            bundle.provider_dataset_id,
            bundle.instrument_id,
            bundle.retrieval_cutoff,
            bundle.reference_market,
        )
    return result


def read_pinned_snapshot(
    store: MarketHistoryStore,
    snapshot_id: str,
) -> ReconstructedMarketSnapshot:
    if (
        SNAPSHOT_V2_ID_PATTERN.fullmatch(snapshot_id) is None
        and LEGACY_SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
    ):
        raise StrictReplayUnavailable(f"unsupported pinned snapshot identity {snapshot_id}")
    connection = store._connection
    parent_record = connection.execute(
        "SELECT bundle_revision_id, instrument_id, provider_dataset_id, "
        "calendar_revision_id, requested_as_of, retrieval_cutoff, adjustment_basis, "
        "derivation_version, frame_digest, provenance_class, history_store_degraded, "
        "identity_version, manifest_digest, manifest_json, normalization_version, "
        "effective_trading_date, history_rows FROM snapshot_pins WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if parent_record is None:
        raise StrictReplayUnavailable(f"pinned snapshot {snapshot_id} is unavailable")
    parent = _SnapshotPinParentRow(*parent_record)
    bundle_record = connection.execute(
        "SELECT b.provider_dataset_id, d.provider_name, b.instrument_id, "
        "i.canonical_symbol, b.retrieval_cutoff, b.provenance_class, b.observed_at, "
        "d.upstream_service_id, i.identity_revision, i.reference_market, "
        "i.instrument_kind, i.currency FROM history_bundle_revisions AS b "
        "JOIN provider_datasets AS d ON d.provider_dataset_id = b.provider_dataset_id "
        "JOIN instruments AS i ON i.instrument_id = b.instrument_id "
        "WHERE b.bundle_revision_id = ?",
        (parent.bundle_revision_id,),
    ).fetchone()
    if bundle_record is None:
        raise SnapshotPinCorruptionError("pinned snapshot parent bundle is unavailable")
    bundle = _BundleRow(*(str(value) for value in bundle_record))
    if (
        bundle.provider_dataset_id != parent.provider_dataset_id
        or bundle.instrument_id != parent.instrument_id
        or bundle.retrieval_cutoff != parent.retrieval_cutoff
        or bundle.provenance_class != parent.provenance_class
    ):
        raise SnapshotPinCorruptionError(
            "pinned snapshot parent metadata contradicts its history bundle"
        )

    pinned_rows = tuple(
        _PinnedObservationRow(*row)
        for row in connection.execute(
            "SELECT p.ordinal, p.session_date, p.observation_revision_id, "
            "p.trading_status_revision_id, o.session_date, o.open_value, o.high_value, "
            "o.low_value, o.close_value, o.volume_value, o.payload_digest, "
            "o.provider_dataset_id, o.instrument_id, s.session_date, s.trading_status, "
            "s.official_carried_close_value, s.volume_value, s.payload_digest, "
            "s.provider_dataset_id, s.instrument_id FROM snapshot_observation_pins AS p "
            "LEFT JOIN raw_market_observation_revisions AS o "
            "ON o.revision_id = p.observation_revision_id "
            "LEFT JOIN trading_status_revisions AS s "
            "ON s.revision_id = p.trading_status_revision_id "
            "WHERE p.snapshot_id = ? ORDER BY p.ordinal",
            (snapshot_id,),
        )
    )
    if not pinned_rows or tuple(int(row.ordinal) for row in pinned_rows) != tuple(
        range(len(pinned_rows))
    ):
        raise SnapshotPinCorruptionError(
            "pinned snapshot observation/status membership is incomplete"
        )
    for row in pinned_rows:
        if (
            row.observation_revision_id is None
            or row.trading_status_revision_id is None
            or row.observation_session_date is None
            or row.observation_payload_digest is None
            or row.status_session_date is None
            or row.trading_status is None
            or row.status_payload_digest is None
        ):
            raise SnapshotPinCorruptionError(
                "pinned snapshot observation/status revision is unavailable"
            )
        if not (
            row.pinned_session_date
            == row.observation_session_date
            == row.status_session_date
            and row.observation_provider_dataset_id
            == row.status_provider_dataset_id
            == parent.provider_dataset_id
            and row.observation_instrument_id
            == row.status_instrument_id
            == parent.instrument_id
        ):
            raise SnapshotPinCorruptionError(
                "pinned snapshot observation/status membership is inconsistent"
            )
        expected_observation_id = revision_identity(
            RevisionKind.RAW_MARKET_OBSERVATION,
            {
                "close": str(row.close_value),
                "high": str(row.high_value),
                "instrument_id": str(row.observation_instrument_id),
                "low": str(row.low_value),
                "open": str(row.open_value),
                "payload_digest": row.observation_payload_digest,
                "provider_dataset_id": str(row.observation_provider_dataset_id),
                "session_date": row.observation_session_date,
                "volume": str(row.volume_value),
            },
        )
        if expected_observation_id != row.observation_revision_id:
            raise SnapshotPinCorruptionError(
                "pinned snapshot observation revision identity is inconsistent"
            )
        expected_status_id = revision_identity(
            RevisionKind.TRADING_STATUS,
            {
                "instrument_id": str(row.status_instrument_id),
                "official_carried_close": (
                    str(row.official_carried_close_value)
                    if row.official_carried_close_value is not None
                    else None
                ),
                "payload_digest": row.status_payload_digest,
                "provider_dataset_id": str(row.status_provider_dataset_id),
                "session_date": row.status_session_date,
                "status": row.trading_status,
                "volume": (
                    str(row.status_volume_value)
                    if row.status_volume_value is not None
                    else None
                ),
            },
        )
        if expected_status_id != row.trading_status_revision_id:
            raise SnapshotPinCorruptionError(
                "pinned snapshot trading-status revision identity is inconsistent"
            )

    pinned_factors = tuple(
        _PinnedFactorRow(*row)
        for row in connection.execute(
            "SELECT p.factor_revision_id, f.effective_date, f.factor_value, "
            "f.payload_digest, f.provider_dataset_id, f.instrument_id "
            "FROM snapshot_factor_pins AS p "
            "LEFT JOIN adjustment_factor_revisions AS f "
            "ON f.revision_id = p.factor_revision_id "
            "WHERE p.snapshot_id = ? ORDER BY f.effective_date, p.factor_revision_id",
            (snapshot_id,),
        )
    )
    if not pinned_factors:
        raise SnapshotPinCorruptionError("pinned snapshot factor membership is incomplete")
    for row in pinned_factors:
        if (
            row.revision_id is None
            or row.effective_date is None
            or row.factor_value is None
            or row.payload_digest is None
            or row.provider_dataset_id != parent.provider_dataset_id
            or row.instrument_id != parent.instrument_id
        ):
            raise SnapshotPinCorruptionError(
                "pinned snapshot factor membership is inconsistent"
            )
        expected_factor_id = revision_identity(
            RevisionKind.ADJUSTMENT_FACTOR,
            {
                "effective_date": row.effective_date,
                "factor": row.factor_value,
                "instrument_id": str(row.instrument_id),
                "payload_digest": row.payload_digest,
                "provider_dataset_id": str(row.provider_dataset_id),
            },
        )
        if expected_factor_id != row.revision_id:
            raise SnapshotPinCorruptionError(
                "pinned snapshot factor revision identity is inconsistent"
            )
    bundle_observations = {
        str(row[0])
        for row in connection.execute(
            "SELECT revision_id FROM history_bundle_observation_revisions "
            "WHERE bundle_revision_id = ?",
            (parent.bundle_revision_id,),
        )
    }
    bundle_statuses = {
        str(row[0])
        for row in connection.execute(
            "SELECT revision_id FROM history_bundle_trading_status_revisions "
            "WHERE bundle_revision_id = ?",
            (parent.bundle_revision_id,),
        )
    }
    bundle_factors = {
        str(row[0])
        for row in connection.execute(
            "SELECT revision_id FROM history_bundle_adjustment_factor_revisions "
            "WHERE bundle_revision_id = ?",
            (parent.bundle_revision_id,),
        )
    }
    if (
        not {row.observation_revision_id for row in pinned_rows} <= bundle_observations
        or not {row.trading_status_revision_id for row in pinned_rows} <= bundle_statuses
        or not {row.revision_id for row in pinned_factors} <= bundle_factors
    ):
        raise SnapshotPinCorruptionError(
            "pinned snapshot child membership contradicts its parent bundle"
        )

    is_v2 = SNAPSHOT_V2_ID_PATTERN.fullmatch(snapshot_id) is not None
    calendar_revision_id = parent.calendar_revision_id
    calendar_payload_digest: str | None = None
    if is_v2 and calendar_revision_id is None:
        raise SnapshotPinCorruptionError("pinned snapshot calendar membership is incomplete")
    if calendar_revision_id is not None:
        calendar = connection.execute(
            "SELECT reference_market, timezone_name, provider_dataset_id, payload_digest "
            "FROM market_session_calendars "
            "WHERE calendar_revision_id = ?",
            (calendar_revision_id,),
        ).fetchone()
        if calendar is None or str(calendar[0]) != bundle.reference_market:
            raise SnapshotPinCorruptionError(
                "pinned snapshot calendar membership is inconsistent"
            )
        calendar_payload_digest = str(calendar[3])
        calendar_session_rows = tuple(
            connection.execute(
                "SELECT session_date, session_status, opens_at, closes_at "
                "FROM market_sessions WHERE calendar_revision_id = ? "
                "ORDER BY session_date",
                (calendar_revision_id,),
            )
        )
        expected_calendar_id = revision_identity(
            RevisionKind.MARKET_SESSION_CALENDAR,
            {
                "payload_digest": calendar_payload_digest,
                "provider_dataset_id": str(calendar[2]),
                "reference_market": str(calendar[0]),
                "sessions": [
                    {
                        "closes_at": str(item[3]) if item[3] is not None else None,
                        "opens_at": str(item[2]) if item[2] is not None else None,
                        "session_date": str(item[0]),
                        "status": str(item[1]),
                    }
                    for item in calendar_session_rows
                ],
                "timezone_name": str(calendar[1]),
            },
        )
        if expected_calendar_id != calendar_revision_id:
            raise SnapshotPinCorruptionError(
                "pinned snapshot calendar revision identity is inconsistent"
            )
        calendar_sessions = {
            str(item[0]): str(item[1]) for item in calendar_session_rows
        }
        if any(
            calendar_sessions.get(row.pinned_session_date) != "open"
            for row in pinned_rows
        ):
            raise SnapshotPinCorruptionError(
                "pinned snapshot calendar sessions are unavailable or inconsistent"
            )

    payload_digests = {
        *(str(row.observation_payload_digest) for row in pinned_rows),
        *(str(row.status_payload_digest) for row in pinned_rows),
        *(str(row.payload_digest) for row in pinned_factors),
    }
    if calendar_payload_digest is not None:
        payload_digests.add(calendar_payload_digest)
    try:
        for digest in sorted(payload_digests):
            store.read_payload(digest)
    except Exception as exc:
        raise SnapshotPinCorruptionError(
            f"pinned snapshot payload is unavailable or corrupt: {exc}"
        ) from exc

    factor_inputs = tuple(
        _SnapshotFactorInput(
            revision_id=row.revision_id,
            effective_date=str(row.effective_date),
            factor_value=Decimal(str(row.factor_value)),
        )
        for row in pinned_factors
    )
    observation_inputs = tuple(
        _SnapshotObservationInput(
            observation_revision_id=row.observation_revision_id,
            trading_status_revision_id=row.trading_status_revision_id,
            session_date=row.pinned_session_date,
            open_value=Decimal(str(row.open_value)),
            high_value=Decimal(str(row.high_value)),
            low_value=Decimal(str(row.low_value)),
            close_value=Decimal(str(row.close_value)),
            volume_value=Decimal(str(row.volume_value)),
            trading_status=TradingStatus(str(row.trading_status)),
            official_carried_close=(
                Decimal(str(row.official_carried_close_value))
                if row.official_carried_close_value is not None
                else None
            ),
            status_volume=(
                Decimal(str(row.status_volume_value))
                if row.status_volume_value is not None
                else None
            ),
        )
        for row in pinned_rows
    )
    materialization = _derive_qfq_materialization(
        observation_inputs,
        factor_inputs,
        error=SnapshotPinCorruptionError,
    )
    if frozenset(materialization.factor_revision_ids) != frozenset(
        row.revision_id for row in factor_inputs
    ):
        raise SnapshotPinCorruptionError(
            "pinned snapshot factor membership contains unused revisions"
        )

    if (
        materialization.frame_digest != parent.frame_digest
        or (
            parent.effective_trading_date is not None
            and materialization.effective_trading_date
            != parent.effective_trading_date
        )
        or (
            parent.history_rows is not None
            and len(materialization.frame) != int(parent.history_rows)
        )
    ):
        raise SnapshotPinCorruptionError("pinned snapshot reconstruction digest mismatch")

    pin_membership_digest: str | None = None
    manifest_json: str | None = None
    if is_v2:
        if (
            parent.identity_version != "v2"
            or parent.derivation_version != SNAPSHOT_DERIVATION_VERSION
            or parent.normalization_version != SNAPSHOT_NORMALIZATION_VERSION
        ):
            raise SnapshotPinCorruptionError(
                "pinned snapshot v2 version metadata is unsupported or incomplete"
            )
        identity = snapshot_v2_identity(
            instrument_id=parent.instrument_id,
            canonical_symbol=bundle.canonical_symbol,
            identity_revision=bundle.identity_revision,
            reference_market=bundle.reference_market,
            instrument_kind=bundle.instrument_kind,
            currency=bundle.currency,
            provider_dataset_id=parent.provider_dataset_id,
            provider_name=bundle.provider_name,
            upstream_service_id=bundle.upstream_service_id,
            requested_date=parent.requested_as_of,
            effective_trading_date=materialization.effective_trading_date,
            adjustment_basis=parent.adjustment_basis,
            frame_digest=materialization.frame_digest,
            history_rows=len(materialization.frame),
            derivation_version=parent.derivation_version,
            normalization_version=str(parent.normalization_version),
            observation_membership=tuple(
                (
                    row.session_date,
                    row.observation_revision_id,
                    row.trading_status_revision_id,
                    row.trading_status.value,
                )
                for row in observation_inputs
            ),
            factor_revision_ids=tuple(row.revision_id for row in factor_inputs),
            calendar_revision_id=str(calendar_revision_id),
            provenance_class=parent.provenance_class,
            bundle_revision_id=parent.bundle_revision_id,
            retrieval_cutoff=parent.retrieval_cutoff,
            bundle_observed_at=bundle.observed_at,
        )
        if (
            identity.snapshot_id != snapshot_id
            or identity.membership_digest != parent.manifest_digest
            or identity.manifest_json != parent.manifest_json
        ):
            raise SnapshotPinCorruptionError(
                "pinned snapshot v2 manifest identity is inconsistent"
            )
        pin_membership_digest = identity.membership_digest
        manifest_json = identity.manifest_json
    else:
        if (
            parent.identity_version != "v1"
            or parent.manifest_digest is not None
            or parent.manifest_json is not None
        ):
            raise SnapshotPinCorruptionError(
                "legacy pinned snapshot has colliding version metadata"
            )
        expected_legacy_id = stable_market_snapshot_id(
            symbol=bundle.canonical_symbol,
            provider=bundle.provider_name,
            adjustment_basis=parent.adjustment_basis,
            requested_date=parent.requested_as_of,
            effective_trading_date=materialization.effective_trading_date,
            frame_sha256=materialization.frame_digest,
            history_rows=len(materialization.frame),
        )
        if expected_legacy_id != snapshot_id:
            raise SnapshotPinCorruptionError(
                "legacy pinned snapshot identity is inconsistent"
            )

    provenance = ProvenanceClass(parent.provenance_class)
    return _build_reconstructed_snapshot(
        bundle=bundle,
        materialization=materialization,
        adjustment_basis=parent.adjustment_basis,
        requested_date=parent.requested_as_of,
        snapshot_id=snapshot_id,
        bundle_revision_id=parent.bundle_revision_id,
        provenance_class=provenance,
        calendar_revision_id=calendar_revision_id,
        snapshot_id_version="v2" if is_v2 else "v1",
        pin_membership_digest=pin_membership_digest,
        manifest_json=manifest_json,
    )


def _validate_bundle(publication: ProviderHistoryBundlePublication) -> None:
    if not publication.provider.strict_history_qualified:
        raise ValueError("history bundle provider is not qualified for strict reconstruction")
    if not publication.raw_payload:
        raise ValueError("history bundle payload must not be empty")
    if not publication.observations:
        raise ValueError("history bundle must include observations")
    observation_dates = tuple(item.session_date for item in publication.observations)
    if observation_dates != tuple(sorted(set(observation_dates))):
        raise ValueError("history bundle observation dates must be unique and sorted")
    status_dates = tuple(item.session_date for item in publication.trading_statuses)
    if status_dates != observation_dates:
        raise ValueError("history bundle requires exactly one trading status per observation")
    factor_dates = tuple(item.effective_date for item in publication.adjustment_factors)
    if factor_dates != tuple(sorted(set(factor_dates))):
        raise ValueError("adjustment factor dates must be unique and sorted")
    if not factor_dates or factor_dates[0] > observation_dates[0]:
        raise ValueError("history bundle has no factor effective for its first observation")
    if publication.requested_as_of < observation_dates[-1]:
        raise ValueError("requested_as_of precedes the latest history observation")
    if publication.retrieval_cutoff.tzinfo is None or publication.observed_at.tzinfo is None:
        raise ValueError("history bundle timestamps must be timezone-aware")


def _register_bundle_entities(connection, publication, created_at: str) -> None:
    provider = publication.provider
    instrument = publication.instrument
    connection.execute(
        "INSERT OR IGNORE INTO upstream_services "
        "(upstream_service_id, service_name, account_scope, created_at) VALUES (?, ?, '', ?)",
        (provider.upstream_service_id, provider.upstream_service_name, created_at),
    )
    connection.execute(
        "INSERT OR IGNORE INTO provider_datasets "
        "(provider_dataset_id, provider_name, upstream_service_id, dataset_name, "
        "adjustment_methodology, strict_history_qualified, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            provider.provider_dataset_id,
            provider.provider_name,
            provider.upstream_service_id,
            provider.dataset_name,
            provider.adjustment_methodology,
            int(provider.strict_history_qualified),
            created_at,
        ),
    )
    connection.execute(
        "INSERT OR IGNORE INTO instruments "
        "(instrument_id, canonical_symbol, reference_market, instrument_kind, currency, "
        "identity_revision, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            instrument.instrument_id,
            instrument.canonical_symbol,
            instrument.reference_market,
            instrument.instrument_kind,
            instrument.currency,
            instrument.identity_revision,
            created_at,
        ),
    )


def _prior_bundle_memberships(
    connection,
    prior_bundle_revision_id: str | None,
    *,
    provider_dataset_id: str,
    instrument_id: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    if prior_bundle_revision_id is None:
        return {}, {}, {}
    prior = connection.execute(
        "SELECT provider_dataset_id, instrument_id FROM history_bundle_revisions "
        "WHERE bundle_revision_id = ?",
        (prior_bundle_revision_id,),
    ).fetchone()
    if prior is None:
        raise KeyError(f"unknown prior history bundle {prior_bundle_revision_id}")
    if (str(prior[0]), str(prior[1])) != (provider_dataset_id, instrument_id):
        raise ValueError("incremental history bundle identity does not match its prior")
    observations = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT o.session_date, o.revision_id "
            "FROM history_bundle_observation_revisions AS m "
            "JOIN raw_market_observation_revisions AS o ON o.revision_id = m.revision_id "
            "WHERE m.bundle_revision_id = ?",
            (prior_bundle_revision_id,),
        )
    }
    statuses = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT s.session_date, s.revision_id "
            "FROM history_bundle_trading_status_revisions AS m "
            "JOIN trading_status_revisions AS s ON s.revision_id = m.revision_id "
            "WHERE m.bundle_revision_id = ?",
            (prior_bundle_revision_id,),
        )
    }
    factors = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT f.effective_date, f.revision_id "
            "FROM history_bundle_adjustment_factor_revisions AS m "
            "JOIN adjustment_factor_revisions AS f ON f.revision_id = m.revision_id "
            "WHERE m.bundle_revision_id = ?",
            (prior_bundle_revision_id,),
        )
    }
    return observations, statuses, factors


def _insert_memberships(
    connection,
    bundle_revision_id,
    observation_ids,
    status_ids,
    factor_ids,
) -> None:
    connection.executemany(
        "INSERT OR IGNORE INTO history_bundle_observation_revisions "
        "(bundle_revision_id, revision_id) VALUES (?, ?)",
        ((bundle_revision_id, revision_id) for revision_id in observation_ids),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO history_bundle_trading_status_revisions "
        "(bundle_revision_id, revision_id) VALUES (?, ?)",
        ((bundle_revision_id, revision_id) for revision_id in status_ids),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO history_bundle_adjustment_factor_revisions "
        "(bundle_revision_id, revision_id) VALUES (?, ?)",
        ((bundle_revision_id, revision_id) for revision_id in factor_ids),
    )


def _insert_membership_audit(
    connection,
    *,
    bundle_revision_id: str,
    ingestion_run_id: str,
    observation_revision_ids: tuple[str, ...],
    trading_status_revision_ids: tuple[str, ...],
    factor_revision_ids: tuple[str, ...],
    candidate_observation_provenance: dict[str, _RevisionProvenanceInput],
    candidate_trading_status_provenance: dict[str, _RevisionProvenanceInput],
    candidate_factor_provenance: dict[str, _RevisionProvenanceInput],
) -> None:
    memberships = (
        (
            "observation",
            "raw_market_observation_revisions",
            observation_revision_ids,
            candidate_observation_provenance,
        ),
        (
            "trading_status",
            "trading_status_revisions",
            trading_status_revision_ids,
            candidate_trading_status_provenance,
        ),
        (
            "adjustment_factor",
            "adjustment_factor_revisions",
            factor_revision_ids,
            candidate_factor_provenance,
        ),
    )
    for revision_kind, table, revision_ids, candidate_provenance in memberships:
        placeholders = ",".join("?" for _ in revision_ids)
        stored = {
            str(row[0]): _revision_provenance_input(row[1], row[2], row[3])
            for row in connection.execute(
                f"SELECT revision_id, provenance_class, provider_available_at, "
                f"first_observed_at FROM {table} "
                f"WHERE revision_id IN ({placeholders})",
                revision_ids,
            )
        }
        audit_rows = []
        for revision_id in revision_ids:
            candidate = candidate_provenance.get(revision_id)
            metadata = candidate or stored.get(revision_id)
            if metadata is None:
                raise RuntimeError(
                    f"history bundle audit revision is unavailable: {revision_id}"
                )
            audit_rows.append(
                (
                    ingestion_run_id,
                    bundle_revision_id,
                    revision_kind,
                    revision_id,
                    "refreshed" if candidate is not None else "retained",
                    metadata.provenance_class.value,
                    metadata.provider_available_at,
                    metadata.first_observed_at,
                )
            )
        connection.executemany(
            "INSERT OR IGNORE INTO history_bundle_publication_membership_audit "
            "(ingestion_run_id, bundle_revision_id, revision_kind, revision_id, "
            "membership_origin, asserted_provenance_class, provider_available_at, "
            "first_observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            audit_rows,
        )


def _pin_snapshot(
    store,
    snapshot,
    provider_dataset_id,
    instrument_id,
    retrieval_cutoff,
    reference_market,
) -> None:
    connection = store._connection
    if (
        snapshot.snapshot_id_version != "v2"
        or snapshot.pin_membership_digest is None
        or snapshot.manifest_json is None
        or snapshot.calendar_revision_id is None
    ):
        raise ValueError("new snapshot publication requires a complete v2 manifest")
    parent_values = _SnapshotPinParentRow(
        snapshot.bundle_revision_id,
        instrument_id,
        provider_dataset_id,
        snapshot.calendar_revision_id,
        snapshot.requested_date,
        retrieval_cutoff,
        snapshot.adjustment_basis,
        SNAPSHOT_DERIVATION_VERSION,
        snapshot.frame_sha256,
        snapshot.provenance_class.value,
        0,
        "v2",
        snapshot.pin_membership_digest,
        snapshot.manifest_json,
        SNAPSHOT_NORMALIZATION_VERSION,
        snapshot.effective_trading_date,
        len(snapshot.frame),
    )
    expected_observations = tuple(
        _ExpectedPinnedObservation(
            snapshot.snapshot_id,
            ordinal,
            session_date,
            observation_id,
            status_id,
        )
        for ordinal, (session_date, observation_id, status_id) in enumerate(
            zip(
                snapshot.frame["Date"].dt.strftime("%Y-%m-%d"),
                snapshot.observation_revision_ids,
                snapshot.trading_status_revision_ids,
                strict=True,
            )
        )
    )
    expected_factors = tuple(
        _ExpectedPinnedFactor(snapshot.snapshot_id, factor_id)
        for factor_id in sorted(snapshot.factor_revision_ids)
    )
    expected_observations_by_id = {
        row.observation_revision_id: row for row in expected_observations
    }
    expected_statuses_by_id = {
        row.trading_status_revision_id: row for row in expected_observations
    }
    payload_digests: set[str] = set()
    try:
        for revision_id in snapshot.observation_revision_ids:
            row = connection.execute(
                "SELECT session_date, open_value, high_value, low_value, close_value, "
                "volume_value, payload_digest, provider_dataset_id, instrument_id "
                "FROM raw_market_observation_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown observation revision {revision_id}")
            expected_observation = expected_observations_by_id[revision_id]
            if (
                str(row[0]) != expected_observation.session_date
                or str(row[7]) != provider_dataset_id
                or str(row[8]) != instrument_id
            ):
                raise SnapshotPinCorruptionError(
                    "snapshot pin observation revision contradicts its parent identity"
                )
            expected_id = revision_identity(
                RevisionKind.RAW_MARKET_OBSERVATION,
                {
                    "close": str(row[4]),
                    "high": str(row[2]),
                    "instrument_id": str(row[8]),
                    "low": str(row[3]),
                    "open": str(row[1]),
                    "payload_digest": str(row[6]),
                    "provider_dataset_id": str(row[7]),
                    "session_date": str(row[0]),
                    "volume": str(row[5]),
                },
            )
            if expected_id != revision_id:
                raise SnapshotPinCorruptionError(
                    "snapshot pin observation revision identity is inconsistent"
                )
            payload_digests.add(str(row[6]))
        for revision_id in snapshot.trading_status_revision_ids:
            row = connection.execute(
                "SELECT session_date, trading_status, official_carried_close_value, "
                "volume_value, payload_digest, provider_dataset_id, instrument_id "
                "FROM trading_status_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown trading-status revision {revision_id}")
            expected_status = expected_statuses_by_id[revision_id]
            if (
                str(row[0]) != expected_status.session_date
                or str(row[5]) != provider_dataset_id
                or str(row[6]) != instrument_id
            ):
                raise SnapshotPinCorruptionError(
                    "snapshot pin trading-status revision contradicts its parent identity"
                )
            expected_id = revision_identity(
                RevisionKind.TRADING_STATUS,
                {
                    "instrument_id": str(row[6]),
                    "official_carried_close": (
                        str(row[2]) if row[2] is not None else None
                    ),
                    "payload_digest": str(row[4]),
                    "provider_dataset_id": str(row[5]),
                    "session_date": str(row[0]),
                    "status": str(row[1]),
                    "volume": str(row[3]) if row[3] is not None else None,
                },
            )
            if expected_id != revision_id:
                raise SnapshotPinCorruptionError(
                    "snapshot pin trading-status revision identity is inconsistent"
                )
            payload_digests.add(str(row[4]))
        for revision_id in snapshot.factor_revision_ids:
            row = connection.execute(
                "SELECT effective_date, factor_value, payload_digest, "
                "provider_dataset_id, instrument_id "
                "FROM adjustment_factor_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown factor revision {revision_id}")
            if str(row[3]) != provider_dataset_id or str(row[4]) != instrument_id:
                raise SnapshotPinCorruptionError(
                    "snapshot pin factor revision contradicts its parent identity"
                )
            expected_id = revision_identity(
                RevisionKind.ADJUSTMENT_FACTOR,
                {
                    "effective_date": str(row[0]),
                    "factor": str(row[1]),
                    "instrument_id": str(row[4]),
                    "payload_digest": str(row[2]),
                    "provider_dataset_id": str(row[3]),
                },
            )
            if expected_id != revision_id:
                raise SnapshotPinCorruptionError(
                    "snapshot pin factor revision identity is inconsistent"
                )
            payload_digests.add(str(row[2]))
        calendar = connection.execute(
            "SELECT reference_market, timezone_name, provider_dataset_id, payload_digest "
            "FROM market_session_calendars "
            "WHERE calendar_revision_id = ?",
            (snapshot.calendar_revision_id,),
        ).fetchone()
        if calendar is None:
            raise KeyError(
                f"unknown market session calendar {snapshot.calendar_revision_id}"
            )
        if str(calendar[0]) != reference_market:
            raise SnapshotPinCorruptionError(
                "snapshot pin calendar revision contradicts its parent identity"
            )
        calendar_session_rows = tuple(
            connection.execute(
                "SELECT session_date, session_status, opens_at, closes_at "
                "FROM market_sessions WHERE calendar_revision_id = ? "
                "ORDER BY session_date",
                (snapshot.calendar_revision_id,),
            )
        )
        expected_calendar_id = revision_identity(
            RevisionKind.MARKET_SESSION_CALENDAR,
            {
                "payload_digest": str(calendar[3]),
                "provider_dataset_id": str(calendar[2]),
                "reference_market": str(calendar[0]),
                "sessions": [
                    {
                        "closes_at": str(item[3]) if item[3] is not None else None,
                        "opens_at": str(item[2]) if item[2] is not None else None,
                        "session_date": str(item[0]),
                        "status": str(item[1]),
                    }
                    for item in calendar_session_rows
                ],
                "timezone_name": str(calendar[1]),
            },
        )
        if expected_calendar_id != snapshot.calendar_revision_id:
            raise SnapshotPinCorruptionError(
                "snapshot pin calendar revision identity is inconsistent"
            )
        open_sessions = {
            str(row[0])
            for row in calendar_session_rows
            if str(row[1]) == "open"
        }
        expected_sessions = {row.session_date for row in expected_observations}
        if not expected_sessions <= open_sessions:
            raise SnapshotPinCorruptionError(
                "snapshot pin calendar sessions are unavailable or inconsistent"
            )
        payload_digests.add(str(calendar[3]))
        for digest in sorted(payload_digests):
            store.read_payload(digest)
    except SnapshotPinCorruptionError:
        raise
    except Exception as exc:
        raise SnapshotPinCorruptionError(
            f"snapshot pin payload is unavailable or corrupt: {exc}"
        ) from exc

    connection.execute("BEGIN IMMEDIATE")
    try:
        existing_parent_record = connection.execute(
            "SELECT bundle_revision_id, instrument_id, provider_dataset_id, "
            "calendar_revision_id, requested_as_of, retrieval_cutoff, adjustment_basis, "
            "derivation_version, frame_digest, provenance_class, history_store_degraded, "
            "identity_version, manifest_digest, manifest_json, normalization_version, "
            "effective_trading_date, history_rows FROM snapshot_pins WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()
        if existing_parent_record is None:
            connection.execute(
                "INSERT INTO snapshot_pins "
                "(snapshot_id, bundle_revision_id, instrument_id, provider_dataset_id, "
                "calendar_revision_id, requested_as_of, retrieval_cutoff, adjustment_basis, "
                "derivation_version, frame_digest, provenance_class, history_store_degraded, "
                "created_at, identity_version, manifest_digest, manifest_json, "
                "normalization_version, effective_trading_date, history_rows) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.snapshot_id,
                    *parent_values[:11],
                    datetime.now(timezone.utc).isoformat(),
                    *parent_values[11:],
                ),
            )
            connection.executemany(
                "INSERT INTO snapshot_observation_pins "
                "(snapshot_id, ordinal, session_date, observation_revision_id, "
                "trading_status_revision_id) VALUES (?, ?, ?, ?, ?)",
                expected_observations,
            )
            connection.executemany(
                "INSERT INTO snapshot_factor_pins "
                "(snapshot_id, factor_revision_id) VALUES (?, ?)",
                expected_factors,
            )
        else:
            existing_parent = _SnapshotPinParentRow(*existing_parent_record)
            if tuple(existing_parent) != parent_values:
                if (
                    existing_parent.calendar_revision_id
                    != parent_values.calendar_revision_id
                ):
                    raise SnapshotIdentityCollisionError(
                        "snapshot identity conflicts with calendar membership"
                    )
                raise SnapshotIdentityCollisionError(
                    "snapshot identity conflicts with existing parent metadata"
                )
            existing_observations = tuple(
                connection.execute(
                    "SELECT snapshot_id, ordinal, session_date, observation_revision_id, "
                    "trading_status_revision_id FROM snapshot_observation_pins "
                    "WHERE snapshot_id = ? ORDER BY ordinal",
                    (snapshot.snapshot_id,),
                )
            )
            if existing_observations != expected_observations:
                raise SnapshotIdentityCollisionError(
                    "snapshot identity conflicts with observation/status membership"
                )
            existing_factors = tuple(
                connection.execute(
                    "SELECT snapshot_id, factor_revision_id FROM snapshot_factor_pins "
                    "WHERE snapshot_id = ? ORDER BY factor_revision_id",
                    (snapshot.snapshot_id,),
                )
            )
            if existing_factors != expected_factors:
                raise SnapshotIdentityCollisionError(
                    "snapshot identity conflicts with factor membership"
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()

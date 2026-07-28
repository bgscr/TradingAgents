from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.evidence import stable_market_snapshot_id
from tradingagents.market_history import (
    AdjustmentFactorObservation,
    DataUsageMode,
    InstrumentSpec,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
    ProvenanceClass,
    ProviderDatasetSpec,
    ProviderHistoryBundlePublication,
    RawMarketObservation,
    SnapshotIdentityCollisionError,
    SnapshotPinCorruptionError,
    SnapshotPurpose,
    StrictReplayUnavailable,
    TradingStatus,
    TradingStatusObservation,
    payload_mutation_sidecar_path,
)


def _config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


def _observed_publication(*, retrieval_cutoff: datetime) -> ProviderHistoryBundlePublication:
    return ProviderHistoryBundlePublication(
        provider=ProviderDatasetSpec(
            upstream_service_id="upstream:baostock-tcp",
            upstream_service_name="BaoStock TCP service",
            provider_dataset_id="provider-dataset:baostock-cn-a-v1",
            provider_name="baostock",
            dataset_name="mainland-raw-status-factors-v1",
            adjustment_methodology="baostock-fore-factor-v1",
            strict_history_qualified=True,
        ),
        instrument=InstrumentSpec(
            instrument_id="instrument:600519.SS",
            canonical_symbol="600519.SS",
            reference_market="shanghai",
            instrument_kind="equity",
            currency="CNY",
            identity_revision="registry:mainland-v1",
        ),
        requested_as_of=date(2026, 7, 24),
        retrieval_cutoff=retrieval_cutoff,
        observed_at=retrieval_cutoff,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"provider":"baostock","bundle":"observed-example"}',
        observations=(
            RawMarketObservation(
                session_date=date(2026, 7, 24),
                open=Decimal("10.5"),
                high=Decimal("11.5"),
                low=Decimal("10"),
                close=Decimal("11"),
                volume=Decimal("120"),
            ),
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 24), Decimal("1")),
        ),
    )


def _publish_calendar(
    store: MarketHistoryStore,
    publication: ProviderHistoryBundlePublication,
) -> str:
    published = store.publish_session_calendar(
        MarketSessionCalendarPublication(
            provider=replace(
                publication.provider,
                provider_dataset_id="provider-dataset:baostock-calendar-v1",
                dataset_name="mainland-session-calendar-v1",
                adjustment_methodology="not-applicable",
                strict_history_qualified=False,
            ),
            reference_market=publication.instrument.reference_market,
            timezone_name="Asia/Shanghai",
            observed_at=publication.observed_at,
            provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
            raw_payload=b'{"calendar":"ticket-03"}',
            sessions=tuple(
                MarketSession(item.session_date, MarketSessionStatus.OPEN)
                for item in sorted(
                    publication.observations,
                    key=lambda observation: observation.session_date,
                )
            ),
        )
    )
    return published.calendar_revision_id


def _pin_state(store: MarketHistoryStore, snapshot_id: str) -> tuple[object, ...]:
    parent = store._connection.execute(
        "SELECT * FROM snapshot_pins WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    observations = tuple(
        store._connection.execute(
            "SELECT * FROM snapshot_observation_pins WHERE snapshot_id = ? "
            "ORDER BY ordinal",
            (snapshot_id,),
        )
    )
    factors = tuple(
        store._connection.execute(
            "SELECT * FROM snapshot_factor_pins WHERE snapshot_id = ? "
            "ORDER BY factor_revision_id",
            (snapshot_id,),
        )
    )
    return parent, observations, factors


def _publish_legacy_pin(
    store: MarketHistoryStore,
    snapshot,
    *,
    status_revision_id: str | None = None,
    include_factors: bool = True,
) -> str:
    legacy_id = stable_market_snapshot_id(
        symbol=snapshot.symbol,
        provider=snapshot.provider,
        adjustment_basis=snapshot.adjustment_basis,
        requested_date=snapshot.requested_date,
        effective_trading_date=snapshot.effective_trading_date,
        frame_sha256=snapshot.frame_sha256,
        history_rows=len(snapshot.frame),
    )
    store._connection.execute(
        "INSERT INTO snapshot_pins "
        "(snapshot_id, bundle_revision_id, instrument_id, provider_dataset_id, "
        "calendar_revision_id, requested_as_of, retrieval_cutoff, adjustment_basis, "
        "derivation_version, frame_digest, provenance_class, history_store_degraded, "
        "created_at, identity_version, manifest_digest, manifest_json, "
        "normalization_version, effective_trading_date, history_rows) "
        "SELECT ?, bundle_revision_id, instrument_id, provider_dataset_id, NULL, "
        "requested_as_of, retrieval_cutoff, adjustment_basis, derivation_version, "
        "frame_digest, provenance_class, history_store_degraded, created_at, 'v1', "
        "NULL, NULL, NULL, NULL, NULL FROM snapshot_pins WHERE snapshot_id = ?",
        (legacy_id, snapshot.snapshot_id),
    )
    pinned_rows = tuple(
        store._connection.execute(
            "SELECT ordinal, session_date, observation_revision_id, "
            "trading_status_revision_id FROM snapshot_observation_pins "
            "WHERE snapshot_id = ? ORDER BY ordinal",
            (snapshot.snapshot_id,),
        )
    )
    store._connection.executemany(
        "INSERT INTO snapshot_observation_pins "
        "(snapshot_id, ordinal, session_date, observation_revision_id, "
        "trading_status_revision_id) VALUES (?, ?, ?, ?, ?)",
        (
            (
                legacy_id,
                int(row[0]),
                str(row[1]),
                str(row[2]),
                status_revision_id or str(row[3]),
            )
            for row in pinned_rows
        ),
    )
    if include_factors:
        store._connection.execute(
            "INSERT INTO snapshot_factor_pins (snapshot_id, factor_revision_id) "
            "SELECT ?, factor_revision_id FROM snapshot_factor_pins WHERE snapshot_id = ?",
            (legacy_id, snapshot.snapshot_id),
        )
    return legacy_id


@pytest.mark.unit
def test_bundle_calendar_and_exact_snapshot_hold_mutex_through_references(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    publication = _observed_publication(
        retrieval_cutoff=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    )
    phases: list[str] = []
    store: MarketHistoryStore

    def phase_hook(phase: str, _digest: str) -> None:
        phases.append(phase)
        if phase != "publisher_references_committed":
            return
        assert store._connection.in_transaction is False
        contender = sqlite3.connect(
            payload_mutation_sidecar_path(config.database_path),
            isolation_level=None,
            timeout=0,
        )
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()

    with MarketHistoryStore.open(config, _phase_hook=phase_hook) as store:
        _publish_calendar(store, publication)
        calendar_phases = tuple(phases)
        phases.clear()

        published = store.publish_history_bundle(publication)
        bundle_phases = tuple(phases)
        phases.clear()

        store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        snapshot_phases = tuple(phases)

    assert calendar_phases == (
        "publisher_mutex_waiting",
        "publisher_mutex_acquired",
        "publisher_file_installed",
        "publisher_payload_row_committed",
        "publisher_references_committed",
    )
    assert bundle_phases == calendar_phases
    assert snapshot_phases == (
        "publisher_mutex_waiting",
        "publisher_mutex_acquired",
        "publisher_references_committed",
    )


@pytest.mark.unit
def test_snapshot_v2_identity_changes_for_status_only_revision(tmp_path) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    traded = replace(
        _observed_publication(retrieval_cutoff=observed_at),
        raw_payload=b'{"provider":"baostock","bundle":"same-market-bytes"}',
        observations=(
            RawMarketObservation(
                session_date=date(2026, 7, 24),
                open=Decimal("11"),
                high=Decimal("11"),
                low=Decimal("11"),
                close=Decimal("11"),
                volume=Decimal("0"),
            ),
        ),
    )
    suspended = replace(
        traded,
        retrieval_cutoff=observed_at.replace(hour=11),
        observed_at=observed_at.replace(hour=11),
        trading_statuses=(
            TradingStatusObservation(
                date(2026, 7, 24),
                TradingStatus.SUSPENDED,
                official_carried_close=Decimal("11"),
                volume=Decimal("0"),
            ),
        ),
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, traded)
        traded_bundle = store.publish_history_bundle(traded)
        suspended_bundle = store.publish_history_bundle(suspended)
        traded_snapshot = store.reconstruct_snapshot(
            traded_bundle.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        suspended_snapshot = store.reconstruct_snapshot(
            suspended_bundle.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )

    assert traded_snapshot.frame_sha256 == suspended_snapshot.frame_sha256
    assert traded_snapshot.snapshot_id.startswith("snapshot:v2:")
    assert suspended_snapshot.snapshot_id.startswith("snapshot:v2:")
    assert traded_snapshot.snapshot_id != suspended_snapshot.snapshot_id


@pytest.mark.unit
def test_snapshot_v2_identity_is_stable_across_membership_input_order(tmp_path) -> None:
    publication = ProviderHistoryBundlePublication(
        provider=ProviderDatasetSpec(
            upstream_service_id="upstream:baostock-tcp",
            upstream_service_name="BaoStock TCP service",
            provider_dataset_id="provider-dataset:baostock-cn-a-v1",
            provider_name="baostock",
            dataset_name="mainland-raw-status-factors-v1",
            adjustment_methodology="baostock-fore-factor-v1",
            strict_history_qualified=True,
        ),
        instrument=InstrumentSpec(
            instrument_id="instrument:600519.SS",
            canonical_symbol="600519.SS",
            reference_market="shanghai",
            instrument_kind="equity",
            currency="CNY",
            identity_revision="registry:mainland-v1",
        ),
        requested_as_of=date(2026, 7, 24),
        retrieval_cutoff=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"provider":"baostock","bundle":"ordered-membership"}',
        observations=(
            RawMarketObservation(
                date(2026, 7, 23),
                Decimal("20"),
                Decimal("22"),
                Decimal("18"),
                Decimal("21"),
                Decimal("100"),
            ),
            RawMarketObservation(
                date(2026, 7, 24),
                Decimal("10.5"),
                Decimal("11.5"),
                Decimal("10"),
                Decimal("11"),
                Decimal("120"),
            ),
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 23), TradingStatus.TRADED),
            TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 23), Decimal("0.5")),
            AdjustmentFactorObservation(date(2026, 7, 24), Decimal("1")),
        ),
    )
    permuted = replace(
        publication,
        observations=tuple(reversed(publication.observations)),
        trading_statuses=tuple(reversed(publication.trading_statuses)),
        adjustment_factors=tuple(reversed(publication.adjustment_factors)),
    )

    identities = []
    for root, candidate in ((tmp_path / "ordered", publication), (tmp_path / "permuted", permuted)):
        with MarketHistoryStore.open(_config(root)) as store:
            _publish_calendar(store, candidate)
            published = store.publish_history_bundle(candidate)
            identities.append(
                store.reconstruct_snapshot(
                    published.bundle_revision_id,
                    requested_date=date(2026, 7, 24),
                    purpose=SnapshotPurpose.CURRENT_ANALYSIS,
                ).snapshot_id
            )

    assert identities[0] == identities[1]


@pytest.mark.unit
def test_snapshot_v2_exact_republication_is_idempotent(tmp_path) -> None:
    publication = _observed_publication(
        retrieval_cutoff=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        first = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        before = _pin_state(store, first.snapshot_id)
        second = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        after = _pin_state(store, first.snapshot_id)
        parent_columns = {
            str(row[1])
            for row in store._connection.execute("PRAGMA table_info(snapshot_pins)")
        }

    assert second.snapshot_id == first.snapshot_id
    assert after == before
    assert {
        "identity_version",
        "manifest_digest",
        "manifest_json",
        "normalization_version",
        "effective_trading_date",
        "history_rows",
    } <= parent_columns


@pytest.mark.unit
def test_snapshot_v2_conflicting_provenance_fails_without_mutating_pin(tmp_path) -> None:
    publication = _observed_publication(
        retrieval_cutoff=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        store._connection.execute(
            "UPDATE snapshot_pins SET provenance_class = 'retrospective_backfill' "
            "WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        )
        conflicting = _pin_state(store, snapshot.snapshot_id)

        with pytest.raises(SnapshotIdentityCollisionError, match="parent metadata"):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.CURRENT_ANALYSIS,
            )

        assert _pin_state(store, snapshot.snapshot_id) == conflicting


@pytest.mark.unit
@pytest.mark.parametrize(
    ("conflict", "diagnostic"),
    (
        ("parent", "parent metadata"),
        ("observations", "observation/status membership"),
        ("factors", "factor membership"),
        ("calendar", "calendar membership"),
    ),
)
def test_snapshot_v2_membership_conflicts_fail_atomically(
    tmp_path,
    conflict: str,
    diagnostic: str,
) -> None:
    publication = _observed_publication(
        retrieval_cutoff=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        if conflict == "parent":
            store._connection.execute(
                "UPDATE snapshot_pins SET adjustment_basis = 'conflicting' "
                "WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
        elif conflict == "observations":
            store._connection.execute(
                "UPDATE snapshot_observation_pins SET session_date = '2026-07-23' "
                "WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
        elif conflict == "factors":
            store._connection.execute(
                "DELETE FROM snapshot_factor_pins WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
        else:
            store._connection.execute(
                "UPDATE snapshot_pins SET calendar_revision_id = NULL "
                "WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
        conflicting = _pin_state(store, snapshot.snapshot_id)

        with pytest.raises(SnapshotIdentityCollisionError, match=diagnostic):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.CURRENT_ANALYSIS,
            )

        assert _pin_state(store, snapshot.snapshot_id) == conflicting


@pytest.mark.unit
def test_strict_pin_read_uses_only_exact_pinned_membership(tmp_path) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
        )
        corrected_publication = replace(
            publication,
            retrieval_cutoff=observed_at.replace(hour=11),
            observed_at=observed_at.replace(hour=11),
            raw_payload=b'{"provider":"baostock","bundle":"later-correction"}',
            observations=(
                replace(publication.observations[0], close=Decimal("11.25")),
            ),
        )
        corrected = store.publish_history_bundle(corrected_publication)
        store._connection.execute(
            "INSERT INTO history_bundle_observation_revisions "
            "(bundle_revision_id, revision_id) VALUES (?, ?)",
            (published.bundle_revision_id, corrected.observation_revision_ids[0]),
        )

        replayed = store.read_pinned_snapshot(snapshot.snapshot_id)

    assert replayed.snapshot_id == snapshot.snapshot_id
    assert replayed.observation_revision_ids == snapshot.observation_revision_ids
    assert replayed.frame.equals(snapshot.frame)


@pytest.mark.unit
def test_strict_replay_admits_a_correction_only_at_its_observation_time(
    tmp_path,
) -> None:
    original_observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    correction_observed_at = original_observed_at.replace(hour=11)
    publication = _observed_publication(retrieval_cutoff=original_observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        original = store.publish_history_bundle(publication)
        original_snapshot = store.reconstruct_snapshot(
            original.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=original_observed_at,
        )
        correction = store.publish_history_bundle(
            replace(
                publication,
                retrieval_cutoff=correction_observed_at,
                observed_at=correction_observed_at,
                raw_payload=b'{"provider":"baostock","bundle":"observed-correction"}',
                observations=(
                    replace(publication.observations[0], close=Decimal("11.25")),
                ),
            ),
            prior_bundle_revision_id=original.bundle_revision_id,
        )

        with pytest.raises(StrictReplayUnavailable, match="retrieval cutoff"):
            store.reconstruct_snapshot(
                correction.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=original_observed_at,
            )
        corrected_snapshot = store.reconstruct_snapshot(
            correction.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=correction_observed_at,
        )
        earlier_replay = store.read_pinned_snapshot(original_snapshot.snapshot_id)

    assert earlier_replay.frame["Close"].tolist() == [11.0]
    assert earlier_replay.observation_revision_ids == (
        original_snapshot.observation_revision_ids
    )
    assert corrected_snapshot.frame["Close"].tolist() == [11.25]
    assert corrected_snapshot.observation_revision_ids == (
        correction.observation_revision_ids
    )
    assert corrected_snapshot.snapshot_id != original_snapshot.snapshot_id


@pytest.mark.unit
def test_incremental_bundle_audit_identifies_exact_retained_and_refreshed_members(
    tmp_path,
) -> None:
    original_observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=original_observed_at)
    seed = replace(
        publication,
        observations=(
            RawMarketObservation(
                session_date=date(2026, 7, 23),
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9.5"),
                close=Decimal("10.5"),
                volume=Decimal("100"),
            ),
            *publication.observations,
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 23), TradingStatus.TRADED),
            *publication.trading_statuses,
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 23), Decimal("1")),
        ),
    )
    correction_observed_at = original_observed_at.replace(hour=11)
    correction = replace(
        publication,
        retrieval_cutoff=correction_observed_at,
        observed_at=correction_observed_at,
        raw_payload=b'{"provider":"baostock","bundle":"bounded-correction"}',
        observations=(
            replace(publication.observations[0], close=Decimal("11.25")),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 23), Decimal("1")),
        ),
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, seed)
        original = store.publish_history_bundle(seed)
        successor = store.publish_history_bundle(
            correction,
            prior_bundle_revision_id=original.bundle_revision_id,
        )
        snapshot = store.reconstruct_snapshot(
            successor.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        audit = store.read_history_bundle_provenance_audit(
            successor.bundle_revision_id
        )

    manifest = json.loads(snapshot.manifest_json)
    assert len(audit.snapshot_v2_memberships) == 1
    snapshot_membership = audit.snapshot_v2_memberships[0]
    assert snapshot_membership.snapshot_id == snapshot.snapshot_id
    assert snapshot_membership.identity_version == "v2"
    assert snapshot_membership.membership_digest == snapshot.pin_membership_digest
    assert snapshot_membership.calendar_revision_id == snapshot.calendar_revision_id
    assert snapshot_membership.requested_as_of == date(2026, 7, 24)
    assert snapshot_membership.observation_status_membership == tuple(
        (
            item["session_date"],
            item["observation_revision_id"],
            item["trading_status_revision_id"],
        )
        for item in manifest["observations"]
    )
    assert snapshot_membership.factor_revision_ids == tuple(manifest["factors"])
    assert audit.as_of_cutoff == correction_observed_at
    assert audit.provenance_class is ProvenanceClass.OBSERVED_POINT_IN_TIME
    assert audit.observation_revision_ids == successor.observation_revision_ids
    assert audit.trading_status_revision_ids == successor.trading_status_revision_ids
    assert audit.factor_revision_ids == successor.factor_revision_ids
    assert audit.retained_observation_revision_ids == (
        original.observation_revision_ids[0],
    )
    assert audit.refreshed_observation_revision_ids == (
        successor.observation_revision_ids[1],
    )
    assert audit.retained_trading_status_revision_ids == (
        original.trading_status_revision_ids[0],
    )
    assert audit.refreshed_trading_status_revision_ids == (
        successor.trading_status_revision_ids[1],
    )
    assert audit.retained_factor_revision_ids == ()
    assert audit.refreshed_factor_revision_ids == successor.factor_revision_ids
    assert [item["observation_revision_id"] for item in manifest["observations"]] == (
        list(audit.observation_revision_ids)
    )
    assert manifest["factors"] == list(audit.factor_revision_ids)
    assert manifest["retrieval_cutoff"] == correction_observed_at.isoformat()
    assert manifest["provenance_class"] == audit.provenance_class.value


@pytest.mark.unit
def test_incremental_bundle_audit_records_reobserved_exact_members_as_refreshed(
    tmp_path,
) -> None:
    original_observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=original_observed_at)
    refresh_observed_at = original_observed_at.replace(hour=11)
    refresh = replace(
        publication,
        retrieval_cutoff=refresh_observed_at,
        observed_at=refresh_observed_at,
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        original = store.publish_history_bundle(publication)
        successor = store.publish_history_bundle(
            refresh,
            prior_bundle_revision_id=original.bundle_revision_id,
        )
        audit = store.read_history_bundle_provenance_audit(
            successor.bundle_revision_id
        )

    assert successor.observation_revision_ids == original.observation_revision_ids
    assert successor.trading_status_revision_ids == original.trading_status_revision_ids
    assert successor.factor_revision_ids == original.factor_revision_ids
    assert audit.retained_observation_revision_ids == ()
    assert audit.refreshed_observation_revision_ids == successor.observation_revision_ids
    assert audit.retained_trading_status_revision_ids == ()
    assert (
        audit.refreshed_trading_status_revision_ids
        == successor.trading_status_revision_ids
    )
    assert audit.retained_factor_revision_ids == ()
    assert audit.refreshed_factor_revision_ids == successor.factor_revision_ids


@pytest.mark.unit
def test_first_observed_before_cutoff_preserves_opit_when_availability_is_later(
    tmp_path,
) -> None:
    first_observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    cutoff = first_observed_at.replace(hour=11)
    later_availability = cutoff.replace(hour=12)
    base = _observed_publication(retrieval_cutoff=cutoff)
    publication = replace(
        base,
        observed_at=first_observed_at,
        observations=tuple(
            replace(
                item,
                provider_available_at=later_availability,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
            )
            for item in base.observations
        ),
        trading_statuses=tuple(
            replace(
                item,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
            )
            for item in base.trading_statuses
        ),
        adjustment_factors=tuple(
            replace(
                item,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
            )
            for item in base.adjustment_factors
        ),
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=base.requested_as_of,
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )

    assert snapshot.provenance_class is ProvenanceClass.OBSERVED_POINT_IN_TIME


@pytest.mark.unit
def test_incremental_provenance_and_v2_identity_are_input_order_invariant(
    tmp_path,
) -> None:
    session_dates = (date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24))
    seed_observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    refresh_observed_at = seed_observed_at.replace(hour=11)
    base = _observed_publication(retrieval_cutoff=seed_observed_at)
    seed_observations = tuple(
        RawMarketObservation(
            day,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.5"),
            Decimal("100"),
        )
        for day in session_dates
    )
    seed_statuses = tuple(
        TradingStatusObservation(day, TradingStatus.TRADED)
        for day in session_dates
    )
    seed = replace(
        base,
        raw_payload=b'{"provider":"baostock","bundle":"order-seed"}',
        observations=seed_observations,
        trading_statuses=seed_statuses,
        adjustment_factors=(
            AdjustmentFactorObservation(session_dates[0], Decimal("1")),
        ),
    )
    refresh_observations = (
        seed_observations[1],
        replace(seed_observations[2], close=Decimal("10.75")),
    )
    refresh_statuses = seed_statuses[1:]
    refresh = replace(
        base,
        retrieval_cutoff=refresh_observed_at,
        observed_at=refresh_observed_at,
        raw_payload=b'{"provider":"baostock","bundle":"order-refresh"}',
        observations=refresh_observations,
        trading_statuses=refresh_statuses,
        adjustment_factors=seed.adjustment_factors,
    )

    results = []
    for root, seed_candidate, refresh_candidate in (
        (tmp_path / "ordered", seed, refresh),
        (
            tmp_path / "reversed",
            replace(
                seed,
                observations=tuple(reversed(seed.observations)),
                trading_statuses=tuple(reversed(seed.trading_statuses)),
            ),
            replace(
                refresh,
                observations=tuple(reversed(refresh.observations)),
                trading_statuses=tuple(reversed(refresh.trading_statuses)),
            ),
        ),
    ):
        with MarketHistoryStore.open(_config(root)) as store:
            _publish_calendar(store, seed_candidate)
            original = store.publish_history_bundle(seed_candidate)
            successor = store.publish_history_bundle(
                refresh_candidate,
                prior_bundle_revision_id=original.bundle_revision_id,
            )
            snapshot = store.reconstruct_snapshot(
                successor.bundle_revision_id,
                requested_date=session_dates[-1],
                purpose=SnapshotPurpose.CURRENT_ANALYSIS,
            )
            audit = store.read_history_bundle_provenance_audit(
                successor.bundle_revision_id
            )
            results.append((snapshot.snapshot_id, snapshot.provenance_class, audit))

    assert results[0][0] == results[1][0]
    assert results[0][1] is results[1][1] is ProvenanceClass.OBSERVED_POINT_IN_TIME
    assert results[0][2] == results[1][2]


@pytest.mark.unit
def test_conflicting_revision_provenance_is_conservative_and_ingestion_order_invariant(
    tmp_path,
) -> None:
    cutoff = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    base = _observed_publication(retrieval_cutoff=cutoff)

    def with_provenance(
        provenance_class: ProvenanceClass,
    ) -> ProviderHistoryBundlePublication:
        provider_available_at = (
            cutoff
            if provenance_class is ProvenanceClass.OBSERVED_POINT_IN_TIME
            else None
        )
        return replace(
            base,
            provenance_class=provenance_class,
            observations=tuple(
                replace(
                    item,
                    provider_available_at=provider_available_at,
                    provenance_class=provenance_class,
                )
                for item in base.observations
            ),
            trading_statuses=tuple(
                replace(
                    item,
                    provider_available_at=provider_available_at,
                    provenance_class=provenance_class,
                )
                for item in base.trading_statuses
            ),
            adjustment_factors=tuple(
                replace(
                    item,
                    provider_available_at=provider_available_at,
                    provenance_class=provenance_class,
                )
                for item in base.adjustment_factors
            ),
        )

    retrospective = with_provenance(ProvenanceClass.RETROSPECTIVE_BACKFILL)
    observed = with_provenance(ProvenanceClass.OBSERVED_POINT_IN_TIME)
    results = []
    for root, first, second in (
        (tmp_path / "retrospective-first", retrospective, observed),
        (tmp_path / "observed-first", observed, retrospective),
    ):
        with MarketHistoryStore.open(_config(root)) as store:
            _publish_calendar(store, first)
            original = store.publish_history_bundle(first)
            successor = store.publish_history_bundle(
                second,
                prior_bundle_revision_id=original.bundle_revision_id,
            )
            snapshot = store.reconstruct_snapshot(
                successor.bundle_revision_id,
                requested_date=base.requested_as_of,
                purpose=SnapshotPurpose.CURRENT_ANALYSIS,
            )
            results.append(
                (
                    successor.bundle_revision_id,
                    snapshot.snapshot_id,
                    snapshot.provenance_class,
                )
            )

    assert results[0] == results[1]
    assert results[0][2] is ProvenanceClass.RETROSPECTIVE_BACKFILL


@pytest.mark.unit
def test_strict_pin_read_fails_closed_for_missing_referenced_payload(tmp_path) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)
    config = _config(tmp_path)

    with MarketHistoryStore.open(config) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
        )
        payload = store._connection.execute(
            "SELECT a.relative_path FROM raw_market_observation_revisions AS o "
            "JOIN payload_artifacts AS a ON a.digest = o.payload_digest "
            "WHERE o.revision_id = ?",
            (published.observation_revision_ids[0],),
        ).fetchone()
        assert payload is not None
        (config.payload_root / str(payload[0])).unlink()

        with pytest.raises(SnapshotPinCorruptionError, match="payload"):
            store.read_pinned_snapshot(snapshot.snapshot_id)


@pytest.mark.unit
def test_strict_pin_read_fails_closed_for_tampered_referenced_payload(tmp_path) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)
    config = _config(tmp_path)

    with MarketHistoryStore.open(config) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
        )
        payload = store._connection.execute(
            "SELECT a.relative_path FROM raw_market_observation_revisions AS o "
            "JOIN payload_artifacts AS a ON a.digest = o.payload_digest "
            "WHERE o.revision_id = ?",
            (published.observation_revision_ids[0],),
        ).fetchone()
        assert payload is not None
        (config.payload_root / str(payload[0])).write_bytes(b"tampered")

        with pytest.raises(SnapshotPinCorruptionError, match="payload"):
            store.read_pinned_snapshot(snapshot.snapshot_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("revision_kind", "table", "id_column", "diagnostic"),
    (
        (
            "observation",
            "raw_market_observation_revisions",
            "revision_id",
            "observation revision",
        ),
        (
            "status",
            "trading_status_revisions",
            "revision_id",
            "trading-status revision",
        ),
        (
            "factor",
            "adjustment_factor_revisions",
            "revision_id",
            "factor revision",
        ),
        (
            "calendar",
            "market_session_calendars",
            "calendar_revision_id",
            "calendar revision",
        ),
    ),
)
def test_strict_pin_read_rejects_revision_rebound_to_different_valid_payload(
    tmp_path,
    revision_kind: str,
    table: str,
    id_column: str,
    diagnostic: str,
) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        calendar_revision_id = _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
        )
        alternate = store.install_payload(
            b'{"provider":"baostock","different-valid-payload":true}',
            media_type="application/json",
        )
        revision_id = {
            "observation": published.observation_revision_ids[0],
            "status": published.trading_status_revision_ids[0],
            "factor": published.factor_revision_ids[0],
            "calendar": calendar_revision_id,
        }[revision_kind]
        store._connection.execute(
            f"UPDATE {table} SET payload_digest = ? WHERE {id_column} = ?",
            (alternate.digest, revision_id),
        )

        with pytest.raises(SnapshotPinCorruptionError, match=diagnostic):
            store.read_pinned_snapshot(snapshot.snapshot_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("table", "revision_kind"),
    (
        ("raw_market_observation_revisions", "observation"),
        ("trading_status_revisions", "status"),
        ("adjustment_factor_revisions", "factor"),
    ),
)
def test_strict_pin_read_fails_closed_when_exact_revision_is_deleted(
    tmp_path,
    table: str,
    revision_kind: str,
) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
        )
        revision_id = {
            "observation": published.observation_revision_ids[0],
            "status": published.trading_status_revision_ids[0],
            "factor": published.factor_revision_ids[0],
        }[revision_kind]
        store._connection.execute("PRAGMA foreign_keys = OFF")
        store._connection.execute(
            f"DELETE FROM {table} WHERE revision_id = ?",
            (revision_id,),
        )
        store._connection.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(SnapshotPinCorruptionError, match=revision_kind):
            store.read_pinned_snapshot(snapshot.snapshot_id)


@pytest.mark.unit
@pytest.mark.parametrize("deleted_part", ("calendar", "session"))
def test_strict_pin_read_fails_closed_when_exact_calendar_state_is_deleted(
    tmp_path,
    deleted_part: str,
) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        calendar_revision_id = _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
        )
        if deleted_part == "calendar":
            store._connection.execute("PRAGMA foreign_keys = OFF")
            store._connection.execute(
                "DELETE FROM market_session_calendars WHERE calendar_revision_id = ?",
                (calendar_revision_id,),
            )
            store._connection.execute("PRAGMA foreign_keys = ON")
        else:
            store._connection.execute(
                "DELETE FROM market_sessions WHERE calendar_revision_id = ? "
                "AND session_date = ?",
                (calendar_revision_id, snapshot.effective_trading_date),
            )

        with pytest.raises(SnapshotPinCorruptionError, match="calendar"):
            store.read_pinned_snapshot(snapshot.snapshot_id)


@pytest.mark.unit
def test_snapshot_v2_publication_rejects_missing_payload_without_partial_pin(
    tmp_path,
) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)
    config = _config(tmp_path)

    with MarketHistoryStore.open(config) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        payload = store._connection.execute(
            "SELECT a.relative_path FROM raw_market_observation_revisions AS o "
            "JOIN payload_artifacts AS a ON a.digest = o.payload_digest "
            "WHERE o.revision_id = ?",
            (published.observation_revision_ids[0],),
        ).fetchone()
        assert payload is not None
        (config.payload_root / str(payload[0])).unlink()

        with pytest.raises(SnapshotPinCorruptionError, match="payload"):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=observed_at,
            )

        pin_count = store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_pins"
        ).fetchone()
        observation_pin_count = store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_observation_pins"
        ).fetchone()
        factor_pin_count = store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_factor_pins"
        ).fetchone()

    assert pin_count == (0,)
    assert observation_pin_count == (0,)
    assert factor_pin_count == (0,)


@pytest.mark.unit
def test_snapshot_v2_publication_rejects_incomplete_calendar_without_partial_pin(
    tmp_path,
) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        calendar_revision_id = _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        store._connection.execute(
            "DELETE FROM market_sessions WHERE calendar_revision_id = ?",
            (calendar_revision_id,),
        )

        with pytest.raises(SnapshotPinCorruptionError, match="calendar"):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=observed_at,
            )

        assert store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_pins"
        ).fetchone() == (0,)
        assert store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_observation_pins"
        ).fetchone() == (0,)
        assert store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_factor_pins"
        ).fetchone() == (0,)


@pytest.mark.unit
def test_snapshot_v2_publication_rejects_inconsistent_revision_without_partial_pin(
    tmp_path,
) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        alternate = store.install_payload(
            b'{"provider":"baostock","different-valid-payload":true}',
            media_type="application/json",
        )
        store._connection.execute(
            "UPDATE raw_market_observation_revisions SET payload_digest = ? "
            "WHERE revision_id = ?",
            (alternate.digest, published.observation_revision_ids[0]),
        )

        with pytest.raises(SnapshotPinCorruptionError, match="observation revision"):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=observed_at,
            )

        assert store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_pins"
        ).fetchone() == (0,)


@pytest.mark.unit
def test_snapshot_v2_publication_rejects_child_from_different_parent_identity(
    tmp_path,
) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        alternate = store.publish_history_bundle(
            replace(
                publication,
                instrument=replace(
                    publication.instrument,
                    instrument_id="instrument:000001.SZ",
                    canonical_symbol="000001.SZ",
                    reference_market="shenzhen",
                ),
                raw_payload=b'{"provider":"baostock","alternate-instrument":true}',
            )
        )
        store._connection.execute(
            "UPDATE history_bundle_observation_revisions SET revision_id = ? "
            "WHERE bundle_revision_id = ?",
            (alternate.observation_revision_ids[0], published.bundle_revision_id),
        )

        with pytest.raises(SnapshotPinCorruptionError, match="observation.*parent"):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=observed_at,
            )

        assert store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_pins"
        ).fetchone() == (0,)


@pytest.mark.unit
def test_valid_legacy_v1_pin_remains_readable_and_incomplete_pin_fails_closed(
    tmp_path,
) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
        )
        legacy_id = _publish_legacy_pin(store, snapshot)
        replayed = store.read_pinned_snapshot(legacy_id)
        store._connection.execute(
            "DELETE FROM snapshot_factor_pins WHERE snapshot_id = ?",
            (legacy_id,),
        )

        with pytest.raises(SnapshotPinCorruptionError, match="factor membership"):
            store.read_pinned_snapshot(legacy_id)

    assert replayed.snapshot_id == legacy_id
    assert replayed.snapshot_id_version == "v1"
    assert replayed.frame.equals(snapshot.frame)


@pytest.mark.unit
def test_colliding_legacy_v1_child_membership_fails_closed(tmp_path) -> None:
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=observed_at)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
        )
        alternate = store.publish_history_bundle(
            replace(
                publication,
                retrieval_cutoff=observed_at.replace(hour=11),
                observed_at=observed_at.replace(hour=11),
                raw_payload=b'{"provider":"baostock","bundle":"alternate-status-revision"}',
            )
        )
        legacy_id = _publish_legacy_pin(
            store,
            snapshot,
            status_revision_id=alternate.trading_status_revision_ids[0],
        )

        with pytest.raises(SnapshotPinCorruptionError, match="parent bundle"):
            store.read_pinned_snapshot(legacy_id)


@pytest.mark.unit
def test_strict_provider_bundle_reconstructs_qfq_and_pins_exact_revisions(tmp_path) -> None:
    publication = ProviderHistoryBundlePublication(
        provider=ProviderDatasetSpec(
            upstream_service_id="upstream:baostock-tcp",
            upstream_service_name="BaoStock TCP service",
            provider_dataset_id="provider-dataset:baostock-cn-a-v1",
            provider_name="baostock",
            dataset_name="mainland-raw-status-factors-v1",
            adjustment_methodology="baostock-fore-factor-v1",
            strict_history_qualified=True,
        ),
        instrument=InstrumentSpec(
            instrument_id="instrument:600519.SS",
            canonical_symbol="600519.SS",
            reference_market="shanghai",
            instrument_kind="equity",
            currency="CNY",
            identity_revision="registry:mainland-v1",
        ),
        requested_as_of=date(2026, 7, 24),
        retrieval_cutoff=datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=b'{"provider":"baostock","bundle":"worked-example"}',
        observations=(
            RawMarketObservation(
                session_date=date(2026, 7, 23),
                open=Decimal("20"),
                high=Decimal("22"),
                low=Decimal("18"),
                close=Decimal("21"),
                volume=Decimal("100"),
            ),
            RawMarketObservation(
                session_date=date(2026, 7, 24),
                open=Decimal("10.5"),
                high=Decimal("11.5"),
                low=Decimal("10"),
                close=Decimal("11"),
                volume=Decimal("120"),
            ),
        ),
        trading_statuses=(
            TradingStatusObservation(date(2026, 7, 23), TradingStatus.TRADED),
            TradingStatusObservation(date(2026, 7, 24), TradingStatus.TRADED),
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(date(2026, 7, 23), Decimal("0.5")),
            AdjustmentFactorObservation(date(2026, 7, 24), Decimal("1")),
        ),
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        reconstructed = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        pinned = store.read_pinned_snapshot(reconstructed.snapshot_id)

    assert reconstructed.provider == "baostock"
    assert reconstructed.adjustment_basis == "qfq"
    assert reconstructed.frame["Open"].tolist() == [10.0, 10.5]
    assert reconstructed.frame["Close"].tolist() == [10.5, 11.0]
    assert reconstructed.frame["Volume"].tolist() == [100.0, 120.0]
    assert reconstructed.observation_revision_ids == published.observation_revision_ids
    assert reconstructed.factor_revision_ids == published.factor_revision_ids
    assert pinned.snapshot_id == reconstructed.snapshot_id
    assert pinned.frame_sha256 == reconstructed.frame_sha256
    assert pinned.frame.equals(reconstructed.frame)
    assert pinned.observation_revision_ids == reconstructed.observation_revision_ids
    assert pinned.trading_status_revision_ids == (
        reconstructed.trading_status_revision_ids
    )
    assert pinned.factor_revision_ids == reconstructed.factor_revision_ids
    assert pinned.calendar_revision_id == reconstructed.calendar_revision_id
    assert pinned.current_tradeability == reconstructed.current_tradeability
    assert pinned.current_status_provenance == reconstructed.current_status_provenance
    assert pinned.latest_traded_close == reconstructed.latest_traded_close


@pytest.mark.unit
def test_strict_replay_rejects_observations_retrieved_after_replay_as_of(
    tmp_path,
) -> None:
    publication = _observed_publication(
        retrieval_cutoff=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        with pytest.raises(StrictReplayUnavailable, match="retrieval cutoff"):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
            )


@pytest.mark.unit
def test_strict_replay_rejects_bundle_observed_after_replay_as_of(tmp_path) -> None:
    replay_as_of = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = replace(
        _observed_publication(
            retrieval_cutoff=datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
        ),
        observed_at=datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc),
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        with pytest.raises(StrictReplayUnavailable, match="observed after"):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=replay_as_of,
            )


@pytest.mark.unit
def test_strict_replay_accepts_observed_history_available_by_replay_as_of(
    tmp_path,
) -> None:
    replay_as_of = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=replay_as_of)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        reconstructed = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=replay_as_of,
        )

    assert reconstructed.provenance_class is ProvenanceClass.OBSERVED_POINT_IN_TIME
    assert reconstructed.frame["Close"].tolist() == [11.0]


@pytest.mark.unit
def test_strict_replay_rejects_retrospective_backfill(tmp_path) -> None:
    replay_as_of = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = replace(
        _observed_publication(retrieval_cutoff=replay_as_of),
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        published = store.publish_history_bundle(publication)
        with pytest.raises(StrictReplayUnavailable, match="retrospective backfill"):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=date(2026, 7, 24),
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=replay_as_of,
            )


@pytest.mark.unit
def test_existing_retrospective_revisions_are_not_relabelled_without_exact_proof(
    tmp_path,
) -> None:
    first_observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = replace(
        _observed_publication(retrieval_cutoff=first_observed_at),
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
    )

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        original = store.publish_history_bundle(publication)
        original_snapshot = store.reconstruct_snapshot(
            original.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        original_revision_state = tuple(
            store._connection.execute(
                "SELECT provenance_class, observed_at, first_observed_at "
                "FROM raw_market_observation_revisions WHERE revision_id = ?",
                (original.observation_revision_ids[0],),
            ).fetchone()
        )

        successor = store.publish_history_bundle(
            replace(
                publication,
                retrieval_cutoff=first_observed_at.replace(hour=11),
                observed_at=first_observed_at.replace(hour=11),
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
            ),
            prior_bundle_revision_id=original.bundle_revision_id,
        )
        successor_snapshot = store.reconstruct_snapshot(
            successor.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
        )
        retained_revision_state = tuple(
            store._connection.execute(
                "SELECT provenance_class, observed_at, first_observed_at "
                "FROM raw_market_observation_revisions WHERE revision_id = ?",
                (successor.observation_revision_ids[0],),
            ).fetchone()
        )
        replayed_original = store.read_pinned_snapshot(original_snapshot.snapshot_id)

    assert successor.observation_revision_ids == original.observation_revision_ids
    assert successor.trading_status_revision_ids == original.trading_status_revision_ids
    assert successor.factor_revision_ids == original.factor_revision_ids
    assert successor_snapshot.provenance_class is ProvenanceClass.RETROSPECTIVE_BACKFILL
    assert retained_revision_state == original_revision_state
    assert retained_revision_state[0] == ProvenanceClass.RETROSPECTIVE_BACKFILL.value
    assert replayed_original.snapshot_id == original_snapshot.snapshot_id
    assert replayed_original.provenance_class is ProvenanceClass.RETROSPECTIVE_BACKFILL


@pytest.mark.unit
def test_pinned_snapshot_fails_closed_when_exact_revision_membership_is_missing(
    tmp_path,
) -> None:
    replay_as_of = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=replay_as_of)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        reconstructed = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=replay_as_of,
        )
        store._connection.execute(
            "DELETE FROM snapshot_observation_pins WHERE snapshot_id = ?",
            (reconstructed.snapshot_id,),
        )

        with pytest.raises(SnapshotPinCorruptionError, match="membership"):
            store.read_pinned_snapshot(reconstructed.snapshot_id)


@pytest.mark.unit
def test_missing_pinned_snapshot_fails_closed(tmp_path) -> None:
    with (
        MarketHistoryStore.open(_config(tmp_path)) as store,
        pytest.raises(StrictReplayUnavailable, match="is unavailable"),
    ):
        store.read_pinned_snapshot("snapshot:" + "0" * 64)


@pytest.mark.unit
def test_corrupt_pinned_snapshot_fails_closed(tmp_path) -> None:
    replay_as_of = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    publication = _observed_publication(retrieval_cutoff=replay_as_of)

    with MarketHistoryStore.open(_config(tmp_path)) as store:
        _publish_calendar(store, publication)
        published = store.publish_history_bundle(publication)
        reconstructed = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=date(2026, 7, 24),
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=replay_as_of,
        )
        store._connection.execute(
            "UPDATE raw_market_observation_revisions SET close_value = '999' "
            "WHERE revision_id = ?",
            (published.observation_revision_ids[0],),
        )

        with pytest.raises(StrictReplayUnavailable, match="revision identity"):
            store.read_pinned_snapshot(reconstructed.snapshot_id)

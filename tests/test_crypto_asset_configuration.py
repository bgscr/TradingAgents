from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from langgraph.graph import END, StateGraph
from pydantic import ValidationError
from typer.testing import CliRunner

from cli import main as cli_main
from tradingagents import asset_configuration as asset_configuration_module
from tradingagents.asset_configuration import (
    ObservationCalendarKind,
    RunAssetConfiguration,
    RunAssetConfigurationError,
    resolve_run_asset_configuration,
)
from tradingagents.dataflows import instrument_identity as identity_module, market_snapshot
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.market_snapshot import (
    SnapshotProvider,
    authoritative_snapshot_run,
)
from tradingagents.decision_audit import prepare_decision_audit
from tradingagents.decision_policy import DecisionHorizon, HorizonUnit
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    EvidenceState,
    InstrumentKind,
    SourceAcquisitionAvailable,
    SourceArtifact,
    acquire_run_evidence,
    capability_profile_for,
    stable_acquisition_source_ref,
    stable_market_snapshot_id,
)
from tradingagents.graph import trading_graph as trading_graph_module
from tradingagents.graph.checkpointer import get_checkpointer, has_checkpoint, thread_id
from tradingagents.graph.evidence_gate import create_preflight_gate_node
from tradingagents.graph.propagation import Propagator
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
    SnapshotPinCorruptionError,
    SnapshotPurpose,
    TradingStatus,
    TradingStatusObservation,
)
from tradingagents.market_history.coordinator import (
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.snapshot_identity import (
    CryptoProviderDatasetDescriptor,
    CryptoSnapshotIdentityMismatch,
    LegacyCryptoSnapshotIdentityIncomplete,
    build_crypto_provider_dataset_descriptor,
    crypto_snapshot_identity_revision,
    crypto_snapshot_instrument_id,
    live_snapshot_v2_identity,
    snapshot_v2_identity,
)
from tradingagents.strategy_registry import create_production_decision_policy


def _crypto_dataset_descriptor(
    *,
    provider_name: str = "yfinance",
    upstream_service_id: str | None = None,
    dataset_name: str = "yahoo-ccc-daily-ohlcv",
    dataset_version: str = "1.0",
    dataset_revision: str = "yfinance-download-auto-adjusted-v1",
    tags: tuple[str, ...] = ("crypto", "ccc", "daily", "ohlcv"),
    reference_market: str = "CCC",
):
    if upstream_service_id is None:
        upstream_service_id, _ = upstream_service_identity_for_provider(provider_name)
    return build_crypto_provider_dataset_descriptor(
        provider_name=provider_name,
        upstream_service_id=upstream_service_id,
        dataset_family="crypto",
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        dataset_revision=dataset_revision,
        tags=tags,
        reference_market=reference_market,
    )


def _mismatched_replay_dataset(
    dataset: CryptoProviderDatasetDescriptor,
    field: str,
) -> CryptoProviderDatasetDescriptor:
    if field == "provider_dataset_id":
        return replace(dataset, provider_dataset_id="provider-dataset:crypto:v1:wrong")
    replacements = {
        "provider_name": "newer-provider",
        "upstream_service_id": "upstream:newer-service",
        "contract_version": "2.0",
        "dataset_family": "mainland-equity",
        "dataset_name": "newer-ccc-bars",
        "dataset_version": "2.0",
        "dataset_revision": "revision-2",
        "reference_market": "XSHG",
        "tags": ("crypto", "ccc", "mainland"),
    }
    values = {**dataset.__dict__, field: replacements[field]}
    values.pop("provider_dataset_id")
    return build_crypto_provider_dataset_descriptor(**values)


def _live_crypto_identity(
    asset_configuration: RunAssetConfiguration,
    *,
    dataset=None,
    **updates,
):
    dataset = dataset or _crypto_dataset_descriptor()
    identity = asset_configuration.instrument_identity
    values = {
        "instrument_id": crypto_snapshot_instrument_id(asset_configuration),
        "canonical_symbol": identity.symbol,
        "identity_revision": crypto_snapshot_identity_revision(asset_configuration),
        "reference_market": identity.venue,
        "instrument_kind": identity.instrument_kind.value,
        "currency": identity.currency,
        "provider_dataset_id": dataset.provider_dataset_id,
        "provider_name": dataset.provider_name,
        "upstream_service_id": dataset.upstream_service_id,
        "requested_date": "2026-07-25",
        "effective_trading_date": "2026-07-25",
        "adjustment_basis": "auto_adjusted",
        "frame_digest": "1" * 64,
        "history_rows": 21,
        "derivation_version": "provider-current-frame-v1",
        "normalization_version": "normalized-frame-csv-v1",
        "accepted_artifact_identity": "artifact:sha256:" + "2" * 64,
        "authoritative_status_identity": "crypto-status=sha256:" + "3" * 64,
        "authoritative_status_value": "not_applicable",
        "provenance_class": "live",
        "asset_configuration": asset_configuration,
        "crypto_provider_dataset": dataset,
    }
    values.update(updates)
    return live_snapshot_v2_identity(**values)


def _publish_corrected_crypto_replay_fixture(tmp_path: Path):
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    dataset = _crypto_dataset_descriptor()
    observed_at = datetime(2026, 7, 25, 23, 0, tzinfo=timezone.utc)
    session_dates = (date(2026, 7, 24), date(2026, 7, 25))
    provider = ProviderDatasetSpec(
        upstream_service_id=dataset.upstream_service_id,
        upstream_service_name="Yahoo Finance service",
        provider_dataset_id=dataset.provider_dataset_id,
        provider_name=dataset.provider_name,
        dataset_name=dataset.dataset_name,
        adjustment_methodology="auto_adjusted",
        strict_history_qualified=True,
    )
    publication = ProviderHistoryBundlePublication(
        provider=provider,
        instrument=InstrumentSpec(
            instrument_id=crypto_snapshot_instrument_id(asset_configuration),
            canonical_symbol="SOL-USD",
            reference_market="CCC",
            instrument_kind="crypto",
            currency="USD",
            identity_revision=crypto_snapshot_identity_revision(asset_configuration),
        ),
        requested_as_of=session_dates[-1],
        retrieval_cutoff=observed_at,
        observed_at=observed_at,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"provider":"yfinance","symbol":"SOL-USD"}',
        observations=tuple(
            RawMarketObservation(
                session_date=session_date,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10"),
            )
            for session_date in session_dates
        ),
        trading_statuses=tuple(
            TradingStatusObservation(session_date, TradingStatus.TRADED)
            for session_date in session_dates
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(session_dates[0], Decimal("1")),
        ),
    )
    history_root = tmp_path / "corrected-crypto-replay"
    history_config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=history_root / "market_history.sqlite3",
        payload_root=history_root / "payloads",
        backup_root=history_root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    with MarketHistoryStore.open(history_config) as store:
        store.publish_session_calendar(
            MarketSessionCalendarPublication(
                provider=replace(
                    provider,
                    provider_dataset_id="provider-dataset:crypto-calendar:v1",
                    dataset_name="crypto-consecutive-daily-calendar-v1",
                    adjustment_methodology="not-applicable",
                    strict_history_qualified=False,
                ),
                reference_market="CCC",
                timezone_name="UTC",
                observed_at=observed_at,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"calendar":"crypto-consecutive-daily"}',
                sessions=tuple(
                    MarketSession(session_date, MarketSessionStatus.OPEN)
                    for session_date in session_dates
                ),
            )
        )
        published = store.publish_history_bundle(publication)
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=session_dates[-1],
            purpose=SnapshotPurpose.STRICT_REPLAY,
            replay_as_of=observed_at,
            asset_configuration=asset_configuration,
            crypto_provider_dataset=dataset,
        )
    return history_config, asset_configuration, dataset, snapshot


def _clone_crypto_pin_with_manifest(
    store: MarketHistoryStore,
    source_snapshot_id: str,
    manifest: dict[str, object],
) -> str:
    manifest_json = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    manifest_digest = sha256(manifest_json.encode("utf-8")).hexdigest()
    snapshot_id = f"snapshot:v2:{manifest_digest}"
    store._connection.execute(
        "INSERT INTO snapshot_pins "
        "(snapshot_id, bundle_revision_id, instrument_id, provider_dataset_id, "
        "calendar_revision_id, requested_as_of, retrieval_cutoff, adjustment_basis, "
        "derivation_version, frame_digest, provenance_class, history_store_degraded, "
        "created_at, identity_version, manifest_digest, manifest_json, "
        "normalization_version, effective_trading_date, history_rows) "
        "SELECT ?, bundle_revision_id, instrument_id, provider_dataset_id, "
        "calendar_revision_id, requested_as_of, retrieval_cutoff, adjustment_basis, "
        "derivation_version, frame_digest, provenance_class, history_store_degraded, "
        "created_at, identity_version, ?, ?, normalization_version, "
        "effective_trading_date, history_rows FROM snapshot_pins WHERE snapshot_id = ?",
        (snapshot_id, manifest_digest, manifest_json, source_snapshot_id),
    )
    store._connection.execute(
        "INSERT INTO snapshot_observation_pins "
        "(snapshot_id, ordinal, session_date, observation_revision_id, "
        "trading_status_revision_id) SELECT ?, ordinal, session_date, "
        "observation_revision_id, trading_status_revision_id "
        "FROM snapshot_observation_pins WHERE snapshot_id = ?",
        (snapshot_id, source_snapshot_id),
    )
    store._connection.execute(
        "INSERT INTO snapshot_factor_pins (snapshot_id, factor_revision_id) "
        "SELECT ?, factor_revision_id FROM snapshot_factor_pins WHERE snapshot_id = ?",
        (snapshot_id, source_snapshot_id),
    )
    return snapshot_id


def _clone_crypto_v1_pin(store: MarketHistoryStore, snapshot) -> str:
    snapshot_id = stable_market_snapshot_id(
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
        (snapshot_id, snapshot.snapshot_id),
    )
    store._connection.execute(
        "INSERT INTO snapshot_observation_pins "
        "(snapshot_id, ordinal, session_date, observation_revision_id, "
        "trading_status_revision_id) SELECT ?, ordinal, session_date, "
        "observation_revision_id, trading_status_revision_id "
        "FROM snapshot_observation_pins WHERE snapshot_id = ?",
        (snapshot_id, snapshot.snapshot_id),
    )
    store._connection.execute(
        "INSERT INTO snapshot_factor_pins (snapshot_id, factor_revision_id) "
        "SELECT ?, factor_revision_id FROM snapshot_factor_pins WHERE snapshot_id = ?",
        (snapshot_id, snapshot.snapshot_id),
    )
    return snapshot_id


def _mismatched_replay_configuration(
    asset_configuration: RunAssetConfiguration,
    field: str,
) -> RunAssetConfiguration:
    identity = asset_configuration.instrument_identity
    provenance = identity.provenance
    assert provenance is not None
    if field == "asset_configuration_version":
        return asset_configuration.model_copy(
            update={"asset_configuration_version": "2.0"}
        )
    if field == "canonical_symbol":
        return asset_configuration.model_copy(
            update={"instrument_identity": identity.model_copy(update={"symbol": "ETH-USD"})}
        )
    if field == "reference_market":
        return asset_configuration.model_copy(
            update={
                "reference_market": "XSHG",
                "instrument_identity": identity.model_copy(update={"venue": "XSHG"}),
            }
        )
    if field == "incoherent_reference_market":
        return asset_configuration.model_copy(update={"reference_market": "XSHG"})
    if field == "instrument_kind":
        return asset_configuration.model_copy(
            update={
                "instrument_identity": identity.model_copy(
                    update={"instrument_kind": InstrumentKind.EQUITY}
                )
            }
        )
    if field == "incoherent_instrument_kind":
        return asset_configuration.model_copy(
            update={"instrument_kind": InstrumentKind.EQUITY}
        )
    if field == "currency":
        return asset_configuration.model_copy(
            update={"instrument_identity": identity.model_copy(update={"currency": "EUR"})}
        )
    if field == "identity_provenance":
        return asset_configuration.model_copy(
            update={
                "instrument_identity": identity.model_copy(
                    update={
                        "provenance": provenance.model_copy(
                            update={"retrieved_at": "2026-07-26T00:00:00+00:00"}
                        )
                    }
                )
            }
        )
    if field == "registry_id":
        return asset_configuration.model_copy(update={"registry_id": "crypto-registry-v2"})
    if field == "registry_digest":
        digest = "a" * 64
        return asset_configuration.model_copy(
            update={
                "registry_digest": digest,
                "instrument_identity": identity.model_copy(
                    update={
                        "provenance": provenance.model_copy(
                            update={"artifact_sha256": digest}
                        )
                    }
                ),
            }
        )
    if field == "capability_profile":
        return asset_configuration.model_copy(
            update={
                "capability_profile": asset_configuration.capability_profile.model_copy(
                    update={"profile_id": "crypto.v2"}
                )
            }
        )
    if field == "incoherent_capability_profile_kind":
        return asset_configuration.model_copy(
            update={
                "capability_profile": asset_configuration.capability_profile.model_copy(
                    update={"instrument_kind": InstrumentKind.EQUITY}
                )
            }
        )
    if field == "observation_calendar":
        return asset_configuration.model_copy(
            update={"observation_calendar_kind": ObservationCalendarKind.MARKET_SESSIONS}
        )
    if field == "adjustment_basis":
        return asset_configuration.model_copy(update={"adjustment_basis": "qfq"})
    raise AssertionError(field)


def _replace_manifest_value(
    manifest: dict[str, object],
    path: tuple[str, ...],
    value: object,
) -> None:
    current = manifest
    for key in path[:-1]:
        child = current[key]
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = value


def _delete_manifest_value(
    manifest: dict[str, object],
    path: tuple[str, ...],
) -> None:
    current = manifest
    for key in path[:-1]:
        child = current[key]
        assert isinstance(child, dict)
        current = child
    del current[path[-1]]


def test_sol_asset_configuration_is_authoritative_immutable_and_crypto_specific():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    assert isinstance(asset_configuration, RunAssetConfiguration)
    assert asset_configuration.instrument_identity.symbol == "SOL-USD"
    assert asset_configuration.instrument_identity.venue == "CCC"
    assert asset_configuration.instrument_kind is InstrumentKind.CRYPTO
    assert asset_configuration.registry_id == "crypto-ccc-identity-registry-v1"
    assert asset_configuration.registry_digest == (
        "d8bd9fa6491af365384015b1e34b452d0df2a8ca5c5b0d9cb0a78908361de5d1"
    )
    assert asset_configuration.reference_market == "CCC"
    assert asset_configuration.capability_profile.profile_id == "crypto.v1"
    assert asset_configuration.observation_calendar_kind is (
        ObservationCalendarKind.CONSECUTIVE_DAILY
    )
    assert asset_configuration.calculation_id == (
        "market.close_return_20d.crypto.auto_adjusted"
    )
    assert asset_configuration.horizon.count == 20
    assert asset_configuration.horizon.unit is HorizonUnit.CALENDAR_DAYS
    assert asset_configuration.asset_configuration_version == "1.0"
    assert asset_configuration.asset_configuration_signature.startswith(
        "asset-config:v1:"
    )
    assert "trading_days" not in asset_configuration.model_dump_json()

    with pytest.raises(ValidationError, match="frozen"):
        asset_configuration.reference_market = "XSHG"


def test_durable_crypto_snapshot_v2_manifest_binds_exact_authoritative_membership():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    dataset = _crypto_dataset_descriptor()
    identity = snapshot_v2_identity(
        instrument_id=crypto_snapshot_instrument_id(asset_configuration),
        canonical_symbol="SOL-USD",
        identity_revision=crypto_snapshot_identity_revision(asset_configuration),
        reference_market="CCC",
        instrument_kind="crypto",
        currency="USD",
        provider_dataset_id=dataset.provider_dataset_id,
        provider_name=dataset.provider_name,
        upstream_service_id=dataset.upstream_service_id,
        requested_date="2026-07-25",
        effective_trading_date="2026-07-25",
        adjustment_basis="auto_adjusted",
        frame_digest="4" * 64,
        history_rows=2,
        derivation_version="crypto-auto-adjusted-v1",
        normalization_version="normalized-frame-csv-v1",
        observation_membership=(
            ("2026-07-25", "observation:2", "status:2", "traded"),
            ("2026-07-24", "observation:1", "status:1", "traded"),
        ),
        factor_revision_ids=("factor:2", "factor:1"),
        calendar_revision_id="crypto-consecutive-daily-calendar:v1",
        provenance_class="observed_point_in_time",
        bundle_revision_id="crypto-bundle:1",
        retrieval_cutoff="2026-07-25T23:59:59+00:00",
        bundle_observed_at="2026-07-25T23:00:00+00:00",
        asset_configuration=asset_configuration,
        crypto_provider_dataset=dataset,
    )

    manifest = json.loads(identity.manifest_json)
    assert identity.snapshot_id == f"snapshot:v2:{identity.membership_digest}"
    assert manifest["snapshot_kind"] == "durable_exact_pin"
    assert manifest["crypto_identity_binding_version"] == "1.0"
    assert manifest["asset_configuration"]["registry"] == {
        "digest": asset_configuration.registry_digest,
        "registry_id": asset_configuration.registry_id,
    }
    assert manifest["provider"]["provider_dataset_id"] == (
        dataset.provider_dataset_id
    )
    assert [row["session_date"] for row in manifest["observations"]] == [
        "2026-07-24",
        "2026-07-25",
    ]
    assert manifest["factors"] == ["factor:1", "factor:2"]
    assert manifest["calendar_revision_id"] == (
        "crypto-consecutive-daily-calendar:v1"
    )
    assert "mainland" not in identity.manifest_json.casefold()
    assert "unknown" not in identity.manifest_json.casefold()


@pytest.mark.parametrize("identity_kind", ("live", "durable"))
def test_unresolved_crypto_symbol_cannot_bypass_typed_identity_validation(
    identity_kind: str,
) -> None:
    common = {
        "instrument_id": "instrument:SOL-USD",
        "canonical_symbol": "SOL-USD",
        "identity_revision": "current-live-symbol-v1",
        "reference_market": "unresolved",
        "instrument_kind": "unknown",
        "currency": "unknown",
        "provider_dataset_id": "provider-dataset:mainland-current-adjusted-v1",
        "provider_name": "yfinance",
        "upstream_service_id": "upstream:yahoo-finance",
        "requested_date": "2026-07-25",
        "effective_trading_date": "2026-07-25",
        "adjustment_basis": "qfq",
        "frame_digest": "1" * 64,
        "history_rows": 1,
        "derivation_version": "provider-current-frame-v1",
        "normalization_version": "normalized-frame-csv-v1",
        "provenance_class": "live",
    }
    if identity_kind == "live":
        def call():
            return live_snapshot_v2_identity(
                **common,
                accepted_artifact_identity="artifact:sha256:" + "2" * 64,
                authoritative_status_identity="unknown",
                authoritative_status_value="unknown",
            )
    else:
        def call():
            return snapshot_v2_identity(
                **common,
                observation_membership=(
                    ("2026-07-25", "observation:1", "status:1", "traded"),
                ),
                factor_revision_ids=("factor:1",),
                calendar_revision_id="mainland-session-calendar:v1",
                bundle_revision_id="bundle:1",
                retrieval_cutoff="2026-07-25T23:59:59+00:00",
                bundle_observed_at="2026-07-25T23:00:00+00:00",
            )

    with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
        call()

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    "updates",
    (
        {
            "observation_membership": (
                ("bad-date", "observation:1", "status:1", "traded"),
            ),
            "history_rows": 1,
        },
        {
            "observation_membership": (
                ("2026-07-25", "", "status:1", "traded"),
            ),
            "history_rows": 1,
        },
        {
            "observation_membership": (
                ("2026-07-25", "observation:1", "", "traded"),
            ),
            "history_rows": 1,
        },
        {
            "observation_membership": (
                ("2026-07-24", "observation:1", "status:1", "unknown"),
                ("2026-07-25", "observation:2", "status:2", "traded"),
            ),
        },
        {"calendar_revision_id": ""},
        {"calendar_revision_id": "mainland-session-calendar:v1"},
        {
            "observation_membership": (
                ("2026-07-23", "observation:1", "status:1", "traded"),
                ("2026-07-25", "observation:2", "status:2", "traded"),
            ),
        },
        {"bundle_revision_id": ""},
        {"retrieval_cutoff": "not-a-timestamp"},
        {"bundle_observed_at": "2026-07-25T23:00:00"},
    ),
)
def test_durable_crypto_membership_rejects_each_incomplete_material_input(
    updates: dict[str, object],
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    dataset = _crypto_dataset_descriptor()
    values = {
        "instrument_id": crypto_snapshot_instrument_id(asset_configuration),
        "canonical_symbol": "SOL-USD",
        "identity_revision": crypto_snapshot_identity_revision(asset_configuration),
        "reference_market": "CCC",
        "instrument_kind": "crypto",
        "currency": "USD",
        "provider_dataset_id": dataset.provider_dataset_id,
        "provider_name": dataset.provider_name,
        "upstream_service_id": dataset.upstream_service_id,
        "requested_date": "2026-07-25",
        "effective_trading_date": "2026-07-25",
        "adjustment_basis": "auto_adjusted",
        "frame_digest": "4" * 64,
        "history_rows": 2,
        "derivation_version": "crypto-auto-adjusted-v1",
        "normalization_version": "normalized-frame-csv-v1",
        "observation_membership": (
            ("2026-07-24", "observation:1", "status:1", "traded"),
            ("2026-07-25", "observation:2", "status:2", "traded"),
        ),
        "factor_revision_ids": ("factor:1",),
        "calendar_revision_id": "crypto-consecutive-daily-calendar:v1",
        "provenance_class": "observed_point_in_time",
        "bundle_revision_id": "crypto-bundle:1",
        "retrieval_cutoff": "2026-07-25T23:59:59+00:00",
        "bundle_observed_at": "2026-07-25T23:00:00+00:00",
        "asset_configuration": asset_configuration,
        "crypto_provider_dataset": dataset,
    }
    values.update(updates)

    with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
        snapshot_v2_identity(**values)

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


def test_public_store_publishes_durable_crypto_pin_from_authoritative_configuration(
    tmp_path: Path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    dataset = _crypto_dataset_descriptor()
    observed_at = datetime(2026, 7, 25, 23, 0, tzinfo=timezone.utc)
    session_dates = (date(2026, 7, 24), date(2026, 7, 25))
    provider = ProviderDatasetSpec(
        upstream_service_id=dataset.upstream_service_id,
        upstream_service_name="Yahoo Finance service",
        provider_dataset_id=dataset.provider_dataset_id,
        provider_name=dataset.provider_name,
        dataset_name=dataset.dataset_name,
        adjustment_methodology="auto_adjusted",
        strict_history_qualified=True,
    )
    publication = ProviderHistoryBundlePublication(
        provider=provider,
        instrument=InstrumentSpec(
            instrument_id=crypto_snapshot_instrument_id(asset_configuration),
            canonical_symbol="SOL-USD",
            reference_market="CCC",
            instrument_kind="crypto",
            currency="USD",
            identity_revision=crypto_snapshot_identity_revision(asset_configuration),
        ),
        requested_as_of=session_dates[-1],
        retrieval_cutoff=observed_at,
        observed_at=observed_at,
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        raw_payload=b'{"provider":"yfinance","symbol":"SOL-USD"}',
        observations=tuple(
            RawMarketObservation(
                session_date=session_date,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10"),
            )
            for session_date in session_dates
        ),
        trading_statuses=tuple(
            TradingStatusObservation(session_date, TradingStatus.TRADED)
            for session_date in session_dates
        ),
        adjustment_factors=(
            AdjustmentFactorObservation(session_dates[0], Decimal("1")),
        ),
    )
    history_root = tmp_path / "crypto-history"
    history_config = MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=history_root / "market_history.sqlite3",
        payload_root=history_root / "payloads",
        backup_root=history_root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )

    with MarketHistoryStore.open(history_config) as store:
        store.publish_session_calendar(
            MarketSessionCalendarPublication(
                provider=replace(
                    provider,
                    provider_dataset_id="provider-dataset:crypto-calendar:v1",
                    dataset_name="crypto-consecutive-daily-calendar-v1",
                    adjustment_methodology="not-applicable",
                    strict_history_qualified=False,
                ),
                reference_market="CCC",
                timezone_name="UTC",
                observed_at=observed_at - timedelta(minutes=1),
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"calendar":"incomplete-crypto-daily"}',
                sessions=(
                    MarketSession(session_dates[0], MarketSessionStatus.OPEN),
                ),
            )
        )
        published = store.publish_history_bundle(publication)
        with pytest.raises(CryptoSnapshotIdentityMismatch):
            store.reconstruct_snapshot(
                published.bundle_revision_id,
                requested_date=session_dates[-1],
                purpose=SnapshotPurpose.CURRENT_ANALYSIS,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=dataset,
            )
        assert store._connection.execute(
            "SELECT COUNT(*) FROM snapshot_pins"
        ).fetchone()[0] == 0

        calendar = store.publish_session_calendar(
            MarketSessionCalendarPublication(
                provider=replace(
                    provider,
                    provider_dataset_id="provider-dataset:crypto-calendar:v1",
                    dataset_name="crypto-consecutive-daily-calendar-v1",
                    adjustment_methodology="not-applicable",
                    strict_history_qualified=False,
                ),
                reference_market="CCC",
                timezone_name="UTC",
                observed_at=observed_at,
                provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
                raw_payload=b'{"calendar":"crypto-consecutive-daily"}',
                sessions=tuple(
                    MarketSession(session_date, MarketSessionStatus.OPEN)
                    for session_date in session_dates
                ),
            )
        )
        snapshot = store.reconstruct_snapshot(
            published.bundle_revision_id,
            requested_date=session_dates[-1],
            purpose=SnapshotPurpose.CURRENT_ANALYSIS,
            asset_configuration=asset_configuration,
            crypto_provider_dataset=dataset,
        )
        pinned = store.read_pinned_snapshot(
            snapshot.snapshot_id,
            asset_configuration=asset_configuration,
            crypto_provider_dataset=dataset,
        )
        stored_manifest = store._connection.execute(
            "SELECT manifest_json FROM snapshot_pins WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()

    manifest = json.loads(snapshot.manifest_json)
    assert snapshot.snapshot_id == pinned.snapshot_id
    assert snapshot.manifest_json == pinned.manifest_json == stored_manifest[0]
    assert snapshot.adjustment_basis == "auto_adjusted"
    assert snapshot.factor_revision_ids
    assert snapshot.calendar_revision_id == calendar.calendar_revision_id
    assert manifest["asset_configuration"]["observation_calendar_kind"] == (
        "consecutive_daily"
    )
    assert manifest["provider"]["dataset"]["reference_market"] == "CCC"
    assert "mainland" not in snapshot.manifest_json.casefold()


def test_historical_crypto_snapshot_ids_and_manifests_are_not_rehashed_or_repaired():
    frame = pd.DataFrame(
        {
            "Date": pd.date_range(start="2026-07-24", periods=2, freq="D"),
            "Open": [100.0, 101.0],
            "High": [100.0, 101.0],
            "Low": [100.0, 101.0],
            "Close": [100.0, 101.0],
            "Volume": [100.0, 100.0],
        }
    )
    historical_manifest = json.dumps(
        {
            "instrument": {
                "canonical_symbol": "SOL-USD",
                "instrument_kind": "unknown",
                "reference_market": "unresolved",
            },
            "provider": {"dataset_name": "mainland-current-adjusted-v1"},
            "snapshot_kind": "current_only_live_artifact",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    historical_digest = sha256(historical_manifest.encode("utf-8")).hexdigest()
    historical_v2 = market_snapshot.AuthoritativeMarketSnapshot(
        symbol="SOL-USD",
        frame=frame,
        provider="yfinance",
        retrieved_at="2026-07-25T23:00:00+00:00",
        adjustment_basis="auto_adjusted",
        requested_date="2026-07-25",
        effective_trading_date="2026-07-25",
        snapshot_id=f"snapshot:v2:{historical_digest}",
        snapshot_id_version="v2",
        pin_membership_digest=historical_digest,
        snapshot_manifest_json=historical_manifest,
    )
    historical_v1_id = "snapshot:" + "c" * 64
    historical_v1 = market_snapshot.AuthoritativeMarketSnapshot(
        symbol="SOL-USD",
        frame=frame,
        provider="yfinance",
        retrieved_at="2026-07-25T23:00:00+00:00",
        adjustment_basis="auto_adjusted",
        requested_date="2026-07-25",
        effective_trading_date="2026-07-25",
        snapshot_id=historical_v1_id,
        snapshot_id_version="v1",
    )

    assert historical_v2.snapshot_id == f"snapshot:v2:{historical_digest}"
    assert historical_v2.snapshot_manifest_json == historical_manifest
    assert historical_v1.snapshot_id == historical_v1_id
    assert historical_v1.snapshot_manifest_json is None


@pytest.mark.parametrize(
    "missing_path",
    (
        ("crypto_identity_binding_version",),
        ("asset_configuration", "contract_version"),
        ("asset_configuration", "registry", "digest"),
        ("asset_configuration", "capability_profile"),
        ("instrument", "identity_provenance"),
        ("provider", "dataset"),
        ("observations",),
        ("retrieval_cutoff",),
    ),
)
def test_corrected_crypto_strict_replay_rejects_incomplete_legacy_manifest(
    tmp_path: Path,
    missing_path: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    incomplete_manifest = json.loads(snapshot.manifest_json)
    _delete_manifest_value(incomplete_manifest, missing_path)

    def forbidden_current_resolution(*_args, **_kwargs):
        raise AssertionError("strict replay must not resolve current identity material")

    monkeypatch.setattr(
        asset_configuration_module,
        "resolve_run_asset_configuration",
        forbidden_current_resolution,
    )
    monkeypatch.setattr(
        identity_module,
        "resolve_authoritative_instrument_identity",
        forbidden_current_resolution,
    )

    with MarketHistoryStore.open(history_config) as store:
        historical_snapshot_id = _clone_crypto_pin_with_manifest(
            store,
            snapshot.snapshot_id,
            incomplete_manifest,
        )
        historical_rows_before = (
            store._connection.execute(
                "SELECT * FROM snapshot_pins WHERE snapshot_id = ?",
                (historical_snapshot_id,),
            ).fetchone(),
            tuple(
                store._connection.execute(
                    "SELECT * FROM snapshot_observation_pins WHERE snapshot_id = ? "
                    "ORDER BY ordinal",
                    (historical_snapshot_id,),
                )
            ),
            tuple(
                store._connection.execute(
                    "SELECT * FROM snapshot_factor_pins WHERE snapshot_id = ? "
                    "ORDER BY factor_revision_id",
                    (historical_snapshot_id,),
                )
            ),
        )

        with pytest.raises(LegacyCryptoSnapshotIdentityIncomplete) as raised:
            store.read_pinned_snapshot(
                historical_snapshot_id,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=dataset,
            )
        historical_rows_after = (
            store._connection.execute(
                "SELECT * FROM snapshot_pins WHERE snapshot_id = ?",
                (historical_snapshot_id,),
            ).fetchone(),
            tuple(
                store._connection.execute(
                    "SELECT * FROM snapshot_observation_pins WHERE snapshot_id = ? "
                    "ORDER BY ordinal",
                    (historical_snapshot_id,),
                )
            ),
            tuple(
                store._connection.execute(
                    "SELECT * FROM snapshot_factor_pins WHERE snapshot_id = ? "
                    "ORDER BY factor_revision_id",
                    (historical_snapshot_id,),
                )
            ),
        )

    assert raised.value.diagnostic_code == (
        "legacy_crypto_snapshot_identity_incomplete"
    )
    assert historical_rows_after == historical_rows_before


@pytest.mark.parametrize(
    "material_field",
    (
        "asset_configuration_version",
        "canonical_symbol",
        "reference_market",
        "incoherent_reference_market",
        "instrument_kind",
        "incoherent_instrument_kind",
        "currency",
        "identity_provenance",
        "registry_id",
        "registry_digest",
        "capability_profile",
        "incoherent_capability_profile_kind",
        "observation_calendar",
        "adjustment_basis",
    ),
)
def test_corrected_crypto_strict_replay_validates_configuration_before_membership(
    tmp_path: Path,
    material_field: str,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    mismatched_configuration = _mismatched_replay_configuration(
        asset_configuration,
        material_field,
    )
    manifest = json.loads(snapshot.manifest_json)

    with MarketHistoryStore.open(history_config) as store:
        observation_revision_id = manifest["observations"][0][
            "observation_revision_id"
        ]
        store._connection.execute(
            "UPDATE raw_market_observation_revisions SET close_value = '999' "
            "WHERE revision_id = ?",
            (observation_revision_id,),
        )

        with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
            store.read_pinned_snapshot(
                snapshot.snapshot_id,
                asset_configuration=mismatched_configuration,
                crypto_provider_dataset=dataset,
            )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    ("manifest_path", "replacement"),
    (
        (("provider", "provider_name"), "newer-provider"),
        (("provider", "upstream_service_id"), "upstream:newer-service"),
        (("provider", "provider_dataset_id"), "provider-dataset:newer"),
        (("provider", "dataset", "contract_version"), "2.0"),
        (("provider", "dataset", "dataset_family"), "mainland-equity"),
        (("provider", "dataset", "dataset_name"), "newer-ccc-bars"),
        (("provider", "dataset", "dataset_version"), "2.0"),
        (("provider", "dataset", "dataset_revision"), "revision-2"),
        (("provider", "dataset", "reference_market"), "XSHG"),
        (("provider", "dataset", "tags"), ["crypto", "ccc", "mainland"]),
    ),
)
def test_corrected_crypto_strict_replay_validates_recorded_dataset_before_membership(
    tmp_path: Path,
    manifest_path: tuple[str, ...],
    replacement: object,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    mismatched_manifest = json.loads(snapshot.manifest_json)
    _replace_manifest_value(mismatched_manifest, manifest_path, replacement)

    with MarketHistoryStore.open(history_config) as store:
        historical_snapshot_id = _clone_crypto_pin_with_manifest(
            store,
            snapshot.snapshot_id,
            mismatched_manifest,
        )
        store._connection.execute(
            "DELETE FROM snapshot_observation_pins WHERE snapshot_id = ?",
            (historical_snapshot_id,),
        )

        with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
            store.read_pinned_snapshot(
                historical_snapshot_id,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=dataset,
            )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    "dataset_field",
    (
        "provider_dataset_id",
        "provider_name",
        "upstream_service_id",
        "contract_version",
        "dataset_family",
        "dataset_name",
        "dataset_version",
        "dataset_revision",
        "reference_market",
        "tags",
    ),
)
def test_corrected_crypto_strict_replay_validates_requested_dataset_before_membership(
    tmp_path: Path,
    dataset_field: str,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    mismatched_dataset = _mismatched_replay_dataset(dataset, dataset_field)
    manifest = json.loads(snapshot.manifest_json)

    with MarketHistoryStore.open(history_config) as store:
        observation_revision_id = manifest["observations"][0][
            "observation_revision_id"
        ]
        store._connection.execute(
            "UPDATE raw_market_observation_revisions SET close_value = '999' "
            "WHERE revision_id = ?",
            (observation_revision_id,),
        )

        with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
            store.read_pinned_snapshot(
                snapshot.snapshot_id,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=mismatched_dataset,
            )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    ("manifest_path", "replacement"),
    (
        (("crypto_identity_binding_version",), "2.0"),
        (("manifest_version",), "3.0"),
        (("snapshot_kind",), "current_only_live_artifact"),
        (("adjustment_basis",), "qfq"),
        (("authoritative_status", "identity"), "unknown"),
        (("authoritative_status", "value"), "suspended"),
        (("bundle_revision_id",), "history-bundle:newer"),
        (("bundle_observed_at",), "2026-07-26T00:00:00+00:00"),
        (("calendar_revision_id",), "mainland-session-calendar:v1"),
        (("derivation_version",), "crypto-auto-adjusted-v2"),
        (("effective_trading_date",), "2026-07-24"),
        (("factors",), ["adjustment-factor:newer"]),
        (("frame", "rows"), 3),
        (("frame", "sha256"), "b" * 64),
        (("normalization_version",), "normalized-frame-csv-v2"),
        (("observations",), []),
        (("provenance_class",), "retrospective_backfill"),
        (("requested_date",), "2026-07-26"),
        (("retrieval_cutoff",), "2026-07-26T00:00:00+00:00"),
    ),
)
def test_corrected_crypto_strict_replay_validates_recorded_material_before_reconstruction(
    tmp_path: Path,
    manifest_path: tuple[str, ...],
    replacement: object,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    mismatched_manifest = json.loads(snapshot.manifest_json)
    _replace_manifest_value(mismatched_manifest, manifest_path, replacement)

    with MarketHistoryStore.open(history_config) as store:
        historical_snapshot_id = _clone_crypto_pin_with_manifest(
            store,
            snapshot.snapshot_id,
            mismatched_manifest,
        )
        store._connection.execute(
            "DELETE FROM snapshot_observation_pins WHERE snapshot_id = ?",
            (historical_snapshot_id,),
        )

        with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
            store.read_pinned_snapshot(
                historical_snapshot_id,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=dataset,
            )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    ("parent_field", "manifest_path", "replacement"),
    (
        ("adjustment_basis", ("adjustment_basis",), "qfq"),
        ("derivation_version", ("derivation_version",), "crypto-v2"),
        (
            "normalization_version",
            ("normalization_version",),
            "normalized-frame-csv-v2",
        ),
        ("identity_version", None, "v1"),
    ),
)
def test_corrected_crypto_strict_replay_rejects_joint_parent_manifest_mismatch(
    tmp_path: Path,
    parent_field: str,
    manifest_path: tuple[str, ...] | None,
    replacement: str,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    manifest = json.loads(snapshot.manifest_json)
    if manifest_path is not None:
        _replace_manifest_value(manifest, manifest_path, replacement)

    with MarketHistoryStore.open(history_config) as store:
        historical_snapshot_id = (
            _clone_crypto_pin_with_manifest(
                store,
                snapshot.snapshot_id,
                manifest,
            )
            if manifest_path is not None
            else snapshot.snapshot_id
        )
        store._connection.execute(
            f"UPDATE snapshot_pins SET {parent_field} = ? WHERE snapshot_id = ?",
            (replacement, historical_snapshot_id),
        )
        observation_revision_id = manifest["observations"][0][
            "observation_revision_id"
        ]
        store._connection.execute(
            "UPDATE raw_market_observation_revisions SET close_value = '999' "
            "WHERE revision_id = ?",
            (observation_revision_id,),
        )

        with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
            store.read_pinned_snapshot(
                historical_snapshot_id,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=dataset,
            )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    "stored_field",
    (
        "canonical_symbol",
        "reference_market",
        "instrument_kind",
        "currency",
        "identity_revision",
        "provider_name",
        "upstream_service_id",
        "dataset_name",
        "adjustment_methodology",
    ),
)
def test_corrected_crypto_strict_replay_validates_stored_provider_metadata(
    tmp_path: Path,
    stored_field: str,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    manifest = json.loads(snapshot.manifest_json)
    instrument_id = manifest["instrument"]["instrument_id"]
    provider_dataset_id = manifest["provider"]["provider_dataset_id"]
    replacements = {
        "canonical_symbol": "ETH-USD",
        "reference_market": "XSHG",
        "instrument_kind": "unknown",
        "currency": "EUR",
        "identity_revision": "crypto-registry:newer",
        "provider_name": "newer-provider",
        "dataset_name": "newer-ccc-bars",
        "adjustment_methodology": "qfq",
    }

    with MarketHistoryStore.open(history_config) as store:
        if stored_field == "upstream_service_id":
            store._connection.execute(
                "INSERT INTO upstream_services "
                "(upstream_service_id, service_name, account_scope, created_at) "
                "VALUES ('upstream:newer', 'Newer service', '', ?)",
                ("2026-07-26T00:00:00+00:00",),
            )
            store._connection.execute(
                "UPDATE provider_datasets SET upstream_service_id = 'upstream:newer' "
                "WHERE provider_dataset_id = ?",
                (provider_dataset_id,),
            )
        elif stored_field in {
            "provider_name",
            "dataset_name",
            "adjustment_methodology",
        }:
            store._connection.execute(
                f"UPDATE provider_datasets SET {stored_field} = ? "
                "WHERE provider_dataset_id = ?",
                (replacements[stored_field], provider_dataset_id),
            )
        else:
            store._connection.execute(
                f"UPDATE instruments SET {stored_field} = ? WHERE instrument_id = ?",
                (replacements[stored_field], instrument_id),
            )
        observation_revision_id = manifest["observations"][0][
            "observation_revision_id"
        ]
        store._connection.execute(
            "UPDATE raw_market_observation_revisions SET close_value = '999' "
            "WHERE revision_id = ?",
            (observation_revision_id,),
        )

        with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
            store.read_pinned_snapshot(
                snapshot.snapshot_id,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=dataset,
            )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


def test_corrected_crypto_strict_replay_is_exact_immutable_and_provider_free(
    tmp_path: Path,
    monkeypatch,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    provider_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def unexpected_provider_call(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("strict replay must not call a provider")

    for provider_name, provider in tuple(market_snapshot.SNAPSHOT_PROVIDERS.items()):
        monkeypatch.setitem(
            market_snapshot.SNAPSHOT_PROVIDERS,
            provider_name,
            replace(
                provider,
                load=unexpected_provider_call,
                history_load=unexpected_provider_call,
            ),
        )

    payload_bytes_before = {
        path.relative_to(history_config.payload_root): path.read_bytes()
        for path in history_config.payload_root.rglob("*")
        if path.is_file()
    }
    historical_files = {
        tmp_path / "historical-report.md": b"unchanged historical report\n",
        tmp_path / "historical-checkpoint.bin": b"unchanged checkpoint\x00",
        tmp_path / "historical-artifact.bin": b"unchanged artifact\x00",
    }
    for path, content in historical_files.items():
        path.write_bytes(content)
    with MarketHistoryStore.open(history_config) as store:
        pin_rows_before = (
            store._connection.execute(
                "SELECT * FROM snapshot_pins WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone(),
            tuple(
                store._connection.execute(
                    "SELECT * FROM snapshot_observation_pins WHERE snapshot_id = ? "
                    "ORDER BY ordinal",
                    (snapshot.snapshot_id,),
                )
            ),
            tuple(
                store._connection.execute(
                    "SELECT * FROM snapshot_factor_pins WHERE snapshot_id = ? "
                    "ORDER BY factor_revision_id",
                    (snapshot.snapshot_id,),
                )
            ),
        )
        replayed = store.read_pinned_snapshot(
            snapshot.snapshot_id,
            asset_configuration=asset_configuration,
            crypto_provider_dataset=dataset,
        )
        pin_rows_after = (
            store._connection.execute(
                "SELECT * FROM snapshot_pins WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone(),
            tuple(
                store._connection.execute(
                    "SELECT * FROM snapshot_observation_pins WHERE snapshot_id = ? "
                    "ORDER BY ordinal",
                    (snapshot.snapshot_id,),
                )
            ),
            tuple(
                store._connection.execute(
                    "SELECT * FROM snapshot_factor_pins WHERE snapshot_id = ? "
                    "ORDER BY factor_revision_id",
                    (snapshot.snapshot_id,),
                )
            ),
        )
    payload_bytes_after = {
        path.relative_to(history_config.payload_root): path.read_bytes()
        for path in history_config.payload_root.rglob("*")
        if path.is_file()
    }

    assert provider_calls == []
    assert replayed.snapshot_id == snapshot.snapshot_id
    assert replayed.manifest_json == snapshot.manifest_json
    assert replayed.pin_membership_digest == snapshot.pin_membership_digest
    assert replayed.observation_revision_ids == snapshot.observation_revision_ids
    assert replayed.trading_status_revision_ids == snapshot.trading_status_revision_ids
    assert replayed.factor_revision_ids == snapshot.factor_revision_ids
    assert replayed.calendar_revision_id == snapshot.calendar_revision_id
    assert pin_rows_after == pin_rows_before
    assert payload_bytes_after == payload_bytes_before
    assert {path: path.read_bytes() for path in historical_files} == historical_files


def test_valid_legacy_crypto_v1_pin_remains_readable(tmp_path: Path) -> None:
    history_config, _asset_configuration, _dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )

    with MarketHistoryStore.open(history_config) as store:
        legacy_snapshot_id = _clone_crypto_v1_pin(store, snapshot)
        replayed = store.read_pinned_snapshot(legacy_snapshot_id)

    assert replayed.snapshot_id == legacy_snapshot_id
    assert replayed.snapshot_id_version == "v1"
    assert replayed.manifest_json is None
    assert replayed.frame.equals(snapshot.frame)


@pytest.mark.parametrize(
    "corruption",
    (
        "observation",
        "status",
        "factor",
        "calendar",
        "payload_missing",
        "payload_corrupt",
        "parent",
    ),
)
def test_corrected_crypto_strict_replay_fails_closed_for_missing_exact_material(
    tmp_path: Path,
    corruption: str,
) -> None:
    history_config, asset_configuration, dataset, snapshot = (
        _publish_corrected_crypto_replay_fixture(tmp_path)
    )
    manifest = json.loads(snapshot.manifest_json)
    observation_id = manifest["observations"][0]["observation_revision_id"]
    status_id = manifest["observations"][0]["trading_status_revision_id"]
    factor_id = manifest["factors"][0]
    calendar_id = manifest["calendar_revision_id"]
    bundle_id = manifest["bundle_revision_id"]

    with MarketHistoryStore.open(history_config) as store:
        if corruption in {"payload_missing", "payload_corrupt"}:
            relative_path = store._connection.execute(
                "SELECT p.relative_path FROM payload_artifacts AS p "
                "JOIN raw_market_observation_revisions AS o "
                "ON o.payload_digest = p.digest WHERE o.revision_id = ?",
                (observation_id,),
            ).fetchone()[0]
            payload_path = history_config.payload_root / relative_path
            if corruption == "payload_missing":
                payload_path.unlink()
            else:
                payload_path.write_bytes(b"corrupt historical payload")
        else:
            store._connection.execute("PRAGMA foreign_keys = OFF")
            if corruption == "observation":
                store._connection.execute(
                    "DELETE FROM raw_market_observation_revisions WHERE revision_id = ?",
                    (observation_id,),
                )
            elif corruption == "status":
                store._connection.execute(
                    "DELETE FROM trading_status_revisions WHERE revision_id = ?",
                    (status_id,),
                )
            elif corruption == "factor":
                store._connection.execute(
                    "DELETE FROM adjustment_factor_revisions WHERE revision_id = ?",
                    (factor_id,),
                )
            elif corruption == "calendar":
                store._connection.execute(
                    "DELETE FROM market_session_calendars "
                    "WHERE calendar_revision_id = ?",
                    (calendar_id,),
                )
            elif corruption == "parent":
                store._connection.execute(
                    "DELETE FROM history_bundle_revisions WHERE bundle_revision_id = ?",
                    (bundle_id,),
                )

        with pytest.raises(
            (CryptoSnapshotIdentityMismatch, SnapshotPinCorruptionError)
        ):
            store.read_pinned_snapshot(
                snapshot.snapshot_id,
                asset_configuration=asset_configuration,
                crypto_provider_dataset=dataset,
            )


def test_crypto_snapshot_v2_id_changes_for_each_material_identity_or_dataset_field():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    baseline = _live_crypto_identity(asset_configuration).snapshot_id
    provenance = asset_configuration.instrument_identity.provenance
    assert provenance is not None
    changed_digest = "a" * 64
    registry_identity = asset_configuration.instrument_identity.model_copy(
        update={
            "provenance": provenance.model_copy(
                update={"artifact_sha256": changed_digest}
            )
        }
    )
    registry_configuration = asset_configuration.model_copy(
        update={
            "registry_digest": changed_digest,
            "instrument_identity": registry_identity,
        }
    )
    symbol_configuration = asset_configuration.model_copy(
        update={
            "instrument_identity": asset_configuration.instrument_identity.model_copy(
                update={"symbol": "ETH-USD"}
            )
        }
    )
    provenance_configuration = asset_configuration.model_copy(
        update={
            "instrument_identity": asset_configuration.instrument_identity.model_copy(
                update={
                    "provenance": provenance.model_copy(
                        update={"retrieved_at": "2026-07-26T00:00:00+00:00"}
                    )
                }
            )
        }
    )
    default_upstream_service_id, _ = upstream_service_identity_for_provider(
        "yfinance"
    )
    material_variants = {
        "registry_id": _live_crypto_identity(
            asset_configuration.model_copy(update={"registry_id": "crypto-registry-v2"})
        ),
        "registry_digest": _live_crypto_identity(registry_configuration),
        "canonical_identity": _live_crypto_identity(symbol_configuration),
        "identity_provenance": _live_crypto_identity(provenance_configuration),
        "provider_name": _live_crypto_identity(
            asset_configuration,
            dataset=_crypto_dataset_descriptor(
                provider_name="fixture-crypto-provider",
                upstream_service_id=default_upstream_service_id,
            ),
        ),
        "upstream_service": _live_crypto_identity(
            asset_configuration,
            dataset=_crypto_dataset_descriptor(
                upstream_service_id="upstream-service=sha256:" + "5" * 64,
            ),
        ),
        "dataset_name": _live_crypto_identity(
            asset_configuration,
            dataset=_crypto_dataset_descriptor(dataset_name="ccc-daily-bars"),
        ),
        "dataset_version": _live_crypto_identity(
            asset_configuration,
            dataset=_crypto_dataset_descriptor(dataset_version="2.0"),
        ),
        "dataset_revision": _live_crypto_identity(
            asset_configuration,
            dataset=_crypto_dataset_descriptor(dataset_revision="revision-2"),
        ),
        "dataset_tags": _live_crypto_identity(
            asset_configuration,
            dataset=_crypto_dataset_descriptor(
                tags=("crypto", "ccc", "daily", "ohlcv", "adjusted")
            ),
        ),
        "requested_date": _live_crypto_identity(
            asset_configuration,
            requested_date="2026-07-26",
        ),
        "effective_date": _live_crypto_identity(
            asset_configuration,
            effective_trading_date="2026-07-24",
        ),
        "frame_digest": _live_crypto_identity(
            asset_configuration,
            frame_digest="6" * 64,
        ),
        "row_count": _live_crypto_identity(asset_configuration, history_rows=22),
        "derivation_version": _live_crypto_identity(
            asset_configuration,
            derivation_version="provider-current-frame-v2",
        ),
        "normalization_version": _live_crypto_identity(
            asset_configuration,
            normalization_version="normalized-frame-csv-v2",
        ),
        "artifact_identity": _live_crypto_identity(
            asset_configuration,
            accepted_artifact_identity="artifact:sha256:" + "7" * 64,
        ),
        "status_identity": _live_crypto_identity(
            asset_configuration,
            authoritative_status_identity="crypto-status=sha256:" + "8" * 64,
        ),
        "status_value": _live_crypto_identity(
            asset_configuration,
            authoritative_status_value="available",
        ),
        "provenance_class": _live_crypto_identity(
            asset_configuration,
            provenance_class="stored",
        ),
    }

    for field, variant in material_variants.items():
        assert variant.snapshot_id != baseline, field


def test_crypto_snapshot_v2_id_is_invariant_to_display_metadata_and_ordering():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    display_configuration = asset_configuration.model_copy(
        update={
            "instrument_identity": asset_configuration.instrument_identity.model_copy(
                update={"display_name": "Localized Solana Display Name"}
            )
        }
    )
    baseline = _live_crypto_identity(asset_configuration)
    reordered = _live_crypto_identity(
        display_configuration,
        dataset=_crypto_dataset_descriptor(
            tags=("ohlcv", "daily", "crypto", "ccc")
        ),
    )

    assert reordered.snapshot_id == baseline.snapshot_id
    assert reordered.manifest_json == baseline.manifest_json

    frame = pd.DataFrame(
        {
            "Date": pd.date_range(start="2026-07-05", periods=21, freq="D"),
            "Open": [100.0] * 21,
            "High": [100.0] * 21,
            "Low": [100.0] * 21,
            "Close": [100.0] * 21,
            "Volume": [100.0] * 21,
        }
    )
    raw_text = frame.to_csv(index=False)
    artifact_digest = sha256(raw_text.encode("utf-8")).hexdigest()
    first_artifact = SourceArtifact(
        artifact_sha256=artifact_digest,
        source_ref="acq.v1:crypto-frame",
        tool_call_id="tool-call-1",
        tool_name="fixture-market-tool",
        raw_text=raw_text,
    )
    second_artifact = first_artifact.model_copy(
        update={"tool_call_id": "tool-call-2"}
    )
    dataset = _crypto_dataset_descriptor()
    snapshot_values = {
        "symbol": "SOL-USD",
        "frame": frame,
        "provider": "yfinance",
        "retrieved_at": "2026-07-25T23:00:00+00:00",
        "adjustment_basis": "auto_adjusted",
        "requested_date": "2026-07-25",
        "effective_trading_date": "2026-07-25",
        "asset_configuration": asset_configuration,
        "crypto_provider_dataset": dataset,
    }
    first = market_snapshot.AuthoritativeMarketSnapshot(
        **snapshot_values,
        source_artifact=first_artifact,
    )
    second = market_snapshot.AuthoritativeMarketSnapshot(
        **{
            **snapshot_values,
            "retrieved_at": "2026-07-26T00:00:00+00:00",
            "asset_configuration": display_configuration,
        },
        source_artifact=second_artifact,
    )

    assert first.snapshot_id == second.snapshot_id
    assert first.snapshot_manifest_json == second.snapshot_manifest_json


def test_new_crypto_snapshot_rejects_a_frame_digest_that_does_not_match_its_frame():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    frame = pd.DataFrame(
        {
            "Date": pd.date_range(start="2026-07-24", periods=2, freq="D"),
            "Open": [100.0, 101.0],
            "High": [100.0, 101.0],
            "Low": [100.0, 101.0],
            "Close": [100.0, 101.0],
            "Volume": [100.0, 100.0],
        }
    )

    with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
        market_snapshot.AuthoritativeMarketSnapshot(
            symbol="SOL-USD",
            frame=frame,
            provider="yfinance",
            retrieved_at="2026-07-25T23:00:00+00:00",
            adjustment_basis="auto_adjusted",
            requested_date="2026-07-25",
            effective_trading_date="2026-07-25",
            frame_sha256="0" * 64,
            asset_configuration=asset_configuration,
            crypto_provider_dataset=_crypto_dataset_descriptor(),
        )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"


def test_crypto_durable_membership_is_sensitive_and_canonically_ordered():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    dataset = _crypto_dataset_descriptor()
    common = {
        "instrument_id": crypto_snapshot_instrument_id(asset_configuration),
        "canonical_symbol": "SOL-USD",
        "identity_revision": crypto_snapshot_identity_revision(asset_configuration),
        "reference_market": "CCC",
        "instrument_kind": "crypto",
        "currency": "USD",
        "provider_dataset_id": dataset.provider_dataset_id,
        "provider_name": dataset.provider_name,
        "upstream_service_id": dataset.upstream_service_id,
        "requested_date": "2026-07-25",
        "effective_trading_date": "2026-07-25",
        "adjustment_basis": "auto_adjusted",
        "frame_digest": "9" * 64,
        "history_rows": 2,
        "derivation_version": "crypto-auto-adjusted-v1",
        "normalization_version": "normalized-frame-csv-v1",
        "observation_membership": (
            ("2026-07-24", "observation:1", "status:1", "traded"),
            ("2026-07-25", "observation:2", "status:2", "traded"),
        ),
        "factor_revision_ids": ("factor:1", "factor:2"),
        "calendar_revision_id": "crypto-consecutive-daily-calendar:v1",
        "provenance_class": "observed_point_in_time",
        "bundle_revision_id": "crypto-bundle:1",
        "retrieval_cutoff": "2026-07-25T23:59:59+00:00",
        "bundle_observed_at": "2026-07-25T23:00:00+00:00",
        "asset_configuration": asset_configuration,
        "crypto_provider_dataset": dataset,
    }
    baseline = snapshot_v2_identity(**common)
    permuted = snapshot_v2_identity(
        **{
            **common,
            "observation_membership": tuple(
                reversed(common["observation_membership"])
            ),
            "factor_revision_ids": tuple(reversed(common["factor_revision_ids"])),
        }
    )
    material_variants = (
        {
            "observation_membership": (
                ("2026-07-24", "observation:changed", "status:1", "traded"),
                common["observation_membership"][1],
            )
        },
        {
            "observation_membership": (
                ("2026-07-24", "observation:1", "status:changed", "traded"),
                common["observation_membership"][1],
            )
        },
        {"factor_revision_ids": ("factor:1", "factor:changed")},
        {"calendar_revision_id": "crypto-consecutive-daily-calendar:v2"},
        {"bundle_revision_id": "crypto-bundle:2"},
        {"retrieval_cutoff": "2026-07-25T23:30:00+00:00"},
        {"bundle_observed_at": "2026-07-25T22:00:00+00:00"},
    )

    assert permuted.snapshot_id == baseline.snapshot_id
    for update in material_variants:
        assert snapshot_v2_identity(**{**common, **update}).snapshot_id != (
            baseline.snapshot_id
        )


def test_mainland_asset_configuration_preserves_equity_semantics():
    project_root = Path(__file__).parents[1]
    registry_path = project_root / "config" / "instrument_identity_registry.json"
    registry_digest = registry_path.with_suffix(".sha256").read_text(
        encoding="utf-8"
    ).split()[0]
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "instrument_identity_registry_path": str(registry_path),
            "instrument_identity_registry_sha256": registry_digest,
        }
    )

    asset_configuration = resolve_run_asset_configuration(
        "601328.SS",
        config=config,
    )

    assert asset_configuration.instrument_identity.symbol == "601328.SS"
    assert asset_configuration.reference_market == "XSHG"
    assert asset_configuration.instrument_kind is InstrumentKind.EQUITY
    assert asset_configuration.capability_profile.profile_id == "equity.v1"
    assert asset_configuration.observation_calendar_kind is (
        ObservationCalendarKind.MARKET_SESSIONS
    )
    assert asset_configuration.adjustment_basis == "qfq"
    assert asset_configuration.calculation_id == "market.close_return_20d.qfq"
    assert asset_configuration.horizon == DecisionHorizon(
        count=20,
        unit=HorizonUnit.TRADING_DAYS,
    )


def test_crypto_asset_configuration_drives_graph_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = MagicMock()
    client = MagicMock()
    client.get_llm.return_value = llm
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        lambda **_kwargs: client,
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
        }
    )

    graph = trading_graph_module.TradingAgentsGraph(
        selected_analysts=("market", "social", "news", "fundamentals"),
        config=config,
        asset_configuration=asset_configuration,
    )

    assert graph.asset_configuration is asset_configuration
    assert graph.instrument_kind is InstrumentKind.CRYPTO
    assert graph.capability_profile.profile_id == "crypto.v1"
    assert graph.selected_analysts == ("market", "social", "news")
    assert graph.decision_horizon == asset_configuration.horizon
    assert "Fundamentals Analyst" not in graph.workflow.nodes
    assert "tools_fundamentals" not in graph.workflow.nodes


def test_evidence_uses_pre_resolved_asset_without_reopening_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    asset_configuration = resolve_run_asset_configuration("SOL-USD", config=config)
    dates = pd.date_range(start="2026-07-05", periods=21, freq="D")
    closes = [100.0] * 20 + [110.0]
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [100.0] * 21,
        }
    )

    def forbidden_registry_resolution(*_args, **_kwargs):
        raise AssertionError("evidence must use the pre-resolved run asset")

    monkeypatch.setattr(
        identity_module,
        "resolve_authoritative_instrument_identity",
        forbidden_registry_resolution,
    )
    with (
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {"yfinance": SnapshotProvider(lambda *_args: frame, "auto_adjusted")},
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence(
            "SOL-USD",
            "2026-07-25",
            asset_configuration=asset_configuration,
        )

    assert evidence.instrument_identity == asset_configuration.instrument_identity
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.history_rows == 21
    assert evidence.market_snapshot.snapshot_manifest_json is not None
    manifest = json.loads(evidence.market_snapshot.snapshot_manifest_json)
    assert manifest["asset_configuration"] == {
        "capability_profile": {
            "contract_version": "1.0",
            "profile_id": "crypto.v1",
            "profile_version": "1.0",
        },
        "contract_version": "1.0",
        "observation_calendar_kind": "consecutive_daily",
        "registry": {
            "digest": asset_configuration.registry_digest,
            "registry_id": "crypto-ccc-identity-registry-v1",
        },
    }
    assert manifest["instrument"]["canonical_symbol"] == "SOL-USD"
    assert manifest["instrument"]["reference_market"] == "CCC"
    assert manifest["instrument"]["instrument_kind"] == "crypto"
    assert manifest["instrument"]["currency"] == "USD"
    assert asset_configuration.instrument_identity.provenance is not None
    assert manifest["instrument"]["identity_provenance"] == (
        asset_configuration.instrument_identity.provenance.model_dump(mode="json")
    )
    assert manifest["provider"]["dataset"]["dataset_family"] == "crypto"
    assert manifest["provider"]["dataset"]["dataset_name"] == (
        "yahoo-ccc-daily-ohlcv"
    )
    assert manifest["provider"]["dataset"]["tags"] == [
        "ccc",
        "crypto",
        "daily",
        "ohlcv",
    ]
    assert manifest["authoritative_status"]["value"] == "not_applicable"
    assert "unknown" not in evidence.market_snapshot.snapshot_manifest_json.casefold()
    assert "unresolved" not in evidence.market_snapshot.snapshot_manifest_json.casefold()
    assert "mainland" not in evidence.market_snapshot.snapshot_manifest_json.casefold()
    assert evidence.source_facts[0].calculation_lineage is not None
    assert evidence.source_facts[0].calculation_lineage.calculation_id == (
        asset_configuration.calculation_id
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "symbol",
        "asset_contract_version",
        "identity_provenance",
        "registry_id",
        "registry_digest",
        "reference_market",
        "instrument_kind",
        "currency",
        "capability_profile",
        "capability_profile_version",
        "calendar",
        "adjustment_basis",
    ),
)
def test_invalid_crypto_run_asset_fails_before_provider_publication(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_config(copy.deepcopy(DEFAULT_CONFIG))
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    identity = asset_configuration.instrument_identity
    if mutation == "symbol":
        asset_configuration = asset_configuration.model_copy(
            update={
                "instrument_identity": identity.model_copy(
                    update={"symbol": "BTC-USD"}
                )
            }
        )
    elif mutation == "asset_contract_version":
        asset_configuration = asset_configuration.model_copy(
            update={"asset_configuration_version": "2.0"}
        )
    elif mutation == "identity_provenance":
        asset_configuration = asset_configuration.model_copy(
            update={
                "instrument_identity": identity.model_copy(
                    update={"provenance": None}
                )
            }
        )
    elif mutation == "registry_id":
        asset_configuration = asset_configuration.model_copy(
            update={"registry_id": ""}
        )
    elif mutation == "registry_digest":
        asset_configuration = asset_configuration.model_copy(
            update={"registry_digest": "a" * 64}
        )
    elif mutation == "reference_market":
        asset_configuration = asset_configuration.model_copy(
            update={"reference_market": "XSHG"}
        )
    elif mutation == "instrument_kind":
        asset_configuration = asset_configuration.model_copy(
            update={"instrument_kind": InstrumentKind.EQUITY}
        )
    elif mutation == "currency":
        asset_configuration = asset_configuration.model_copy(
            update={
                "instrument_identity": identity.model_copy(
                    update={"currency": "CNY"}
                )
            }
        )
    elif mutation == "capability_profile":
        asset_configuration = asset_configuration.model_copy(
            update={
                "capability_profile": asset_configuration.capability_profile.model_copy(
                    update={"profile_id": "equity.v1"}
                )
            }
        )
    elif mutation == "capability_profile_version":
        asset_configuration = asset_configuration.model_copy(
            update={
                "capability_profile": asset_configuration.capability_profile.model_copy(
                    update={"contract_version": "2.0"}
                )
            }
        )
    elif mutation == "calendar":
        asset_configuration = asset_configuration.model_copy(
            update={
                "observation_calendar_kind": ObservationCalendarKind.MARKET_SESSIONS
            }
        )
    elif mutation == "adjustment_basis":
        asset_configuration = asset_configuration.model_copy(
            update={"adjustment_basis": "qfq"}
        )
    provider_calls = 0

    def provider(*_args):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("invalid crypto identity must fail before provider I/O")

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"yfinance": SnapshotProvider(provider, "auto_adjusted")},
    )

    with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
        market_snapshot.get_authoritative_market_snapshot(
            "SOL-USD",
            "2026-07-05",
            "2026-07-25",
            asset_configuration=asset_configuration,
        )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"
    assert provider_calls == 0


def test_missing_crypto_run_asset_fails_before_provider_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config = copy.deepcopy(DEFAULT_CONFIG)
    runtime_config["crypto_identity_registry_path"] = None
    runtime_config["crypto_identity_registry_sha256"] = None
    set_config(runtime_config)
    provider_calls = 0

    def provider(*_args):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("missing crypto identity must fail before provider I/O")

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"yfinance": SnapshotProvider(provider, "auto_adjusted")},
    )

    with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
        market_snapshot.get_authoritative_market_snapshot(
            "SOL-USD",
            "2026-07-05",
            "2026-07-25",
        )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"
    assert provider_calls == 0


@pytest.mark.parametrize(
    "contradiction",
    (
        "mainland_dataset",
        "non_ccc_dataset",
        "dataset_id",
        "adjustment_basis",
    ),
)
def test_contradictory_crypto_dataset_fails_before_provider_publication(
    contradiction: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_config(copy.deepcopy(DEFAULT_CONFIG))
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    upstream_service_id, _ = upstream_service_identity_for_provider("yfinance")
    dataset = build_crypto_provider_dataset_descriptor(
        provider_name="yfinance",
        upstream_service_id=upstream_service_id,
        dataset_family=(
            "mainland-equity" if contradiction == "mainland_dataset" else "crypto"
        ),
        dataset_name="yahoo-ccc-daily-ohlcv",
        dataset_version="1.0",
        dataset_revision="yfinance-download-auto-adjusted-v1",
        tags=("crypto", "ccc", "daily", "ohlcv"),
        reference_market=("XNAS" if contradiction == "non_ccc_dataset" else "CCC"),
    )
    if contradiction == "dataset_id":
        dataset = replace(dataset, provider_dataset_id="provider-dataset:contradiction")
    provider_calls = 0

    def provider(*_args):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("contradictory dataset must fail before provider I/O")

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {
            "yfinance": SnapshotProvider(
                provider,
                "qfq" if contradiction == "adjustment_basis" else "auto_adjusted",
                crypto_dataset=dataset,
            )
        },
    )

    with pytest.raises(CryptoSnapshotIdentityMismatch) as raised:
        market_snapshot.get_authoritative_market_snapshot(
            "SOL-USD",
            "2026-07-05",
            "2026-07-25",
            asset_configuration=asset_configuration,
        )

    assert raised.value.diagnostic_code == "crypto_snapshot_identity_mismatch"
    assert provider_calls == 0


def test_run_scoped_crypto_snapshot_cache_rejects_a_contradictory_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_config(copy.deepcopy(DEFAULT_CONFIG))
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    contradictory = asset_configuration.model_copy(
        update={"registry_digest": "b" * 64}
    )
    frame = pd.DataFrame(
        {
            "Date": pd.date_range(start="2026-07-05", periods=21, freq="D"),
            "Open": [100.0] * 21,
            "High": [100.0] * 21,
            "Low": [100.0] * 21,
            "Close": [100.0] * 21,
            "Volume": [100.0] * 21,
        }
    )
    provider_calls = 0

    def provider(*_args):
        nonlocal provider_calls
        provider_calls += 1
        return frame

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"yfinance": SnapshotProvider(provider, "auto_adjusted")},
    )

    with authoritative_snapshot_run(asset_configuration=asset_configuration):
        market_snapshot.get_authoritative_market_snapshot(
            "SOL-USD",
            "2026-07-05",
            "2026-07-25",
        )
        with pytest.raises(CryptoSnapshotIdentityMismatch):
            market_snapshot.get_authoritative_market_snapshot(
                "SOL-USD",
                "2026-07-05",
                "2026-07-25",
                asset_configuration=contradictory,
            )

    assert provider_calls == 1


def test_initially_unbound_snapshot_run_binds_first_crypto_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    second_configuration = first_configuration.model_copy(
        update={"registry_id": "crypto-ccc-identity-registry-v2"}
    )
    frame = pd.DataFrame(
        {
            "Date": pd.date_range(start="2026-07-05", periods=21, freq="D"),
            "Open": [100.0] * 21,
            "High": [100.0] * 21,
            "Low": [100.0] * 21,
            "Close": [100.0] * 21,
            "Volume": [100.0] * 21,
        }
    )
    provider_calls = 0

    def provider(*_args):
        nonlocal provider_calls
        provider_calls += 1
        return frame

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"yfinance": SnapshotProvider(provider, "auto_adjusted")},
    )

    with authoritative_snapshot_run() as active:
        first = market_snapshot.get_authoritative_market_snapshot(
            "SOL-USD",
            "2026-07-05",
            "2026-07-25",
            asset_configuration=first_configuration,
        )
        with pytest.raises(CryptoSnapshotIdentityMismatch):
            market_snapshot.get_authoritative_market_snapshot(
                "SOL-USD",
                "2026-07-05",
                "2026-07-25",
                asset_configuration=second_configuration,
            )

    assert active.asset_configuration is first_configuration
    assert first.asset_configuration is first_configuration
    assert provider_calls == 1


def test_checkpointable_initial_state_contains_asset_configuration_before_stream():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    state = Propagator().create_initial_state(
        "SOL-USD",
        "2026-07-25",
        asset_type="crypto",
        asset_configuration=asset_configuration,
    )

    assert state["asset_configuration"] == asset_configuration.model_dump(mode="json")
    assert state["asset_configuration"]["asset_configuration_signature"] == (
        asset_configuration.asset_configuration_signature
    )


def test_checkpoint_signature_commits_every_crypto_asset_semantic():
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    graph = object.__new__(trading_graph_module.TradingAgentsGraph)
    graph.selected_analysts = ("market", "social", "news")
    graph.config = {
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "evidence_gate_mode": "enforce",
    }
    graph.decision_policy = type(
        "Policy",
        (),
        {"registry_digest": "registry:production-rules"},
    )()
    graph.decision_horizon = asset_configuration.horizon
    graph.asset_configuration = asset_configuration
    baseline = graph._run_signature("crypto")

    variants = (
        asset_configuration.model_copy(
            update={
                "instrument_identity": asset_configuration.instrument_identity.model_copy(
                    update={"symbol": "BTC-USD"}
                )
            }
        ),
        asset_configuration.model_copy(update={"registry_id": "crypto-registry-v2"}),
        asset_configuration.model_copy(update={"registry_digest": "0" * 64}),
        asset_configuration.model_copy(update={"reference_market": "OTHER"}),
        asset_configuration.model_copy(
            update={"observation_calendar_kind": ObservationCalendarKind.MARKET_SESSIONS}
        ),
        asset_configuration.model_copy(
            update={
                "horizon": DecisionHorizon(count=21, unit=HorizonUnit.CALENDAR_DAYS)
            }
        ),
        asset_configuration.model_copy(
            update={"capability_profile": capability_profile_for(InstrumentKind.EQUITY)}
        ),
    )

    for variant in variants:
        graph.asset_configuration = variant
        assert graph._run_signature("crypto") != baseline

    graph.asset_configuration = None
    assert graph._run_signature("crypto") != baseline
    assert "trading_days" not in baseline


def test_legacy_checkpoint_without_asset_semantics_starts_fresh_crypto_run(
    tmp_path: Path,
) -> None:
    class CheckpointState(TypedDict):
        count: int

    def increment(state: CheckpointState) -> dict[str, int]:
        return {"count": state["count"] + 1}

    workflow = StateGraph(CheckpointState)
    workflow.add_node("increment", increment)
    workflow.set_entry_point("increment")
    workflow.add_edge("increment", END)

    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    graph = object.__new__(trading_graph_module.TradingAgentsGraph)
    graph.config = {
        "checkpoint_enabled": True,
        "data_cache_dir": str(tmp_path),
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "evidence_gate_mode": "enforce",
    }
    graph.selected_analysts = ("market", "social", "news")
    graph.decision_policy = create_production_decision_policy()
    graph.decision_horizon = asset_configuration.horizon
    graph.asset_configuration = None
    graph.workflow = workflow
    graph.graph = workflow.compile()

    legacy_signature = graph._run_signature("crypto")
    legacy_config = {
        "configurable": {
            "thread_id": thread_id("SOL-USD", "2026-07-25", legacy_signature),
        }
    }
    with get_checkpointer(tmp_path, "SOL-USD") as saver:
        legacy_graph = workflow.compile(checkpointer=saver)
        legacy_graph.invoke({"count": 40}, config=legacy_config)
    assert has_checkpoint(
        tmp_path,
        "SOL-USD",
        "2026-07-25",
        legacy_signature,
    )

    graph.asset_configuration = asset_configuration
    current_signature = graph._run_signature("crypto")
    assert current_signature != legacy_signature

    with graph.checkpoint_scope("SOL-USD", "2026-07-25", "crypto") as session:
        assert session.resume_from_checkpoint is False
        assert session.graph_config["configurable"]["thread_id"] == thread_id(
            "SOL-USD",
            "2026-07-25",
            current_signature,
        )
        result = graph.graph.invoke({"count": 0}, config=session.graph_config)

    assert result["count"] == 1
    assert has_checkpoint(
        tmp_path,
        "SOL-USD",
        "2026-07-25",
        legacy_signature,
    )


def test_cli_resolves_asset_configuration_before_graph_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    resolved = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )

    class ConstructionObserved(RuntimeError):
        pass

    def resolve_before_graph(symbol: str, *, config):
        assert symbol == "SOL-USD"
        events.append("asset_configuration")
        return resolved

    class GraphProbe:
        def __init__(self, *_args, **kwargs):
            events.append("graph")
            assert kwargs["asset_configuration"] is resolved
            assert kwargs["asset_type"] == "crypto"
            raise ConstructionObserved

    selections = {
        "ticker": "SOL-USD",
        "analysis_date": "2026-07-25",
        "asset_type": "crypto",
        "analysts": [SimpleNamespace(value="market")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": "fixture-quick",
        "deep_thinker": "fixture-deep",
        "backend_url": None,
        "llm_provider": "openai",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }
    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(
        cli_main,
        "DEFAULT_CONFIG",
        dict(
            cli_main.DEFAULT_CONFIG,
            results_dir=str(tmp_path / "results"),
            data_cache_dir=str(tmp_path / "cache"),
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "resolve_run_asset_configuration",
        resolve_before_graph,
        raising=False,
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", GraphProbe)

    result = CliRunner().invoke(cli_main.app, ["analyze", "--no-checkpoint"])

    assert isinstance(result.exception, ConstructionObserved)
    assert events == ["asset_configuration", "graph"]


def test_programmatic_constructor_resolves_asset_before_model_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_resolver = trading_graph_module.resolve_run_asset_configuration

    def recording_resolver(symbol: str, *, config):
        events.append("asset_configuration")
        return original_resolver(symbol, config=config)

    llm = MagicMock()
    llm.with_structured_output.return_value = MagicMock()
    client = MagicMock()
    client.get_llm.return_value = llm

    def recording_model_client(**_kwargs):
        events.append("model_client")
        return client

    monkeypatch.setattr(
        trading_graph_module,
        "resolve_run_asset_configuration",
        recording_resolver,
        raising=False,
    )
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        recording_model_client,
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
        }
    )

    graph = trading_graph_module.TradingAgentsGraph(
        selected_analysts=("market", "fundamentals"),
        config=config,
        instrument_symbol="SOL-USD",
    )

    assert events[0] == "asset_configuration"
    assert events[1:] == ["model_client", "model_client"]
    assert graph.asset_configuration is not None
    assert graph.asset_configuration.instrument_identity.symbol == "SOL-USD"
    assert graph.asset_type == "crypto"


@pytest.mark.parametrize(
    ("config_update", "diagnostic_code"),
    [
        (
            {"crypto_identity_registry_sha256": None},
            "crypto_registry_pin_partial_override",
        ),
        (
            {"crypto_identity_registry_sha256": "0" * 64},
            "registry_digest_mismatch",
        ),
        (
            {"crypto_identity_registry_id": "wrong-registry"},
            "crypto_registry_id_mismatch",
        ),
    ],
)
def test_incoherent_crypto_configuration_fails_before_model_clients(
    config_update: dict,
    diagnostic_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(config_update)

    def forbidden_model_client(**_kwargs):
        raise AssertionError("asset configuration must fail before model clients")

    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        forbidden_model_client,
    )

    with pytest.raises(RunAssetConfigurationError) as raised:
        trading_graph_module.TradingAgentsGraph(
            config=config,
            instrument_symbol="SOL-USD",
        )

    assert raised.value.diagnostic_code == diagnostic_code


def test_decision_audit_records_complete_crypto_asset_configuration(
    tmp_path: Path,
) -> None:
    asset_configuration = resolve_run_asset_configuration(
        "SOL-USD",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    source_ref = stable_acquisition_source_ref(
        "identity-registry",
        asset_configuration.registry_digest,
    )
    registry_artifact = SourceArtifact(
        artifact_sha256=asset_configuration.registry_digest,
        source_ref=source_ref,
        tool_call_id="identity-registry",
        tool_name="instrument_identity_registry",
        raw_text=asset_configuration.registry_artifact,
    )
    evidence = EvidenceState(
        instrument_identity=asset_configuration.instrument_identity,
        source_artifacts=(registry_artifact,),
        acquisition_outcomes=(
            SourceAcquisitionAvailable(
                provider="instrument-identity-registry",
                capability="instrument_identity",
                source_ref=source_ref,
                attempt=1,
                retrieved_at="2026-07-25T00:00:00+00:00",
                artifact=registry_artifact,
            ),
        ),
    )
    state = Propagator().create_initial_state(
        "SOL-USD",
        "2026-07-25",
        asset_type="crypto",
        asset_configuration=asset_configuration,
        evidence_state=evidence,
    )
    state.update(
        create_preflight_gate_node(
            create_production_decision_policy(),
            asset_configuration.horizon,
        )(state)
    )
    state["graph_signature"] = (
        "asset_configuration=" + asset_configuration.asset_configuration_signature
    )
    state["evidence_gate_mode"] = "enforce"
    config = dict(
        DEFAULT_CONFIG,
        asset_configuration_signature=(
            asset_configuration.asset_configuration_signature
        ),
    )

    audit = prepare_decision_audit(state, tmp_path, config=config)

    projected = audit["asset_configuration"]
    assert projected["instrument_identity"]["symbol"] == "SOL-USD"
    assert projected["instrument_kind"] == "crypto"
    assert projected["registry_id"] == "crypto-ccc-identity-registry-v1"
    assert projected["registry_digest"] == asset_configuration.registry_digest
    assert projected["reference_market"] == "CCC"
    assert projected["capability_profile"]["profile_id"] == "crypto.v1"
    assert projected["observation_calendar_kind"] == "consecutive_daily"
    assert projected["calculation_id"] == asset_configuration.calculation_id
    assert projected["horizon"] == {
        "contract_version": "1.0",
        "count": 20,
        "unit": "calendar_days",
    }
    assert projected["asset_configuration_signature"] == (
        asset_configuration.asset_configuration_signature
    )
    assert "registry_source_ref" not in projected
    assert "registry_artifact" not in projected
    assert "trading_days" not in json.dumps(projected, sort_keys=True)


def test_yahoo_backed_sol_cli_and_programmatic_contracts_match_full_asset_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class QuietDisplay:
        def start(self) -> None:
            return None

        def refresh(self, _spinner_text=None) -> None:
            return None

        def publish_event(self, _event) -> None:
            return None

        def report_ready(self, _section, _content, _path) -> None:
            return None

        def close(self) -> None:
            return None

    class StreamProbe:
        def __init__(self, delegate, owner) -> None:
            self._delegate = delegate
            self._owner = owner

        def stream(self, *args, **kwargs):
            events.append("stream")
            assert self._owner.asset_configuration is not None
            return self._delegate.stream(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    class OrderedCliGraph(trading_graph_module.TradingAgentsGraph):
        def __init__(self, *args, **kwargs) -> None:
            events.append("graph")
            assert kwargs["asset_configuration"] is not None
            super().__init__(*args, **kwargs)

        @contextmanager
        def checkpoint_scope(self, *args, **kwargs):
            events.append("checkpoint")
            assert self.asset_configuration is not None
            with super().checkpoint_scope(*args, **kwargs) as session:
                self.graph = StreamProbe(self.graph, self)
                yield session

        def resolve_evidence_state(self, *args, **kwargs):
            events.append("evidence")
            assert self.asset_configuration is not None
            return super().resolve_evidence_state(*args, **kwargs)

        def create_initial_state(self, *args, **kwargs):
            events.append("checkpoint_state")
            assert self.asset_configuration is not None
            state = super().create_initial_state(*args, **kwargs)
            assert state["asset_configuration"] is not None
            return state

    model = MagicMock()
    model.with_structured_output.return_value = model
    client = MagicMock()
    client.get_llm.return_value = model
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        lambda **_kwargs: client,
    )
    dates = pd.date_range(start="2026-07-05", periods=21, freq="D")
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": [100.0] * 21,
            "High": [100.0] * 21,
            "Low": [100.0] * 21,
            "Close": [100.0] * 21,
            "Volume": [100.0] * 21,
        }
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
            "checkpoint_enabled": True,
            "evidence_gate_mode": "enforce",
        }
    )
    selections = {
        "ticker": "SOL-USD",
        "analysis_date": "2026-07-25",
        "asset_type": "crypto",
        "analysts": [SimpleNamespace(value="market")],
        "china_a_enhancement_preset": "basic",
        "research_depth": 1,
        "shallow_thinker": config["quick_think_llm"],
        "deep_thinker": config["deep_think_llm"],
        "backend_url": config["backend_url"],
        "llm_provider": config["llm_provider"],
        "google_thinking_level": config["google_thinking_level"],
        "openai_reasoning_effort": config["openai_reasoning_effort"],
        "anthropic_effort": config["anthropic_effort"],
        "output_language": config["output_language"],
    }
    captured_cli: dict = {}
    original_mark_completed = cli_main._mark_run_completed

    def capture_completed(final_state, artifacts):
        captured_cli.update(final_state)
        original_mark_completed(final_state, artifacts)

    original_cli_resolver = cli_main.resolve_run_asset_configuration

    def recording_cli_resolver(symbol: str, *, config):
        events.append("asset_configuration")
        return original_cli_resolver(symbol, config=config)

    monkeypatch.setattr(cli_main, "get_user_selections", lambda: selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(
        cli_main,
        "resolve_run_asset_configuration",
        recording_cli_resolver,
    )
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", OrderedCliGraph)
    monkeypatch.setattr(
        cli_main,
        "create_run_display",
        lambda *_args, **_kwargs: QuietDisplay(),
    )
    monkeypatch.setattr(cli_main, "_mark_run_completed", capture_completed)
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *_args, **_kwargs: "N")

    with patch.object(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"yfinance": SnapshotProvider(lambda *_args: frame, "auto_adjusted")},
    ):
        cli_result = CliRunner().invoke(
            cli_main.app,
            ["analyze", "--checkpoint"],
        )
        assert cli_result.exit_code == 0, cli_result.output

        programmatic_graph = trading_graph_module.TradingAgentsGraph(
            selected_analysts=("market",),
            config=config,
            instrument_symbol="SOL-USD",
        )
        programmatic, signal = programmatic_graph.propagate(
            "SOL-USD",
            "2026-07-25",
        )

    assert signal is None
    assert captured_cli["terminal_contract"] == programmatic["terminal_contract"]
    assert captured_cli["configuration_digest"] == programmatic["configuration_digest"]
    assert captured_cli["asset_configuration"] == programmatic["asset_configuration"]
    assert events == [
        "asset_configuration",
        "graph",
        "checkpoint",
        "evidence",
        "checkpoint_state",
        "stream",
    ]
    cli_audit = json.loads(
        Path(captured_cli["decision_audit_path"]).read_text(encoding="utf-8")
    ) if captured_cli.get("decision_audit_path") else json.loads(
        next((tmp_path / "results").rglob("decision-audit.json")).read_text(
            encoding="utf-8"
        )
    )
    programmatic_audit = json.loads(
        Path(programmatic["decision_audit_path"]).read_text(encoding="utf-8")
    )
    assert cli_audit["asset_configuration"] == programmatic_audit["asset_configuration"]
    checkpoint_snapshot = captured_cli["evidence_state"]["market_snapshot"]
    for audit in (cli_audit, programmatic_audit):
        asset_audit = audit["asset_configuration"]
        assert asset_audit["instrument_identity"]["symbol"] == "SOL-USD"
        assert asset_audit["instrument_kind"] == "crypto"
        assert asset_audit["registry_id"] == "crypto-ccc-identity-registry-v1"
        assert asset_audit["registry_digest"] == config[
            "crypto_identity_registry_sha256"
        ]
        assert asset_audit["reference_market"] == "CCC"
        assert asset_audit["capability_profile"]["profile_id"] == "crypto.v1"
        assert asset_audit["observation_calendar_kind"] == "consecutive_daily"
        assert asset_audit["horizon"] == {
            "contract_version": "1.0",
            "count": 20,
            "unit": "calendar_days",
        }
        assert "trading_days" not in json.dumps(asset_audit, sort_keys=True)
        market_audit = audit["evidence_state"]["market_snapshot"]
        assert market_audit["history_rows"] == 21
        manifest_json = market_audit["snapshot_manifest_json"]
        manifest_digest = sha256(manifest_json.encode("utf-8")).hexdigest()
        assert market_audit["snapshot_id"] == f"snapshot:v2:{manifest_digest}"
        assert market_audit["pin_membership_digest"] == manifest_digest
        assert json.loads(manifest_json)["instrument"]["canonical_symbol"] == (
            "SOL-USD"
        )
        assert checkpoint_snapshot["snapshot_id"] == market_audit["snapshot_id"]
        assert checkpoint_snapshot["snapshot_manifest_json"] == manifest_json
        market_return_fact = next(
            fact
            for fact in audit["evidence_state"]["source_facts"]
            if fact["canonical_field"] == "market.close_return_20d"
        )
        lineage = market_return_fact["calculation_lineage"]
        assert lineage["calculation_id"] == (
            "market.close_return_20d.crypto.auto_adjusted"
        )
        assert lineage["effective_range_start"] == "2026-07-05"
        assert lineage["effective_range_end"] == "2026-07-25"
        assert lineage["observations_used"] == 21
        assert lineage["input_snapshot_id"] == market_audit["snapshot_id"]
        assert market_audit["physical_attempt_count"] == 1
        assert len(market_audit["physical_attempt_events"]) == 1
        attempt = market_audit["physical_attempt_events"][0]
        assert attempt["attempt_index"] == 1
        assert attempt["outcome"] == "available"
        assert attempt["pacing_event"] == "permit_acquired"
        assert attempt["final_physical_attempt_count"] == 1
        assert attempt["upstream_service_name"] == "Yahoo Finance"
    rendered_reports = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "results").rglob("*.md")
    )
    assert "## Physical Provider Attempts" in rendered_reports
    assert "**Upstream Service Identity:**" in rendered_reports
    assert "**Final physical-attempt count:** 1" in rendered_reports
    assert checkpoint_snapshot["snapshot_id"] in rendered_reports
    assert model.invoke.call_count > 0
    model.stream.assert_not_called()

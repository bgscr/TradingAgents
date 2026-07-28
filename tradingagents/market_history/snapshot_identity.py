from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

SNAPSHOT_V2_MANIFEST_VERSION = "2.0"
SNAPSHOT_V2_ID_PATTERN = re.compile(r"^snapshot:v2:[0-9a-f]{64}$")
LEGACY_SNAPSHOT_ID_PATTERN = re.compile(r"^snapshot:[0-9a-f]{64}$")


@dataclass(frozen=True)
class SnapshotIdentityV2:
    snapshot_id: str
    membership_digest: str
    manifest_json: str


def live_snapshot_v2_identity(
    *,
    instrument_id: str,
    canonical_symbol: str,
    identity_revision: str,
    reference_market: str,
    instrument_kind: str,
    currency: str,
    provider_dataset_id: str,
    provider_name: str,
    upstream_service_id: str,
    requested_date: str,
    effective_trading_date: str,
    adjustment_basis: str,
    frame_digest: str,
    history_rows: int,
    derivation_version: str,
    normalization_version: str,
    accepted_artifact_identity: str,
    authoritative_status_identity: str,
    authoritative_status_value: str,
    provenance_class: str,
) -> SnapshotIdentityV2:
    """Return a v2 identity for a current-only snapshot without durable revisions."""

    manifest = {
        "adjustment_basis": adjustment_basis,
        "authoritative_status": {
            "identity": authoritative_status_identity,
            "value": authoritative_status_value,
        },
        "derivation_version": derivation_version,
        "effective_trading_date": effective_trading_date,
        "frame": {"rows": history_rows, "sha256": frame_digest},
        "instrument": {
            "canonical_symbol": canonical_symbol.strip().upper(),
            "currency": currency,
            "identity_revision": identity_revision,
            "instrument_id": instrument_id,
            "instrument_kind": instrument_kind,
            "reference_market": reference_market,
        },
        "live_artifact_identity": accepted_artifact_identity,
        "manifest_version": SNAPSHOT_V2_MANIFEST_VERSION,
        "normalization_version": normalization_version,
        "provenance_class": provenance_class,
        "provider": {
            "provider_dataset_id": provider_dataset_id,
            "provider_name": provider_name,
            "upstream_service_id": upstream_service_id,
        },
        "requested_date": requested_date,
        "snapshot_kind": "current_only_live_artifact",
    }
    return _identity_from_manifest(manifest)


def snapshot_v2_identity(
    *,
    instrument_id: str,
    canonical_symbol: str,
    identity_revision: str,
    reference_market: str,
    instrument_kind: str,
    currency: str,
    provider_dataset_id: str,
    provider_name: str,
    upstream_service_id: str,
    requested_date: str,
    effective_trading_date: str,
    adjustment_basis: str,
    frame_digest: str,
    history_rows: int,
    derivation_version: str,
    normalization_version: str,
    observation_membership: tuple[tuple[str, str, str, str], ...],
    factor_revision_ids: tuple[str, ...],
    calendar_revision_id: str,
    provenance_class: str,
    bundle_revision_id: str,
    retrieval_cutoff: str,
    bundle_observed_at: str,
) -> SnapshotIdentityV2:
    """Return the canonical exact-membership identity for one stored snapshot."""

    ordered_observations = tuple(
        sorted(
            observation_membership,
            key=lambda item: (item[0], item[1], item[2], item[3]),
        )
    )
    session_dates = tuple(item[0] for item in ordered_observations)
    if len(session_dates) != len(set(session_dates)):
        raise ValueError("snapshot membership contains duplicate session dates")
    canonical_factors = tuple(sorted(set(factor_revision_ids)))
    if len(canonical_factors) != len(factor_revision_ids):
        raise ValueError("snapshot membership contains duplicate factor revisions")
    if history_rows != len(ordered_observations):
        raise ValueError("snapshot row count does not match observation membership")

    latest_status = ordered_observations[-1][3]
    manifest: dict[str, Any] = {
        "adjustment_basis": adjustment_basis,
        "authoritative_status": {
            "identity": ordered_observations[-1][2],
            "value": latest_status,
        },
        "bundle_revision_id": bundle_revision_id,
        "bundle_observed_at": bundle_observed_at,
        "calendar_revision_id": calendar_revision_id,
        "derivation_version": derivation_version,
        "effective_trading_date": effective_trading_date,
        "factors": list(canonical_factors),
        "frame": {"rows": history_rows, "sha256": frame_digest},
        "instrument": {
            "canonical_symbol": canonical_symbol.strip().upper(),
            "currency": currency,
            "identity_revision": identity_revision,
            "instrument_id": instrument_id,
            "instrument_kind": instrument_kind,
            "reference_market": reference_market,
        },
        "manifest_version": SNAPSHOT_V2_MANIFEST_VERSION,
        "normalization_version": normalization_version,
        "observations": [
            {
                "observation_revision_id": observation_revision_id,
                "ordinal": ordinal,
                "session_date": session_date,
                "trading_status_revision_id": status_revision_id,
                "trading_status_value": status_value,
            }
            for ordinal, (
                session_date,
                observation_revision_id,
                status_revision_id,
                status_value,
            ) in enumerate(ordered_observations)
        ],
        "provenance_class": provenance_class,
        "provider": {
            "provider_dataset_id": provider_dataset_id,
            "provider_name": provider_name,
            "upstream_service_id": upstream_service_id,
        },
        "requested_date": requested_date,
        "retrieval_cutoff": retrieval_cutoff,
        "snapshot_kind": "durable_exact_pin",
    }
    return _identity_from_manifest(manifest)


def _identity_from_manifest(manifest: dict[str, Any]) -> SnapshotIdentityV2:
    manifest_json = json.dumps(
        manifest,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = sha256(manifest_json.encode("utf-8")).hexdigest()
    return SnapshotIdentityV2(
        snapshot_id=f"snapshot:v2:{digest}",
        membership_digest=digest,
        manifest_json=manifest_json,
    )

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tradingagents.asset_configuration import RunAssetConfiguration

SNAPSHOT_V2_MANIFEST_VERSION = "2.0"
CRYPTO_SNAPSHOT_IDENTITY_BINDING_VERSION = "1.0"
SNAPSHOT_V2_ID_PATTERN = re.compile(r"^snapshot:v2:[0-9a-f]{64}$")
LEGACY_SNAPSHOT_ID_PATTERN = re.compile(r"^snapshot:[0-9a-f]{64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAINLAND_DATASET_MARKERS = (
    "a-share",
    "cn-a",
    "cn_a",
    "mainland",
    "qfq",
    "xshg",
    "xshe",
)


@dataclass(frozen=True)
class SnapshotIdentityV2:
    snapshot_id: str
    membership_digest: str
    manifest_json: str


class CryptoSnapshotIdentityMismatch(ValueError):
    """A new crypto snapshot cannot prove one authoritative material identity."""

    diagnostic_code = "crypto_snapshot_identity_mismatch"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(self.diagnostic_code)


class LegacyCryptoSnapshotIdentityIncomplete(ValueError):
    """Historical crypto material cannot prove the corrected replay binding."""

    diagnostic_code = "legacy_crypto_snapshot_identity_incomplete"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(self.diagnostic_code)


@dataclass(frozen=True)
class CryptoProviderDatasetDescriptor:
    """Closed material descriptor for one accepted crypto provider dataset."""

    provider_dataset_id: str
    provider_name: str
    upstream_service_id: str
    dataset_family: str
    dataset_name: str
    dataset_version: str
    dataset_revision: str
    reference_market: str
    tags: tuple[str, ...] = ()
    contract_version: str = "1.0"


def _lacks_recorded_shape(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return not isinstance(actual, dict) or any(
            key not in actual or _lacks_recorded_shape(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return not isinstance(actual, list) or any(
            _lacks_recorded_shape(item, expected_item)
            for item, expected_item in zip(actual, expected, strict=False)
        )
    return False


def validate_crypto_replay_manifest_completeness(
    manifest_json: str | None,
    *,
    snapshot_id: str,
    manifest_digest: str | None,
    asset_configuration: RunAssetConfiguration,
    crypto_provider_dataset: CryptoProviderDatasetDescriptor | None,
    stored_material: dict[str, object],
    observation_membership: tuple[tuple[int, str, str, str, str | None], ...],
    factor_revision_ids: tuple[str, ...],
) -> None:
    """Reject recorded crypto v2 material that lacks its corrected binding."""

    try:
        manifest = json.loads(manifest_json) if manifest_json is not None else None
        recorded_asset = manifest["asset_configuration"]
        recorded_instrument = manifest["instrument"]
        recorded_provider = manifest["provider"]
        registry = recorded_asset["registry"]
        registry_digest = registry["digest"]
        registry_id = registry["registry_id"]
        capability_profile = recorded_asset["capability_profile"]
        identity_provenance = recorded_instrument["identity_provenance"]
        recorded_dataset = recorded_provider["dataset"]
        recorded_material = {
            "adjustment_basis": manifest["adjustment_basis"],
            "authoritative_status": manifest["authoritative_status"],
            "bundle_observed_at": manifest["bundle_observed_at"],
            "bundle_revision_id": manifest["bundle_revision_id"],
            "calendar_revision_id": manifest["calendar_revision_id"],
            "crypto_identity_binding_version": manifest[
                "crypto_identity_binding_version"
            ],
            "derivation_version": manifest["derivation_version"],
            "effective_trading_date": manifest["effective_trading_date"],
            "factors": manifest["factors"],
            "frame": manifest["frame"],
            "manifest_version": manifest["manifest_version"],
            "normalization_version": manifest["normalization_version"],
            "observations": manifest["observations"],
            "provenance_class": manifest["provenance_class"],
            "requested_date": manifest["requested_date"],
            "retrieval_cutoff": manifest["retrieval_cutoff"],
            "snapshot_kind": manifest["snapshot_kind"],
        }
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LegacyCryptoSnapshotIdentityIncomplete(
            "historical crypto snapshot lacks corrected identity material"
        ) from exc
    if (
        not isinstance(registry_digest, str)
        or not registry_digest.strip()
        or not isinstance(registry_id, str)
        or not registry_id.strip()
        or not isinstance(capability_profile, dict)
        or not isinstance(identity_provenance, dict)
        or not isinstance(recorded_provider, dict)
        or not isinstance(recorded_dataset, dict)
    ):
        raise LegacyCryptoSnapshotIdentityIncomplete(
            "historical crypto snapshot lacks corrected identity material"
        )
    identity = asset_configuration.instrument_identity
    provenance = identity.provenance
    if provenance is None:
        raise CryptoSnapshotIdentityMismatch(
            "requested crypto Instrument Identity is unresolved"
        )
    if (
        asset_configuration.instrument_kind != identity.instrument_kind
        or asset_configuration.reference_market != identity.venue
        or asset_configuration.capability_profile.instrument_kind
        != asset_configuration.instrument_kind
    ):
        raise CryptoSnapshotIdentityMismatch(
            "requested RunAssetConfiguration has contradictory crypto semantics"
        )
    expected_asset = {
        "capability_profile": {
            "contract_version": asset_configuration.capability_profile.contract_version,
            "profile_id": asset_configuration.capability_profile.profile_id,
            "profile_version": asset_configuration.capability_profile.contract_version,
        },
        "contract_version": asset_configuration.asset_configuration_version,
        "observation_calendar_kind": (
            asset_configuration.observation_calendar_kind.value
        ),
        "registry": {
            "digest": asset_configuration.registry_digest,
            "registry_id": asset_configuration.registry_id,
        },
    }
    expected_instrument = {
        "canonical_symbol": identity.symbol,
        "currency": identity.currency,
        "identity_provenance": provenance.model_dump(mode="json"),
        "identity_revision": crypto_snapshot_identity_revision(asset_configuration),
        "instrument_id": crypto_snapshot_instrument_id(asset_configuration),
        "instrument_kind": identity.instrument_kind.value,
        "reference_market": identity.venue,
    }
    if _lacks_recorded_shape(
        recorded_asset, expected_asset
    ) or _lacks_recorded_shape(recorded_instrument, expected_instrument):
        raise LegacyCryptoSnapshotIdentityIncomplete(
            "historical crypto snapshot lacks corrected identity material"
        )
    if recorded_asset != expected_asset or recorded_instrument != expected_instrument:
        raise CryptoSnapshotIdentityMismatch(
            "recorded crypto identity contradicts the requested run asset"
        )
    if not isinstance(crypto_provider_dataset, CryptoProviderDatasetDescriptor):
        raise CryptoSnapshotIdentityMismatch(
            "requested crypto provider dataset is missing"
        )
    dataset = validate_crypto_provider_dataset_descriptor(
        crypto_provider_dataset,
        provider_name=crypto_provider_dataset.provider_name,
        upstream_service_id=crypto_provider_dataset.upstream_service_id,
    )
    expected_provider = {
        "dataset": _crypto_provider_dataset_payload(dataset),
        "provider_dataset_id": dataset.provider_dataset_id,
        "provider_name": dataset.provider_name,
        "upstream_service_id": dataset.upstream_service_id,
    }
    if _lacks_recorded_shape(recorded_provider, expected_provider):
        raise LegacyCryptoSnapshotIdentityIncomplete(
            "historical crypto snapshot lacks corrected identity material"
        )
    if recorded_provider != expected_provider:
        raise CryptoSnapshotIdentityMismatch(
            "recorded crypto provider dataset contradicts the requested dataset"
        )
    canonical_manifest = json.dumps(
        manifest,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    canonical_digest = sha256(canonical_manifest.encode("utf-8")).hexdigest()
    if (
        canonical_manifest != manifest_json
        or canonical_digest != manifest_digest
        or snapshot_id != f"snapshot:v2:{canonical_digest}"
    ):
        raise CryptoSnapshotIdentityMismatch(
            "recorded crypto manifest identity is inconsistent"
        )
    expected_observations = [
        {
            "observation_revision_id": observation_revision_id,
            "ordinal": ordinal,
            "session_date": session_date,
            "trading_status_revision_id": status_revision_id,
            "trading_status_value": status_value,
        }
        for ordinal, session_date, observation_revision_id, status_revision_id, status_value
        in observation_membership
    ]
    expected_status = (
        {
            "identity": observation_membership[-1][3],
            "value": observation_membership[-1][4],
        }
        if observation_membership
        else None
    )
    expected_material = {
        **stored_material,
        "authoritative_status": expected_status,
        "crypto_identity_binding_version": CRYPTO_SNAPSHOT_IDENTITY_BINDING_VERSION,
        "factors": sorted(factor_revision_ids),
        "manifest_version": SNAPSHOT_V2_MANIFEST_VERSION,
        "observations": expected_observations,
        "snapshot_kind": "durable_exact_pin",
    }
    if _lacks_recorded_shape(recorded_material, expected_material):
        raise LegacyCryptoSnapshotIdentityIncomplete(
            "historical crypto snapshot lacks corrected identity material"
        )
    if recorded_material != expected_material:
        raise CryptoSnapshotIdentityMismatch(
            "recorded crypto manifest contradicts stored exact pin material"
        )


def build_crypto_provider_dataset_descriptor(
    *,
    provider_name: str,
    upstream_service_id: str,
    dataset_family: str,
    dataset_name: str,
    dataset_version: str,
    dataset_revision: str,
    reference_market: str,
    tags: tuple[str, ...] = (),
    contract_version: str = "1.0",
) -> CryptoProviderDatasetDescriptor:
    """Build a descriptor whose ID authenticates every canonical component."""

    provisional = CryptoProviderDatasetDescriptor(
        provider_dataset_id="pending",
        provider_name=provider_name,
        upstream_service_id=upstream_service_id,
        dataset_family=dataset_family,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        dataset_revision=dataset_revision,
        reference_market=reference_market,
        tags=tags,
        contract_version=contract_version,
    )
    return CryptoProviderDatasetDescriptor(
        **{
            **provisional.__dict__,
            "provider_dataset_id": canonical_crypto_provider_dataset_id(provisional),
        }
    )


def canonical_crypto_provider_dataset_id(
    descriptor: CryptoProviderDatasetDescriptor,
) -> str:
    """Return the canonical ID for a crypto provider dataset descriptor."""

    encoded = json.dumps(
        _crypto_provider_dataset_payload(descriptor, include_routing=True),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"provider-dataset:crypto:v1:{sha256(encoded).hexdigest()}"


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
    asset_configuration: RunAssetConfiguration | None = None,
    crypto_provider_dataset: CryptoProviderDatasetDescriptor | None = None,
) -> SnapshotIdentityV2:
    """Return a v2 identity for a current-only snapshot without durable revisions."""

    manifest: dict[str, Any] = {
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
    _bind_crypto_manifest_if_applicable(
        manifest,
        asset_configuration=asset_configuration,
        crypto_provider_dataset=crypto_provider_dataset,
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        identity_revision=identity_revision,
        reference_market=reference_market,
        instrument_kind=instrument_kind,
        currency=currency,
        provider_dataset_id=provider_dataset_id,
        provider_name=provider_name,
        upstream_service_id=upstream_service_id,
        adjustment_basis=adjustment_basis,
        requested_date=requested_date,
        effective_trading_date=effective_trading_date,
        frame_digest=frame_digest,
        history_rows=history_rows,
        derivation_version=derivation_version,
        normalization_version=normalization_version,
        authoritative_status_identity=authoritative_status_identity,
        authoritative_status_value=authoritative_status_value,
        provenance_class=provenance_class,
    )
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
    asset_configuration: RunAssetConfiguration | None = None,
    crypto_provider_dataset: CryptoProviderDatasetDescriptor | None = None,
) -> SnapshotIdentityV2:
    """Return the canonical exact-membership identity for one stored snapshot."""

    return _snapshot_v2_identity(**locals())


def _snapshot_v2_identity(
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
    asset_configuration: RunAssetConfiguration | None,
    crypto_provider_dataset: CryptoProviderDatasetDescriptor | None,
    historical_crypto_compatibility: bool = False,
) -> SnapshotIdentityV2:

    crypto_input = _is_crypto_manifest_input(
        canonical_symbol,
        instrument_kind,
        allow_unbound_crypto_symbol=historical_crypto_compatibility,
    )
    configured_crypto = (
        asset_configuration is not None
        and asset_configuration.instrument_kind.value == "crypto"
    )

    def membership_mismatch(detail: str) -> None:
        if crypto_input or configured_crypto:
            raise CryptoSnapshotIdentityMismatch(detail)
        raise ValueError(detail)

    ordered_observations = tuple(
        sorted(
            observation_membership,
            key=lambda item: (item[0], item[1], item[2], item[3]),
        )
    )
    session_dates = tuple(item[0] for item in ordered_observations)
    if len(session_dates) != len(set(session_dates)):
        membership_mismatch("snapshot membership contains duplicate session dates")
    canonical_factors = tuple(sorted(set(factor_revision_ids)))
    if len(canonical_factors) != len(factor_revision_ids):
        membership_mismatch("snapshot membership contains duplicate factor revisions")
    if history_rows != len(ordered_observations):
        membership_mismatch("snapshot row count does not match observation membership")
    if not ordered_observations:
        membership_mismatch("snapshot observation membership is empty")
    if crypto_input or configured_crypto:
        _validate_crypto_durable_material(
            observation_membership=ordered_observations,
            factor_revision_ids=canonical_factors,
            calendar_revision_id=calendar_revision_id,
            effective_trading_date=effective_trading_date,
            bundle_revision_id=bundle_revision_id,
            retrieval_cutoff=retrieval_cutoff,
            bundle_observed_at=bundle_observed_at,
        )

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
    _bind_crypto_manifest_if_applicable(
        manifest,
        asset_configuration=asset_configuration,
        crypto_provider_dataset=crypto_provider_dataset,
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        identity_revision=identity_revision,
        reference_market=reference_market,
        instrument_kind=instrument_kind,
        currency=currency,
        provider_dataset_id=provider_dataset_id,
        provider_name=provider_name,
        upstream_service_id=upstream_service_id,
        adjustment_basis=adjustment_basis,
        requested_date=requested_date,
        effective_trading_date=effective_trading_date,
        frame_digest=frame_digest,
        history_rows=history_rows,
        derivation_version=derivation_version,
        normalization_version=normalization_version,
        authoritative_status_identity=ordered_observations[-1][2],
        authoritative_status_value=latest_status,
        provenance_class=provenance_class,
        allow_unbound_crypto_symbol=historical_crypto_compatibility,
    )
    return _identity_from_manifest(manifest)


def _historical_snapshot_v2_identity(**kwargs: Any) -> SnapshotIdentityV2:
    """Reconstruct stored pre-binding material without enabling new publication."""

    return _snapshot_v2_identity(
        **kwargs,
        historical_crypto_compatibility=True,
    )


def crypto_snapshot_instrument_id(
    asset_configuration: RunAssetConfiguration,
) -> str:
    """Return the stable identifier authenticated by a crypto run configuration."""

    identity = asset_configuration.instrument_identity
    encoded = json.dumps(
        {
            "canonical_symbol": identity.symbol,
            "currency": identity.currency,
            "instrument_kind": identity.instrument_kind.value,
            "reference_market": identity.venue,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"crypto-instrument=sha256:{sha256(encoded).hexdigest()}"


def crypto_snapshot_identity_revision(
    asset_configuration: RunAssetConfiguration,
) -> str:
    """Return the pinned registry revision used as crypto identity authority."""

    return (
        f"crypto-registry:{asset_configuration.registry_id}:"
        f"sha256:{asset_configuration.registry_digest}"
    )


def validate_crypto_provider_dataset_descriptor(
    descriptor: CryptoProviderDatasetDescriptor | None,
    *,
    provider_name: str,
    upstream_service_id: str,
) -> CryptoProviderDatasetDescriptor:
    """Fail typed before hashing when crypto dataset routing is contradictory."""

    if not isinstance(descriptor, CryptoProviderDatasetDescriptor):
        raise CryptoSnapshotIdentityMismatch("crypto provider dataset is missing")
    material_values = (
        descriptor.contract_version,
        descriptor.provider_dataset_id,
        descriptor.provider_name,
        descriptor.upstream_service_id,
        descriptor.dataset_family,
        descriptor.dataset_name,
        descriptor.dataset_version,
        descriptor.dataset_revision,
        descriptor.reference_market,
        *descriptor.tags,
    )
    if any(not str(value).strip() for value in material_values):
        raise CryptoSnapshotIdentityMismatch(
            "crypto provider dataset contains a blank material field"
        )
    if descriptor.provider_name != provider_name:
        raise CryptoSnapshotIdentityMismatch(
            "crypto provider dataset contradicts the selected provider"
        )
    if descriptor.upstream_service_id != upstream_service_id:
        raise CryptoSnapshotIdentityMismatch(
            "crypto provider dataset contradicts the Upstream Service Identity"
        )
    if descriptor.dataset_family.strip().casefold() != "crypto":
        raise CryptoSnapshotIdentityMismatch(
            "crypto provider dataset has a non-crypto family"
        )
    if descriptor.reference_market != "CCC":
        raise CryptoSnapshotIdentityMismatch(
            "crypto provider dataset has a non-CCC Reference Market"
        )
    searchable = " ".join(str(value).casefold() for value in material_values)
    if any(marker in searchable for marker in _MAINLAND_DATASET_MARKERS):
        raise CryptoSnapshotIdentityMismatch(
            "crypto provider dataset contains mainland semantics"
        )
    if len(set(descriptor.tags)) != len(descriptor.tags):
        raise CryptoSnapshotIdentityMismatch(
            "crypto provider dataset contains duplicate tags"
        )
    if descriptor.provider_dataset_id != canonical_crypto_provider_dataset_id(
        descriptor
    ):
        raise CryptoSnapshotIdentityMismatch(
            "crypto provider dataset ID contradicts its descriptor"
        )
    return descriptor


def _bind_crypto_manifest_if_applicable(
    manifest: dict[str, Any],
    *,
    asset_configuration: RunAssetConfiguration | None,
    crypto_provider_dataset: CryptoProviderDatasetDescriptor | None,
    instrument_id: str,
    canonical_symbol: str,
    identity_revision: str,
    reference_market: str,
    instrument_kind: str,
    currency: str,
    provider_dataset_id: str,
    provider_name: str,
    upstream_service_id: str,
    adjustment_basis: str,
    requested_date: str,
    effective_trading_date: str,
    frame_digest: str,
    history_rows: int,
    derivation_version: str,
    normalization_version: str,
    authoritative_status_identity: str,
    authoritative_status_value: str,
    provenance_class: str,
    allow_unbound_crypto_symbol: bool = False,
) -> None:
    crypto_input = _is_crypto_manifest_input(
        canonical_symbol,
        instrument_kind,
        allow_unbound_crypto_symbol=allow_unbound_crypto_symbol,
    )
    configured_crypto = (
        asset_configuration is not None
        and asset_configuration.instrument_kind.value == "crypto"
    )
    if not crypto_input and not configured_crypto:
        return
    if asset_configuration is None:
        raise CryptoSnapshotIdentityMismatch(
            "authoritative RunAssetConfiguration is required"
        )
    try:
        identity = asset_configuration.instrument_identity
        provenance = identity.provenance
        expected_instrument_id = crypto_snapshot_instrument_id(asset_configuration)
        expected_identity_revision = crypto_snapshot_identity_revision(
            asset_configuration
        )
        expected_identity = (
            expected_instrument_id,
            identity.symbol,
            expected_identity_revision,
            identity.venue,
            identity.instrument_kind.value,
            identity.currency,
        )
        actual_identity = (
            instrument_id,
            canonical_symbol.strip().upper(),
            identity_revision,
            reference_market,
            instrument_kind,
            currency,
        )
        if not identity.is_authoritative or provenance is None:
            raise CryptoSnapshotIdentityMismatch(
                "crypto Instrument Identity is unresolved"
            )
        if (
            asset_configuration.asset_configuration_version != "1.0"
            or not asset_configuration.registry_id.strip()
            or not _SHA256_PATTERN.fullmatch(asset_configuration.registry_digest)
            or provenance.artifact_sha256 != asset_configuration.registry_digest
            or asset_configuration.capability_profile.contract_version != "1.0"
        ):
            raise CryptoSnapshotIdentityMismatch(
                "crypto registry or Capability Profile binding is contradictory"
            )
        if actual_identity != expected_identity:
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot Instrument Identity contradicts the run asset"
            )
        if (
            asset_configuration.reference_market != "CCC"
            or identity.venue != "CCC"
            or asset_configuration.instrument_kind.value != "crypto"
            or identity.instrument_kind.value != "crypto"
            or identity.currency != "USD"
            or asset_configuration.capability_profile.profile_id != "crypto.v1"
            or asset_configuration.capability_profile.instrument_kind.value
            != "crypto"
            or asset_configuration.observation_calendar_kind.value
            != "consecutive_daily"
            or asset_configuration.adjustment_basis != "auto_adjusted"
            or adjustment_basis != asset_configuration.adjustment_basis
        ):
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot semantics contradict the run asset"
            )
        dataset = validate_crypto_provider_dataset_descriptor(
            crypto_provider_dataset,
            provider_name=provider_name,
            upstream_service_id=upstream_service_id,
        )
        if provider_dataset_id != dataset.provider_dataset_id:
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot dataset ID contradicts its descriptor"
            )
        if not _SHA256_PATTERN.fullmatch(frame_digest) or history_rows < 1:
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot frame identity is incomplete"
            )
        if date.fromisoformat(effective_trading_date) > date.fromisoformat(
            requested_date
        ):
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot effective date exceeds its requested date"
            )
        if any(
            not value.strip()
            for value in (
                derivation_version,
                normalization_version,
                authoritative_status_identity,
                authoritative_status_value,
                provenance_class,
            )
        ):
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot material metadata is incomplete"
            )
        if any(
            marker in value.strip().casefold()
            for value in (
                authoritative_status_identity,
                authoritative_status_value,
            )
            for marker in ("unknown", "unresolved")
        ):
            raise CryptoSnapshotIdentityMismatch(
                "crypto snapshot status identity is unresolved"
            )
    except CryptoSnapshotIdentityMismatch:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise CryptoSnapshotIdentityMismatch(
            "crypto snapshot material is malformed"
        ) from exc

    manifest["crypto_identity_binding_version"] = (
        CRYPTO_SNAPSHOT_IDENTITY_BINDING_VERSION
    )
    manifest["asset_configuration"] = {
        "capability_profile": {
            "contract_version": (
                asset_configuration.capability_profile.contract_version
            ),
            "profile_id": asset_configuration.capability_profile.profile_id,
            "profile_version": (
                asset_configuration.capability_profile.contract_version
            ),
        },
        "contract_version": asset_configuration.asset_configuration_version,
        "observation_calendar_kind": (
            asset_configuration.observation_calendar_kind.value
        ),
        "registry": {
            "digest": asset_configuration.registry_digest,
            "registry_id": asset_configuration.registry_id,
        },
    }
    manifest["instrument"] = {
        "canonical_symbol": identity.symbol,
        "currency": identity.currency,
        "identity_provenance": provenance.model_dump(mode="json"),
        "identity_revision": expected_identity_revision,
        "instrument_id": expected_instrument_id,
        "instrument_kind": identity.instrument_kind.value,
        "reference_market": identity.venue,
    }
    manifest["provider"] = {
        "dataset": _crypto_provider_dataset_payload(dataset),
        "provider_dataset_id": dataset.provider_dataset_id,
        "provider_name": dataset.provider_name,
        "upstream_service_id": dataset.upstream_service_id,
    }


def _crypto_provider_dataset_payload(
    descriptor: CryptoProviderDatasetDescriptor,
    *,
    include_routing: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": descriptor.contract_version,
        "dataset_family": descriptor.dataset_family,
        "dataset_name": descriptor.dataset_name,
        "dataset_revision": descriptor.dataset_revision,
        "dataset_version": descriptor.dataset_version,
        "reference_market": descriptor.reference_market,
        "tags": sorted(descriptor.tags),
    }
    if include_routing:
        payload["provider_name"] = descriptor.provider_name
        payload["upstream_service_id"] = descriptor.upstream_service_id
    return payload


def _is_crypto_symbol(symbol: str) -> bool:
    from tradingagents.dataflows.crypto_universe import is_crypto_pair_syntax

    return is_crypto_pair_syntax(symbol)


def _is_crypto_manifest_input(
    canonical_symbol: object,
    instrument_kind: object,
    *,
    allow_unbound_crypto_symbol: bool,
) -> bool:
    if isinstance(instrument_kind, str) and instrument_kind.strip().casefold() == "crypto":
        return True
    return (
        not allow_unbound_crypto_symbol
        and isinstance(canonical_symbol, str)
        and _is_crypto_symbol(canonical_symbol)
    )


def _validate_crypto_durable_material(
    *,
    observation_membership: tuple[tuple[str, str, str, str], ...],
    factor_revision_ids: tuple[str, ...],
    calendar_revision_id: str,
    effective_trading_date: str,
    bundle_revision_id: str,
    retrieval_cutoff: str,
    bundle_observed_at: str,
) -> None:
    try:
        session_dates = tuple(
            date.fromisoformat(session_date)
            for session_date, _, _, _ in observation_membership
        )
        effective_date = date.fromisoformat(effective_trading_date)
        cutoff = datetime.fromisoformat(retrieval_cutoff)
        observed_at = datetime.fromisoformat(bundle_observed_at)
    except (TypeError, ValueError) as exc:
        raise CryptoSnapshotIdentityMismatch(
            "crypto durable membership contains malformed dates"
        ) from exc
    if cutoff.tzinfo is None or observed_at.tzinfo is None or observed_at > cutoff:
        raise CryptoSnapshotIdentityMismatch(
            "crypto durable membership timestamps are contradictory"
        )
    if session_dates[-1] != effective_date or any(
        (current - previous).days != 1
        for previous, current in zip(session_dates, session_dates[1:], strict=False)
    ):
        raise CryptoSnapshotIdentityMismatch(
            "crypto durable membership is not consecutive daily history"
        )
    material_values = (
        calendar_revision_id,
        bundle_revision_id,
        *factor_revision_ids,
        *(
            value
            for membership in observation_membership
            for value in membership[1:]
        ),
    )
    if any(not isinstance(value, str) or not value.strip() for value in material_values):
        raise CryptoSnapshotIdentityMismatch(
            "crypto durable membership contains a blank material field"
        )
    searchable = " ".join(value.strip().casefold() for value in material_values)
    if any(
        marker in searchable
        for marker in (*_MAINLAND_DATASET_MARKERS, "unknown", "unresolved")
    ):
        raise CryptoSnapshotIdentityMismatch(
            "crypto durable membership contains incompatible semantics"
        )


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

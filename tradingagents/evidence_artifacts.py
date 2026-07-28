"""Immutable content-addressed persistence for exact evidence artifacts."""

from __future__ import annotations

import errno
import gzip
import os
import tempfile
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    CalculationLineage,
    ClaimValidationStatus,
    EvidenceState,
    EvidenceStatus,
    InstrumentKind,
    ProviderPhysicalAttemptEvidence,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
)

SOURCE_ARTIFACT_MANIFEST_VERSION = "1.0"
_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceArtifactManifestEntry(BaseModel):
    """Logical reference and decoding metadata for one persisted artifact."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    artifact_ref: str = Field(pattern=r"^artifact=sha256:[0-9a-f]{64}$")
    encoding: Literal["utf-8"] = "utf-8"
    compression: Literal["gzip"] = "gzip"
    byte_length: int = Field(ge=1)


class SourceArtifactManifest(BaseModel):
    """Versioned deterministic inventory of persisted source artifacts."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    artifacts: tuple[SourceArtifactManifestEntry, ...] = ()


class AuditIdentityProvenance(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    provider: str
    source_id: str = Field(pattern=r"^source=sha256:[0-9a-f]{64}$")
    retrieved_at: str
    artifact_ref: str = Field(pattern=r"^artifact=sha256:[0-9a-f]{64}$")


class AuditInstrumentIdentity(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    symbol: str
    venue: str
    instrument_kind: InstrumentKind
    currency: str
    provenance: AuditIdentityProvenance | None = None


class AuditTradingStatusProvenance(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    provider: str
    provider_dataset_id: str
    session_date: str
    status: Literal["traded", "suspended"]
    observed_at: str
    revision_id: str | None = None


class AuditMarketSnapshot(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    symbol: str
    provider: str
    retrieved_at: str
    adjustment_basis: str
    requested_date: str
    effective_trading_date: str
    history_rows: int = Field(ge=0)
    frame_sha256: str
    snapshot_id: str
    snapshot_id_version: Literal["v1", "v2"] = "v1"
    pin_membership_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    current_tradeability: Literal["unknown", "tradeable", "suspended"] = "unknown"
    current_status_provenance: AuditTradingStatusProvenance | None = None
    latest_traded_close: Decimal | None = None
    latest_traded_close_diagnostic: str | None = None
    carried_suspension_close: Decimal | None = None
    history_gap_dates: tuple[str, ...] = ()
    history_store_status: Literal["live", "stored", "degraded"] = "live"
    history_store_diagnostic: str | None = None
    physical_attempt_events: tuple[ProviderPhysicalAttemptEvidence, ...] = ()
    physical_attempt_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_authoritative_status(self) -> AuditMarketSnapshot:
        provenance = self.current_status_provenance
        if self.current_tradeability == "unknown":
            if (
                provenance is not None
                or self.latest_traded_close is not None
                or self.carried_suspension_close is not None
            ):
                raise ValueError(
                    "unknown tradeability cannot carry authoritative status values"
                )
            return self
        if provenance is None:
            raise ValueError(
                "authoritative current tradeability requires status provenance"
            )
        expected_status = (
            "suspended" if self.current_tradeability == "suspended" else "traded"
        )
        if (
            provenance.provider != self.provider
            or provenance.session_date != self.effective_trading_date
            or provenance.status != expected_status
        ):
            raise ValueError(
                "authoritative status provenance contradicts the audit snapshot"
            )
        if self.latest_traded_close is None:
            if not self.latest_traded_close_diagnostic:
                raise ValueError("unavailable latest traded close requires a diagnostic")
        elif self.latest_traded_close_diagnostic is not None:
            raise ValueError(
                "available latest traded close cannot carry an unavailable diagnostic"
            )
        if self.current_tradeability == "suspended":
            if self.carried_suspension_close is None:
                raise ValueError("suspended tradeability requires a carried close")
        else:
            if self.carried_suspension_close is not None:
                raise ValueError("tradeable status cannot carry a suspension close")
            if self.latest_traded_close is None:
                raise ValueError(
                    "tradeable status requires the latest genuinely traded close"
                )
        return self


class AuditMaterialClaim(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    claim_id: str
    analyst: str
    fact_ids: tuple[str, ...] = ()
    minimum_history_rows: int = Field(ge=1)


class AuditSourceFact(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    fact_kind: Literal["excerpt", "canonical"]
    fact_id: str = Field(pattern=r"^fact:[0-9a-f]{64}$")
    source_id: str = Field(pattern=r"^source=sha256:[0-9a-f]{64}$")
    artifact_ref: str = Field(pattern=r"^artifact=sha256:[0-9a-f]{64}$")
    source_span_start: int = Field(ge=0)
    source_span_end: int = Field(ge=0)
    normalized_numeric_tokens: tuple[str, ...] = ()
    calculation_ids: tuple[str, ...] = ()
    canonical_field: str
    normalized_value: Decimal | str | int | bool | None = None
    unit: str
    instrument_symbol: str
    effective_date: str
    calculation_lineage: CalculationLineage | None = None


class AuditClaimValidation(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    claim_id: str
    status: ClaimValidationStatus
    fact_ids: tuple[str, ...] = ()


class AuditEvidenceSource(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    source_id: str = Field(pattern=r"^source=sha256:[0-9a-f]{64}$")
    status: EvidenceStatus
    required: bool


class AuditCalculationReadiness(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    calculation_id: str
    required_observations: int = Field(ge=0)
    available_observations: int = Field(ge=0)
    input_artifact_ref: str = Field(pattern=r"^artifact=sha256:[0-9a-f]{64}$")


class AuditSourceAcquisitionAvailable(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    outcome: Literal["available"] = "available"
    provider: str
    provider_order: int = Field(ge=0)
    capability: str
    source_id: str = Field(pattern=r"^source=sha256:[0-9a-f]{64}$")
    attempt: int = Field(ge=1)
    retrieved_at: str
    retryable: Literal[False] = False
    artifact_ref: str = Field(pattern=r"^artifact=sha256:[0-9a-f]{64}$")


class AuditSourceAcquisitionUnavailable(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    outcome: Literal["unavailable"] = "unavailable"
    provider: str
    provider_order: int = Field(ge=0)
    capability: str
    source_id: str = Field(pattern=r"^source=sha256:[0-9a-f]{64}$")
    attempt: int = Field(ge=1)
    retrieved_at: str
    retryable: bool
    reason: AcquisitionUnavailableReason
    retry_after_seconds: float | None = Field(default=None, ge=0)
    http_status: int | None = Field(default=None, ge=100, le=599)
    calculation_readiness: AuditCalculationReadiness | None = None


AuditSourceAcquisitionOutcome = Annotated[
    AuditSourceAcquisitionAvailable | AuditSourceAcquisitionUnavailable,
    Field(discriminator="outcome"),
]


class AuditEvidenceProjection(BaseModel):
    """Closed payload-safe projection of evidence for immutable audit records."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = SOURCE_ARTIFACT_MANIFEST_VERSION
    instrument_identity: AuditInstrumentIdentity | None = None
    market_snapshot: AuditMarketSnapshot | None = None
    artifact_manifest: SourceArtifactManifest
    material_claims: tuple[AuditMaterialClaim, ...] = ()
    source_facts: tuple[AuditSourceFact, ...] = ()
    claim_validations: tuple[AuditClaimValidation, ...] = ()
    sources: tuple[AuditEvidenceSource, ...] = ()
    acquisition_outcomes: tuple[AuditSourceAcquisitionOutcome, ...] = ()
    physical_attempt_events: tuple[ProviderPhysicalAttemptEvidence, ...] = ()
    physical_attempt_count: int = Field(default=0, ge=0)


def _candidate_artifacts(evidence: EvidenceState) -> tuple[SourceArtifact, ...]:
    nested = tuple(
        outcome.artifact
        for outcome in evidence.acquisition_outcomes
        if isinstance(outcome, SourceAcquisitionAvailable)
    )
    return evidence.source_artifacts + nested


def _raw_bytes_by_digest(evidence: EvidenceState) -> dict[str, bytes]:
    raw_by_digest: dict[str, bytes] = {}
    for artifact in _candidate_artifacts(evidence):
        raw_bytes = artifact.raw_text.encode("utf-8")
        digest = sha256(raw_bytes).hexdigest()
        if digest != artifact.artifact_sha256:
            raise ValueError(
                "source artifact digest does not match exact UTF-8 bytes: "
                f"{artifact.artifact_sha256}"
            )
        previous = raw_by_digest.get(digest)
        if previous is not None and previous != raw_bytes:
            raise ValueError(f"conflicting source artifact bytes for sha256:{digest}")
        raw_by_digest[digest] = raw_bytes
    return raw_by_digest


def _validate_existing_target(target: Path, raw_bytes: bytes, digest: str) -> None:
    try:
        stored_raw_bytes = gzip.decompress(target.read_bytes())
    except (OSError, EOFError) as exc:
        raise FileExistsError(
            f"immutable source artifact is not valid gzip data: {target}"
        ) from exc
    if stored_raw_bytes != raw_bytes or sha256(stored_raw_bytes).hexdigest() != digest:
        raise FileExistsError(f"immutable source artifact conflicts: {target}")


def _persist_one(target: Path, raw_bytes: bytes, digest: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _validate_existing_target(target, raw_bytes, digest)
        return

    encoded = gzip.compress(raw_bytes, mtime=0)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            _validate_existing_target(target, raw_bytes, digest)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES, errno.ENOTSUP):
                raise
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                target_descriptor = os.open(target, flags, 0o600)
            except FileExistsError:
                _validate_existing_target(target, raw_bytes, digest)
            else:
                with os.fdopen(target_descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
    finally:
        temporary.unlink(missing_ok=True)


def persist_source_artifacts(
    evidence: EvidenceState,
    audit_directory: Path,
) -> SourceArtifactManifest:
    """Persist exact artifact bytes immutably and return their logical manifest."""

    raw_by_digest = _raw_bytes_by_digest(evidence)
    entries: list[SourceArtifactManifestEntry] = []
    storage_root = Path(audit_directory) / "evidence_artifacts" / "sha256"
    for digest in sorted(raw_by_digest):
        raw_bytes = raw_by_digest[digest]
        target = storage_root / digest[:2] / f"{digest}.utf8.gz"
        _persist_one(target, raw_bytes, digest)
        entries.append(
            SourceArtifactManifestEntry(
                artifact_ref=f"artifact=sha256:{digest}",
                byte_length=len(raw_bytes),
            )
        )
    return SourceArtifactManifest(artifacts=tuple(entries))


def _opaque_source_id(source_ref: str) -> str:
    return "source=sha256:" + sha256(source_ref.encode("utf-8")).hexdigest()


def _manifest_by_digest(
    manifest: SourceArtifactManifest,
) -> dict[str, SourceArtifactManifestEntry]:
    return {
        entry.artifact_ref.removeprefix("artifact=sha256:"): entry
        for entry in manifest.artifacts
    }


def _require_artifact_ref(
    entries_by_digest: dict[str, SourceArtifactManifestEntry],
    digest: str,
) -> str:
    entry = entries_by_digest.get(digest)
    if entry is None:
        raise ValueError(f"source artifact digest is absent from manifest: {digest}")
    return entry.artifact_ref


def project_evidence_for_audit(
    evidence: EvidenceState,
    manifest: SourceArtifactManifest,
) -> AuditEvidenceProjection:
    """Project safe evidence semantics without provider or model-authored prose."""

    evidence = EvidenceState.model_validate(evidence.model_dump(mode="python"))
    entries_by_digest = _manifest_by_digest(manifest)
    canonical_manifest = SourceArtifactManifest(
        artifacts=tuple(
            sorted(manifest.artifacts, key=lambda entry: entry.artifact_ref)
        )
    )

    identity = evidence.instrument_identity
    projected_identity: AuditInstrumentIdentity | None = None
    if identity is not None:
        provenance = identity.provenance
        projected_provenance = (
            AuditIdentityProvenance(
                provider=provenance.provider,
                source_id=_opaque_source_id(provenance.source_ref),
                retrieved_at=provenance.retrieved_at,
                artifact_ref=_require_artifact_ref(
                    entries_by_digest,
                    provenance.artifact_sha256,
                ),
            )
            if provenance is not None
            else None
        )
        projected_identity = AuditInstrumentIdentity(
            symbol=identity.symbol,
            venue=identity.venue,
            instrument_kind=identity.instrument_kind,
            currency=identity.currency,
            provenance=projected_provenance,
        )

    snapshot = evidence.market_snapshot
    projected_snapshot = (
        AuditMarketSnapshot(
            symbol=snapshot.symbol,
            provider=snapshot.provider,
            retrieved_at=snapshot.retrieved_at,
            adjustment_basis=snapshot.adjustment_basis,
            requested_date=snapshot.requested_date,
            effective_trading_date=snapshot.effective_trading_date,
            history_rows=snapshot.history_rows,
            frame_sha256=snapshot.frame_sha256,
            snapshot_id=snapshot.snapshot_id,
            snapshot_id_version=snapshot.snapshot_id_version,
            pin_membership_digest=snapshot.pin_membership_digest,
            current_tradeability=snapshot.current_tradeability,
            current_status_provenance=(
                AuditTradingStatusProvenance.model_validate(
                    snapshot.current_status_provenance.model_dump(mode="python")
                )
                if snapshot.current_status_provenance is not None
                else None
            ),
            latest_traded_close=snapshot.latest_traded_close,
            latest_traded_close_diagnostic=snapshot.latest_traded_close_diagnostic,
            carried_suspension_close=snapshot.carried_suspension_close,
            history_gap_dates=snapshot.history_gap_dates,
            history_store_status=snapshot.history_store_status,
            history_store_diagnostic=snapshot.history_store_diagnostic,
            physical_attempt_events=snapshot.physical_attempt_events,
            physical_attempt_count=snapshot.physical_attempt_count,
        )
        if snapshot is not None
        else None
    )

    projected_claims = tuple(
        AuditMaterialClaim(
            claim_id=claim.claim_id,
            analyst=claim.analyst,
            fact_ids=tuple(sorted(claim.fact_ids)),
            minimum_history_rows=claim.minimum_history_rows,
        )
        for claim in sorted(evidence.material_claims, key=lambda item: item.claim_id)
    )
    projected_facts = tuple(
        AuditSourceFact(
            fact_kind=fact.fact_kind,
            fact_id=fact.fact_id,
            source_id=_opaque_source_id(fact.source_ref),
            artifact_ref=_require_artifact_ref(
                entries_by_digest,
                fact.artifact_sha256,
            ),
            source_span_start=fact.source_span_start,
            source_span_end=fact.source_span_end,
            normalized_numeric_tokens=tuple(sorted(fact.normalized_numeric_tokens)),
            calculation_ids=tuple(sorted(fact.calculation_ids)),
            canonical_field=fact.canonical_field,
            normalized_value=fact.normalized_value,
            unit=fact.unit,
            instrument_symbol=fact.instrument_symbol,
            effective_date=fact.effective_date,
            calculation_lineage=fact.calculation_lineage,
        )
        for fact in sorted(evidence.source_facts, key=lambda item: item.fact_id)
    )
    projected_validations = tuple(
        AuditClaimValidation(
            claim_id=validation.claim_id,
            status=validation.status,
            fact_ids=tuple(sorted(validation.fact_ids)),
        )
        for validation in sorted(
            evidence.claim_validations,
            key=lambda item: item.claim_id,
        )
    )
    projected_sources = tuple(
        sorted(
            (
                AuditEvidenceSource(
                    source_id=_opaque_source_id(source.source_id),
                    status=source.status,
                    required=source.required,
                )
                for source in evidence.sources
            ),
            key=lambda item: (item.source_id, item.status.value, item.required),
        )
    )

    canonical_outcomes = sorted(
        evidence.acquisition_outcomes,
        key=lambda outcome: (
            outcome.capability,
            outcome.source_ref,
            outcome.provider_order,
            outcome.attempt,
            outcome.provider,
            outcome.retrieved_at,
            outcome.outcome,
        ),
    )
    projected_outcomes: list[AuditSourceAcquisitionOutcome] = []
    for outcome in canonical_outcomes:
        common = {
            "provider": outcome.provider,
            "provider_order": outcome.provider_order,
            "capability": outcome.capability,
            "source_id": _opaque_source_id(outcome.source_ref),
            "attempt": outcome.attempt,
            "retrieved_at": outcome.retrieved_at,
            "retryable": outcome.retryable,
        }
        if isinstance(outcome, SourceAcquisitionAvailable):
            projected_outcomes.append(
                AuditSourceAcquisitionAvailable(
                    **common,
                    artifact_ref=_require_artifact_ref(
                        entries_by_digest,
                        outcome.artifact.artifact_sha256,
                    ),
                )
            )
            continue
        if not isinstance(outcome, SourceAcquisitionUnavailable):
            raise TypeError(f"unsupported acquisition outcome: {type(outcome).__name__}")
        readiness = outcome.calculation_readiness
        projected_readiness = (
            AuditCalculationReadiness(
                calculation_id=readiness.calculation_id,
                required_observations=readiness.required_observations,
                available_observations=readiness.available_observations,
                input_artifact_ref=_require_artifact_ref(
                    entries_by_digest,
                    readiness.input_artifact_sha256,
                ),
            )
            if readiness is not None
            else None
        )
        projected_outcomes.append(
            AuditSourceAcquisitionUnavailable(
                **common,
                reason=outcome.reason,
                retry_after_seconds=outcome.retry_after_seconds,
                http_status=outcome.http_status,
                calculation_readiness=projected_readiness,
            )
        )

    return AuditEvidenceProjection(
        instrument_identity=projected_identity,
        market_snapshot=projected_snapshot,
        artifact_manifest=canonical_manifest,
        material_claims=projected_claims,
        source_facts=projected_facts,
        claim_validations=projected_validations,
        sources=projected_sources,
        acquisition_outcomes=tuple(projected_outcomes),
        physical_attempt_events=evidence.physical_attempt_events,
        physical_attempt_count=evidence.physical_attempt_count,
    )

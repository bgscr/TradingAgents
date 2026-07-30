"""Provider-neutral contracts for dated factors and issuer-name events."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.dataflows.provider_subrequests import ProviderSubrequestArtifactRef
from tradingagents.evidence import InstrumentIdentityEvidence
from tradingagents.market_history import ProvenanceClass

FACTOR_NAME_CONTRACT_VERSION = "1.0"

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _unordered_sequence_digest(value: object) -> str:
    if not isinstance(value, list | tuple):
        return _digest(value)
    canonical_items = sorted(value, key=_canonical_json)
    return _digest(canonical_items)


def _raw_artifact_identity(artifact: ProviderSubrequestArtifactRef) -> str:
    return f"provider-raw-artifact:v1:{_digest(artifact.model_dump(mode='json'))}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provider observation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _authoritative_identity(identity: InstrumentIdentityEvidence) -> dict[str, object]:
    if not identity.is_authoritative or identity.provenance is None:
        raise ValueError("candidate requires authoritative Instrument Identity")
    return {
        "canonical_symbol": identity.symbol.strip().upper(),
        "venue": identity.venue,
        "instrument_kind": identity.instrument_kind.value,
        "currency": identity.currency.upper(),
        "provenance": identity.provenance.model_dump(mode="json"),
    }


class ProviderDatasetIdentity(BaseModel):
    """Stable provider endpoint/dataset identity without a payload."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FACTOR_NAME_CONTRACT_VERSION
    provider_id: str = Field(pattern=_SAFE_ID_PATTERN)
    endpoint_id: str = Field(pattern=_SAFE_ID_PATTERN)
    dataset_id: str = Field(pattern=_SAFE_ID_PATTERN)
    schema_identity: str = Field(pattern=_SAFE_ID_PATTERN)
    dataset_identity: str = Field(
        pattern=r"^provider-dataset:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        endpoint_id: str,
        dataset_id: str,
        schema_identity: str,
    ) -> ProviderDatasetIdentity:
        payload = {
            "contract_version": FACTOR_NAME_CONTRACT_VERSION,
            "provider_id": provider_id,
            "endpoint_id": endpoint_id,
            "dataset_id": dataset_id,
            "schema_identity": schema_identity,
        }
        return cls(
            **payload,
            dataset_identity=f"provider-dataset:v1:{_digest(payload)}",
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> ProviderDatasetIdentity:
        payload = self.model_dump(mode="json", exclude={"dataset_identity"})
        if self.dataset_identity != f"provider-dataset:v1:{_digest(payload)}":
            raise ValueError("provider dataset identity mismatch")
        return self


class ProviderArtifactIdentity(BaseModel):
    """Exact raw binding plus row-order-neutral normalization identity."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FACTOR_NAME_CONTRACT_VERSION
    dataset: ProviderDatasetIdentity
    subrequest_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")
    raw_artifact: ProviderSubrequestArtifactRef
    raw_artifact_identity: str = Field(
        pattern=r"^provider-raw-artifact:v1:[0-9a-f]{64}$"
    )
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    retrieved_at: datetime
    observed_at: datetime
    qualification_profile: str = Field(pattern=_SAFE_ID_PATTERN)
    normalizer_version: str = Field(pattern=_SAFE_ID_PATTERN)
    artifact_identity: str = Field(
        pattern=r"^provider-artifact:v1:[0-9a-f]{64}$"
    )

    @field_validator("retrieved_at", "observed_at")
    @classmethod
    def _validate_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @classmethod
    def create(
        cls,
        *,
        dataset: ProviderDatasetIdentity,
        subrequest_key: str,
        raw_artifact: ProviderSubrequestArtifactRef,
        payload: object,
        retrieved_at: datetime,
        observed_at: datetime,
        qualification_profile: str,
        normalizer_version: str,
    ) -> ProviderArtifactIdentity:
        normalized_payload = {
            "contract_version": FACTOR_NAME_CONTRACT_VERSION,
            "dataset": dataset.model_dump(mode="json"),
            "subrequest_key": subrequest_key,
            "payload_sha256": _unordered_sequence_digest(payload),
            "retrieved_at": _utc(retrieved_at).isoformat().replace("+00:00", "Z"),
            "observed_at": _utc(observed_at).isoformat().replace("+00:00", "Z"),
            "qualification_profile": qualification_profile,
            "normalizer_version": normalizer_version,
        }
        return cls(
            **normalized_payload,
            raw_artifact=raw_artifact,
            raw_artifact_identity=_raw_artifact_identity(raw_artifact),
            artifact_identity=f"provider-artifact:v1:{_digest(normalized_payload)}",
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> ProviderArtifactIdentity:
        if self.raw_artifact_identity != _raw_artifact_identity(self.raw_artifact):
            raise ValueError("raw provider artifact identity mismatch")
        payload = self.model_dump(
            mode="json",
            exclude={"artifact_identity", "raw_artifact", "raw_artifact_identity"},
        )
        if self.artifact_identity != f"provider-artifact:v1:{_digest(payload)}":
            raise ValueError("provider normalization identity mismatch")
        return self


class ProviderRowArtifactIdentity(BaseModel):
    """Occurrence-preserving identity for one row in an immutable response."""

    model_config = _CLOSED_MODEL_CONFIG

    response_artifact_identity: str = Field(
        pattern=r"^provider-artifact:v1:[0-9a-f]{64}$"
    )
    row_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    duplicate_occurrence: int = Field(ge=0)
    row_artifact_identity: str = Field(
        pattern=r"^provider-row-artifact:v1:[0-9a-f]{64}$"
    )

    @classmethod
    def create(
        cls,
        *,
        response_artifact_identity: str,
        row_payload_sha256: str,
        duplicate_occurrence: int,
    ) -> ProviderRowArtifactIdentity:
        payload = {
            "response_artifact_identity": response_artifact_identity,
            "row_payload_sha256": row_payload_sha256,
            "duplicate_occurrence": duplicate_occurrence,
        }
        return cls(
            **payload,
            row_artifact_identity=f"provider-row-artifact:v1:{_digest(payload)}",
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> ProviderRowArtifactIdentity:
        payload = self.model_dump(mode="json", exclude={"row_artifact_identity"})
        expected = f"provider-row-artifact:v1:{_digest(payload)}"
        if self.row_artifact_identity != expected:
            raise ValueError("provider row artifact identity mismatch")
        return self


class FactorNameRejectionReason(str, Enum):
    CONFLICTING_DUPLICATE_FACTOR = "conflicting_duplicate_factor"
    INCOMPATIBLE_METADATA = "incompatible_metadata"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_DATE_INTERVAL = "invalid_date_interval"


class AdjustmentFactorRowRejection(BaseModel):
    """Typed normalization rejection that retains its immutable row binding."""

    model_config = _CLOSED_MODEL_CONFIG

    reason: FactorNameRejectionReason
    row_artifact_identity: str = Field(
        pattern=r"^provider-row-artifact:v1:[0-9a-f]{64}$"
    )
    dataset_identity: str = Field(pattern=r"^provider-dataset:v1:[0-9a-f]{64}$")
    artifact_identity: str = Field(pattern=r"^provider-artifact:v1:[0-9a-f]{64}$")
    trade_date: date | None = None


class AdjustmentFactorCandidate(BaseModel):
    """Native dated factor candidate with explicit strict-history limitations."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FACTOR_NAME_CONTRACT_VERSION
    instrument_identity: InstrumentIdentityEvidence
    canonical_symbol: str
    trade_date: date
    adj_factor: Decimal
    original_adj_factor: str
    dataset_identity: str = Field(pattern=r"^provider-dataset:v1:[0-9a-f]{64}$")
    artifact_identity: str = Field(pattern=r"^provider-artifact:v1:[0-9a-f]{64}$")
    raw_artifact: ProviderSubrequestArtifactRef
    raw_artifact_identity: str = Field(
        pattern=r"^provider-raw-artifact:v1:[0-9a-f]{64}$"
    )
    occurrence_artifact_identities: tuple[str, ...] = Field(min_length=1)
    retrieved_at: datetime
    observed_at: datetime
    qualification_profile: str = Field(pattern=_SAFE_ID_PATTERN)
    normalizer_version: str = Field(pattern=_SAFE_ID_PATTERN)
    provenance_class: Literal[ProvenanceClass.RETROSPECTIVE_BACKFILL] = (
        ProvenanceClass.RETROSPECTIVE_BACKFILL
    )
    first_observed_at: datetime
    revision_identity: str = Field(
        pattern=r"^adjustment-factor-candidate:v1:[0-9a-f]{64}$"
    )
    native_adjustment_factor_revision: Literal[True] = True
    qualified_current_factor_fallback_capable: Literal[True] = True
    raw_observation_history_established: Literal[False] = False
    session_trading_status_established: Literal[False] = False
    provider_history_bundle_complete: Literal[False] = False
    strict_replay_eligible: Literal[False] = False
    source_fact_created: Literal[False] = False
    decision_ready_evidence_created: Literal[False] = False

    @field_validator("retrieved_at", "observed_at", "first_observed_at")
    @classmethod
    def _validate_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @classmethod
    def create(
        cls,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        trade_date: date,
        adj_factor: Decimal,
        original_adj_factor: str,
        artifact: ProviderArtifactIdentity,
        occurrence_artifact_identities: tuple[str, ...],
    ) -> AdjustmentFactorCandidate:
        canonical_occurrences = tuple(sorted(occurrence_artifact_identities))
        payload = {
            "contract_version": FACTOR_NAME_CONTRACT_VERSION,
            "instrument_identity": _authoritative_identity(instrument_identity),
            "canonical_symbol": instrument_identity.symbol.strip().upper(),
            "trade_date": trade_date.isoformat(),
            "adj_factor": str(adj_factor),
            "original_adj_factor": original_adj_factor,
            "dataset_identity": artifact.dataset.dataset_identity,
            "artifact_identity": artifact.artifact_identity,
            "occurrence_artifact_identities": canonical_occurrences,
            "retrieved_at": artifact.retrieved_at.isoformat().replace("+00:00", "Z"),
            "observed_at": artifact.observed_at.isoformat().replace("+00:00", "Z"),
            "qualification_profile": artifact.qualification_profile,
            "normalizer_version": artifact.normalizer_version,
            "provenance_class": ProvenanceClass.RETROSPECTIVE_BACKFILL.value,
            "first_observed_at": artifact.observed_at.isoformat().replace("+00:00", "Z"),
        }
        return cls(
            instrument_identity=instrument_identity,
            raw_artifact=artifact.raw_artifact,
            raw_artifact_identity=artifact.raw_artifact_identity,
            **{key: value for key, value in payload.items() if key != "instrument_identity"},
            revision_identity=f"adjustment-factor-candidate:v1:{_digest(payload)}",
        )

    @model_validator(mode="after")
    def _validate_candidate(self) -> AdjustmentFactorCandidate:
        if not self.adj_factor.is_finite() or self.adj_factor <= 0:
            raise ValueError("adjustment factor must be a positive finite Decimal")
        if self.canonical_symbol != self.instrument_identity.symbol.strip().upper():
            raise ValueError("factor symbol contradicts Instrument Identity")
        if self.occurrence_artifact_identities != tuple(
            sorted(set(self.occurrence_artifact_identities))
        ):
            raise ValueError("factor occurrence evidence must be canonical and unique")
        if self.raw_artifact_identity != _raw_artifact_identity(self.raw_artifact):
            raise ValueError("factor raw provider artifact identity mismatch")
        if self.first_observed_at != self.observed_at:
            raise ValueError("factor first-observed time contradicts provider observation")
        payload = {
            "contract_version": FACTOR_NAME_CONTRACT_VERSION,
            "instrument_identity": _authoritative_identity(self.instrument_identity),
            "canonical_symbol": self.canonical_symbol,
            "trade_date": self.trade_date.isoformat(),
            "adj_factor": str(self.adj_factor),
            "original_adj_factor": self.original_adj_factor,
            "dataset_identity": self.dataset_identity,
            "artifact_identity": self.artifact_identity,
            "occurrence_artifact_identities": self.occurrence_artifact_identities,
            "retrieved_at": self.retrieved_at.isoformat().replace("+00:00", "Z"),
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "qualification_profile": self.qualification_profile,
            "normalizer_version": self.normalizer_version,
            "provenance_class": self.provenance_class.value,
            "first_observed_at": self.first_observed_at.isoformat().replace(
                "+00:00", "Z"
            ),
        }
        expected = f"adjustment-factor-candidate:v1:{_digest(payload)}"
        if self.revision_identity != expected:
            raise ValueError("adjustment factor revision identity mismatch")
        return self


class IssuerNameEventCandidate(BaseModel):
    """Dated issuer-name event that carries no status or lifecycle authority."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FACTOR_NAME_CONTRACT_VERSION
    instrument_identity: InstrumentIdentityEvidence
    canonical_symbol: str
    start_date: date
    end_date: date | None = None
    name: str = Field(min_length=1)
    change_reason: str | None = None
    dataset_identity: str = Field(pattern=r"^provider-dataset:v1:[0-9a-f]{64}$")
    artifact_identity: str = Field(pattern=r"^provider-artifact:v1:[0-9a-f]{64}$")
    raw_artifact: ProviderSubrequestArtifactRef
    raw_artifact_identity: str = Field(
        pattern=r"^provider-raw-artifact:v1:[0-9a-f]{64}$"
    )
    row_artifact_identity: str = Field(
        pattern=r"^provider-row-artifact:v1:[0-9a-f]{64}$"
    )
    retrieved_at: datetime
    observed_at: datetime
    qualification_profile: str = Field(pattern=_SAFE_ID_PATTERN)
    normalizer_version: str = Field(pattern=_SAFE_ID_PATTERN)
    revision_identity: str = Field(
        pattern=r"^issuer-name-event-candidate:v1:[0-9a-f]{64}$"
    )
    st_status_established: Literal[False] = False
    suspension_status_established: Literal[False] = False
    current_tradeability_established: Literal[False] = False
    listing_status_established: Literal[False] = False
    delisting_status_established: Literal[False] = False
    source_fact_created: Literal[False] = False
    decision_ready_evidence_created: Literal[False] = False

    @field_validator("retrieved_at", "observed_at")
    @classmethod
    def _validate_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @classmethod
    def create(
        cls,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        start_date: date,
        end_date: date | None,
        name: str,
        change_reason: str | None,
        artifact: ProviderArtifactIdentity,
        row_artifact_identity: str,
    ) -> IssuerNameEventCandidate:
        payload = {
            "contract_version": FACTOR_NAME_CONTRACT_VERSION,
            "instrument_identity": _authoritative_identity(instrument_identity),
            "canonical_symbol": instrument_identity.symbol.strip().upper(),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat() if end_date is not None else None,
            "name": name,
            "change_reason": change_reason,
            "dataset_identity": artifact.dataset.dataset_identity,
            "artifact_identity": artifact.artifact_identity,
            "row_artifact_identity": row_artifact_identity,
            "retrieved_at": artifact.retrieved_at.isoformat().replace("+00:00", "Z"),
            "observed_at": artifact.observed_at.isoformat().replace("+00:00", "Z"),
            "qualification_profile": artifact.qualification_profile,
            "normalizer_version": artifact.normalizer_version,
        }
        return cls(
            instrument_identity=instrument_identity,
            raw_artifact=artifact.raw_artifact,
            raw_artifact_identity=artifact.raw_artifact_identity,
            **{key: value for key, value in payload.items() if key != "instrument_identity"},
            revision_identity=f"issuer-name-event-candidate:v1:{_digest(payload)}",
        )

    @model_validator(mode="after")
    def _validate_candidate(self) -> IssuerNameEventCandidate:
        if self.canonical_symbol != self.instrument_identity.symbol.strip().upper():
            raise ValueError("name-event symbol contradicts Instrument Identity")
        if not self.name.strip():
            raise ValueError("issuer name must not be blank")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("issuer name interval is reversed")
        if self.raw_artifact_identity != _raw_artifact_identity(self.raw_artifact):
            raise ValueError("name-event raw provider artifact identity mismatch")
        payload = {
            "contract_version": FACTOR_NAME_CONTRACT_VERSION,
            "instrument_identity": _authoritative_identity(self.instrument_identity),
            "canonical_symbol": self.canonical_symbol,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat() if self.end_date is not None else None,
            "name": self.name,
            "change_reason": self.change_reason,
            "dataset_identity": self.dataset_identity,
            "artifact_identity": self.artifact_identity,
            "row_artifact_identity": self.row_artifact_identity,
            "retrieved_at": self.retrieved_at.isoformat().replace("+00:00", "Z"),
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "qualification_profile": self.qualification_profile,
            "normalizer_version": self.normalizer_version,
        }
        expected = f"issuer-name-event-candidate:v1:{_digest(payload)}"
        if self.revision_identity != expected:
            raise ValueError("issuer name-event revision identity mismatch")
        return self


class IssuerNameEventRowRejection(BaseModel):
    """Typed name normalization rejection retaining immutable row evidence."""

    model_config = _CLOSED_MODEL_CONFIG

    reason: FactorNameRejectionReason
    row_artifact_identity: str = Field(
        pattern=r"^provider-row-artifact:v1:[0-9a-f]{64}$"
    )
    dataset_identity: str = Field(pattern=r"^provider-dataset:v1:[0-9a-f]{64}$")
    artifact_identity: str = Field(pattern=r"^provider-artifact:v1:[0-9a-f]{64}$")
    start_date: date | None = None
    end_date: date | None = None


__all__ = [
    "FACTOR_NAME_CONTRACT_VERSION",
    "AdjustmentFactorCandidate",
    "AdjustmentFactorRowRejection",
    "FactorNameRejectionReason",
    "IssuerNameEventCandidate",
    "IssuerNameEventRowRejection",
    "ProviderArtifactIdentity",
    "ProviderDatasetIdentity",
    "ProviderRowArtifactIdentity",
]

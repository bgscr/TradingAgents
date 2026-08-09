"""Checkpoint-safe coordination for exact provider subrequests."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind
from tradingagents.market_history.config import DataUsageMode
from tradingagents.market_history.coordinator import (
    PhysicalAttemptBudgetExhausted,
    PhysicalAttemptFailure,
    PhysicalAttemptNotMade,
    PhysicalAttemptOutcome,
    ProviderPhysicalAttemptEvent,
    ProviderRequestCoordinator,
    RequestPriority,
    upstream_service_identity_for_provider,
)

PROVIDER_SUBREQUEST_KEY_VERSION = "1.0"
PROVIDER_SUBREQUEST_CACHE_CONTEXT_VERSION = "1.1"
FINANCIAL_ELIGIBILITY_CONTEXT_CONTRACT_VERSION = (
    "financial-eligibility-acquisition-context-v1"
)

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:=/-]{0,255}$"
_SAFE_CAPACITY_SCOPE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_SAFE_FIELD_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"
_SECRET_MARKERS = (
    "api-key",
    "api_key",
    "apikey",
    "credential",
    "password",
    "secret",
    "token",
)


class ProviderSubrequestCacheFailureReason(str, Enum):
    ARTIFACT_CORRUPT = "provider_subrequest_artifact_corrupt"
    EXECUTION_INVALID = "provider_subrequest_execution_invalid"
    MALFORMED_CHECKPOINT = "provider_subrequest_checkpoint_malformed"
    TERMINAL_CONTRADICTION = "provider_subrequest_terminal_contradiction"
    UNSAFE_CHECKPOINT_BOUNDARY = "provider_subrequest_checkpoint_unsafe_boundary"
    UNSAFE_CACHE_IDENTITY = "provider_subrequest_cache_identity_unsafe"
    UPSTREAM_IDENTITY_MISMATCH = "provider_subrequest_upstream_identity_mismatch"
    RUN_SCOPE_MISMATCH = "provider_subrequest_run_scope_mismatch"
    CONTEXT_MISMATCH = "provider_subrequest_context_mismatch"


class ProviderSubrequestCacheError(ValueError):
    """Typed payload-free failure raised before any fallback provider work."""

    def __init__(self, reason: ProviderSubrequestCacheFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _contains_reserved_secret_text(values: tuple[str, ...]) -> bool:
    return any(
        not isinstance(value, str)
        or any(marker in value.casefold() for marker in _SECRET_MARKERS)
        for value in values
    )


def _upstream_identity_matches_account(
    *,
    provider_id: str,
    upstream_service_id: str,
    account_scope: str,
) -> bool:
    if provider_id != "tushare":
        return True
    expected_identity, _ = upstream_service_identity_for_provider(
        provider_id,
        account_scope=account_scope,
    )
    return upstream_service_id == expected_identity


class ProviderSubrequestInstrumentIdentity(BaseModel):
    """Safe authoritative identity projection used only for request identity."""

    model_config = _CLOSED_MODEL_CONFIG

    canonical_symbol: str = Field(min_length=1, max_length=64)
    venue: str = Field(pattern=_SAFE_ID_PATTERN)
    instrument_kind: InstrumentKind
    currency: str = Field(pattern=r"^[A-Z][A-Z0-9]{2,7}$")
    provenance_provider_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_source_ref_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_authoritative(
        cls,
        identity: InstrumentIdentityEvidence,
    ) -> ProviderSubrequestInstrumentIdentity:
        if not identity.is_authoritative or identity.provenance is None:
            raise ValueError(
                "provider subrequest requires authoritative Instrument Identity"
            )
        provenance = identity.provenance
        payload = {
            "canonical_symbol": identity.symbol.strip().upper(),
            "venue": identity.venue.strip(),
            "instrument_kind": identity.instrument_kind.value,
            "currency": identity.currency.strip().upper(),
            "provenance_provider_sha256": sha256(
                provenance.provider.encode("utf-8")
            ).hexdigest(),
            "provenance_source_ref_sha256": sha256(
                provenance.source_ref.encode("utf-8")
            ).hexdigest(),
            "provenance_artifact_sha256": provenance.artifact_sha256,
        }
        return cls(
            **payload,
            identity_revision=sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
        )


class ProviderSubrequestKey(BaseModel):
    """Canonical cache and coordinator identity for one physical subrequest."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = PROVIDER_SUBREQUEST_KEY_VERSION
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    upstream_service_id: str = Field(pattern=_SAFE_ID_PATTERN)
    account_scope: str = Field(pattern=_SAFE_ID_PATTERN)
    capacity_scope: str = Field(pattern=_SAFE_CAPACITY_SCOPE_PATTERN)
    instrument_identity: ProviderSubrequestInstrumentIdentity
    canonical_symbol: str = Field(min_length=1, max_length=64)
    requested_range_start: str = Field(pattern=_SAFE_ID_PATTERN)
    requested_range_end: str = Field(pattern=_SAFE_ID_PATTERN)
    requested_fields: tuple[str, ...]
    as_of_date: date
    qualification_profile: str = Field(pattern=_SAFE_ID_PATTERN)
    normalizer_version: str = Field(pattern=_SAFE_ID_PATTERN)
    data_usage_mode: DataUsageMode
    subrequest_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        upstream_service_id: str,
        account_scope: str,
        capacity_scope: str,
        instrument_identity: InstrumentIdentityEvidence,
        requested_range_start: str,
        requested_range_end: str,
        requested_fields: tuple[str, ...],
        as_of_date: date,
        qualification_profile: str,
        normalizer_version: str,
        data_usage_mode: DataUsageMode,
    ) -> ProviderSubrequestKey:
        identity_tokens = (
            upstream_service_id,
            account_scope,
            capacity_scope,
            requested_range_start,
            requested_range_end,
            *requested_fields,
            qualification_profile,
            normalizer_version,
        )
        if _contains_reserved_secret_text(identity_tokens):
            raise ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.UNSAFE_CACHE_IDENTITY
            )
        if not _upstream_identity_matches_account(
            provider_id=provider_id,
            upstream_service_id=upstream_service_id,
            account_scope=account_scope,
        ):
            raise ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.UPSTREAM_IDENTITY_MISMATCH
            )
        identity = ProviderSubrequestInstrumentIdentity.from_authoritative(
            instrument_identity
        )
        canonical_fields = tuple(sorted(set(requested_fields)))
        payload = {
            "contract_version": PROVIDER_SUBREQUEST_KEY_VERSION,
            "provider_id": provider_id,
            "upstream_service_id": upstream_service_id,
            "account_scope": account_scope,
            "capacity_scope": capacity_scope,
            "instrument_identity": identity.model_dump(mode="json"),
            "canonical_symbol": identity.canonical_symbol,
            "requested_range_start": requested_range_start,
            "requested_range_end": requested_range_end,
            "requested_fields": canonical_fields,
            "as_of_date": as_of_date.isoformat(),
            "qualification_profile": qualification_profile,
            "normalizer_version": normalizer_version,
            "data_usage_mode": data_usage_mode.value,
        }
        digest = sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        return cls(
            **payload,
            subrequest_key=f"provider-subrequest:v1:{digest}",
        )

    @model_validator(mode="after")
    def _validate_canonical_key(self) -> ProviderSubrequestKey:
        if self.canonical_symbol != self.instrument_identity.canonical_symbol:
            raise ValueError("canonical symbol contradicts Instrument Identity")
        if self.requested_fields != tuple(sorted(set(self.requested_fields))):
            raise ValueError("requested field set must be canonical")
        if any(
            not isinstance(field, str)
            or re.fullmatch(_SAFE_FIELD_PATTERN, field) is None
            for field in self.requested_fields
        ):
            raise ValueError("requested field set contains an unsafe field ID")
        identity_tokens = (
            self.upstream_service_id,
            self.account_scope,
            self.capacity_scope,
            self.requested_range_start,
            self.requested_range_end,
            *self.requested_fields,
            self.qualification_profile,
            self.normalizer_version,
        )
        if _contains_reserved_secret_text(identity_tokens):
            raise ValueError("provider subrequest identity contains reserved secret text")
        if not _upstream_identity_matches_account(
            provider_id=self.provider_id,
            upstream_service_id=self.upstream_service_id,
            account_scope=self.account_scope,
        ):
            raise ValueError("provider subrequest upstream identity mismatch")
        payload = self.model_dump(mode="json", exclude={"subrequest_key"})
        expected = "provider-subrequest:v1:" + sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
        if self.subrequest_key != expected:
            raise ValueError("provider subrequest key mismatch")
        return self


class ProviderSubrequestArtifactRef(BaseModel):
    """Content-addressed artifact reference safe for checkpoint persistence."""

    model_config = _CLOSED_MODEL_CONFIG

    artifact_ref: str = Field(pattern=r"^provider-subrequest-artifact=sha256:[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    media_type: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+/-]{0,127}$")

    @model_validator(mode="after")
    def _validate_ref(self) -> ProviderSubrequestArtifactRef:
        if self.artifact_ref != (
            f"provider-subrequest-artifact=sha256:{self.artifact_sha256}"
        ):
            raise ValueError("provider subrequest artifact reference mismatch")
        return self


class ProviderSubrequestArtifactStore:
    """Immutable local artifact boundary; paths never enter cache identity."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    def install(self, payload: bytes, *, media_type: str) -> ProviderSubrequestArtifactRef:
        if not isinstance(payload, bytes):
            raise TypeError("provider subrequest payload must be bytes")
        digest = sha256(payload).hexdigest()
        artifact = ProviderSubrequestArtifactRef(
            artifact_ref=f"provider-subrequest-artifact=sha256:{digest}",
            artifact_sha256=digest,
            byte_length=len(payload),
            media_type=media_type,
        )
        target = self._artifact_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
        except FileExistsError:
            self.read(artifact)
        else:
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        return artifact

    def read(self, artifact: ProviderSubrequestArtifactRef) -> bytes:
        try:
            payload = self._artifact_path(artifact.artifact_sha256).read_bytes()
        except OSError:
            payload = None
        if payload is None:
            raise ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.ARTIFACT_CORRUPT
            )
        if (
            len(payload) != artifact.byte_length
            or sha256(payload).hexdigest() != artifact.artifact_sha256
        ):
            raise ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.ARTIFACT_CORRUPT
            )
        return payload

    def _artifact_path(self, digest: str) -> Path:
        return self._root / "sha256" / digest[:2] / f"{digest}.bin"


class ProviderSubrequestOutcomeKind(str, Enum):
    AVAILABLE = "available"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    EMPTY = "empty"
    MALFORMED = "malformed"
    TIMEOUT = "timeout"
    DISCONNECT = "disconnect"
    PROVIDER_ERROR = "provider_error"
    UPSTREAM_BUSY = "upstream_busy"
    ABANDONED = "abandoned"


class ProviderSubrequestOutcome(BaseModel):
    """Bounded typed terminal outcome without provider exception text."""

    model_config = _CLOSED_MODEL_CONFIG

    kind: ProviderSubrequestOutcomeKind
    retryable: bool = False
    status_code: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ProviderSubrequestAttemptEvent(BaseModel):
    """Secret-safe physical-attempt projection retained by the cache."""

    model_config = _CLOSED_MODEL_CONFIG

    attempt_event_id: str = Field(
        pattern=r"^provider-physical-attempt=sha256:[0-9a-f]{64}$"
    )
    sequence_id: str = Field(
        pattern=r"^provider-request-sequence=sha256:[0-9a-f]{64}$"
    )
    request_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")
    upstream_service_id: str = Field(pattern=_SAFE_ID_PATTERN)
    capacity_scope: str = Field(pattern=_SAFE_CAPACITY_SCOPE_PATTERN)
    attempt_index: int = Field(ge=1)
    attempted_at: datetime
    pacing_event: Literal["permit_acquired", "paced_then_permit_acquired"]
    pacing_wait_seconds: float = Field(ge=0, allow_inf_nan=False)
    outcome: ProviderSubrequestOutcomeKind
    retryable: bool
    status_code: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cooldown_changed: bool
    cooldown_until: datetime | None
    final_physical_attempt_count: int | None = Field(default=None, ge=1)

    @classmethod
    def from_coordinator(
        cls,
        event: ProviderPhysicalAttemptEvent,
    ) -> ProviderSubrequestAttemptEvent:
        return cls(
            attempt_event_id=event.attempt_event_id,
            sequence_id=event.sequence_id,
            request_key=event.request_key,
            upstream_service_id=event.upstream_service_id,
            capacity_scope=event.capacity_scope,
            attempt_index=event.attempt_index,
            attempted_at=event.attempted_at,
            pacing_event=event.pacing_event,
            pacing_wait_seconds=event.pacing_wait_seconds,
            outcome=_outcome_from_physical_failure(event.outcome),
            retryable=event.retryable,
            status_code=event.status_code,
            retry_after_seconds=event.retry_after_seconds,
            cooldown_changed=event.cooldown_changed,
            cooldown_until=event.cooldown_until,
            final_physical_attempt_count=event.final_physical_attempt_count,
        )


@dataclass(frozen=True)
class ProviderSubrequestResult:
    key: ProviderSubrequestKey
    value: bytes | None
    artifact: ProviderSubrequestArtifactRef | None
    outcome: ProviderSubrequestOutcome
    sequence_id: str
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]

    def __post_init__(self) -> None:
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.value is not None and self.artifact is not None):
            raise ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.TERMINAL_CONTRADICTION
            )


class ProviderSubrequestCheckpointEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    key: ProviderSubrequestKey
    artifact: ProviderSubrequestArtifactRef | None
    outcome: ProviderSubrequestOutcome
    sequence_id: str = Field(
        pattern=(
            r"^(?:|provider-request-sequence=sha256:[0-9a-f]{64})$"
        )
    )
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]

    @model_validator(mode="after")
    def _validate_terminal_binding(self) -> ProviderSubrequestCheckpointEntry:
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.artifact is not None):
            raise ValueError("provider subrequest terminal artifact contradicts outcome")
        if not self.attempt_events:
            no_attempt_kinds = {
                ProviderSubrequestOutcomeKind.AVAILABLE,
                ProviderSubrequestOutcomeKind.RATE_LIMITED,
                ProviderSubrequestOutcomeKind.UPSTREAM_BUSY,
                ProviderSubrequestOutcomeKind.ABANDONED,
            }
            if self.outcome.kind not in no_attempt_kinds:
                raise ValueError("provider subrequest terminal progress is incomplete")
            if self.sequence_id == "" and available:
                raise ValueError("provider subrequest sequence progress is incomplete")
            if (
                self.outcome.kind is ProviderSubrequestOutcomeKind.RATE_LIMITED
                and self.sequence_id != ""
            ):
                raise ValueError("provider subrequest sequence progress is invalid")
            expected_retryable = self.outcome.kind in {
                ProviderSubrequestOutcomeKind.RATE_LIMITED,
                ProviderSubrequestOutcomeKind.UPSTREAM_BUSY,
            }
            if (
                self.outcome.retryable is not expected_retryable
                or self.outcome.status_code is not None
                or (
                    self.outcome.kind
                    in {
                        ProviderSubrequestOutcomeKind.AVAILABLE,
                        ProviderSubrequestOutcomeKind.ABANDONED,
                    }
                    and self.outcome.retry_after_seconds is not None
                )
            ):
                raise ValueError("provider subrequest no-attempt outcome is invalid")
        attempt_ids = tuple(event.attempt_event_id for event in self.attempt_events)
        attempt_indices = tuple(event.attempt_index for event in self.attempt_events)
        attempted_at = tuple(event.attempted_at for event in self.attempt_events)
        if (
            len(attempt_ids) != len(set(attempt_ids))
            or attempt_indices != tuple(range(1, len(self.attempt_events) + 1))
            or attempted_at != tuple(sorted(attempted_at))
        ):
            raise ValueError("provider subrequest attempt progress is invalid")
        for event in self.attempt_events:
            digest = sha256()
            for component in (
                "provider-physical-attempt",
                self.sequence_id,
                str(event.attempt_index),
            ):
                encoded = component.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            expected_attempt_id = (
                f"provider-physical-attempt=sha256:{digest.hexdigest()}"
            )
            if (
                event.attempt_event_id != expected_attempt_id
                or event.sequence_id != self.sequence_id
                or event.request_key != self.key.subrequest_key
                or event.upstream_service_id != self.key.upstream_service_id
                or event.capacity_scope != self.key.capacity_scope
                or event.final_physical_attempt_count != len(self.attempt_events)
            ):
                raise ValueError("provider subrequest attempt contradicts cache key")
        if any(
            event.outcome is ProviderSubrequestOutcomeKind.AVAILABLE
            or not event.retryable
            for event in self.attempt_events[:-1]
        ):
            raise ValueError("provider subrequest retry progress is invalid")
        if self.attempt_events:
            terminal = self.attempt_events[-1]
            if (
                terminal.outcome is not self.outcome.kind
                or terminal.retryable is not self.outcome.retryable
                or terminal.status_code != self.outcome.status_code
                or terminal.retry_after_seconds != self.outcome.retry_after_seconds
            ):
                raise ValueError(
                    "provider subrequest attempt contradicts terminal outcome"
                )
        return self


class ProviderSubrequestCacheCheckpoint(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0", "1.1"] = "1.0"
    run_scope_id: str = Field(pattern=_SAFE_ID_PATTERN)
    financial_eligibility_context_contract_version: Literal[
        "financial-eligibility-acquisition-context-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_context_identity: str | None = Field(
        default=None,
        pattern=r"^financial-eligibility-acquisition-context:v1:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    entries: tuple[ProviderSubrequestCheckpointEntry, ...] = ()

    @model_validator(mode="after")
    def _validate_run_scope(self) -> ProviderSubrequestCacheCheckpoint:
        if _contains_reserved_secret_text((self.run_scope_id,)):
            raise ValueError("provider subrequest run scope is unsafe")
        has_context_binding = (
            self.financial_eligibility_context_contract_version is not None
            and self.financial_eligibility_context_identity is not None
        )
        if (
            self.contract_version == PROVIDER_SUBREQUEST_CACHE_CONTEXT_VERSION
        ) != has_context_binding:
            raise ValueError("provider subrequest context binding is incomplete")
        return self


def _outcome_from_physical_failure(
    outcome: PhysicalAttemptOutcome,
) -> ProviderSubrequestOutcomeKind:
    return {
        PhysicalAttemptOutcome.AVAILABLE: ProviderSubrequestOutcomeKind.AVAILABLE,
        PhysicalAttemptOutcome.RATE_LIMITED: ProviderSubrequestOutcomeKind.RATE_LIMITED,
        PhysicalAttemptOutcome.PERMISSION_DENIED: (
            ProviderSubrequestOutcomeKind.PERMISSION_DENIED
        ),
        PhysicalAttemptOutcome.AUTHENTICATION: (
            ProviderSubrequestOutcomeKind.AUTHENTICATION
        ),
        PhysicalAttemptOutcome.EMPTY_FRAME: ProviderSubrequestOutcomeKind.EMPTY,
        PhysicalAttemptOutcome.MALFORMED_RESPONSE: (
            ProviderSubrequestOutcomeKind.MALFORMED
        ),
        PhysicalAttemptOutcome.TIMEOUT: ProviderSubrequestOutcomeKind.TIMEOUT,
        PhysicalAttemptOutcome.DISCONNECT: ProviderSubrequestOutcomeKind.DISCONNECT,
        PhysicalAttemptOutcome.UPSTREAM_BUSY: ProviderSubrequestOutcomeKind.UPSTREAM_BUSY,
        PhysicalAttemptOutcome.ABANDONED: ProviderSubrequestOutcomeKind.ABANDONED,
    }.get(outcome, ProviderSubrequestOutcomeKind.PROVIDER_ERROR)


class ProviderSubrequestCache:
    """Run-scoped single-flight cache backed by immutable artifact references."""

    def __init__(
        self,
        *,
        coordinator: ProviderRequestCoordinator,
        artifact_store: ProviderSubrequestArtifactStore,
        run_scope_id: str,
        checkpoint: Mapping[str, object] | ProviderSubrequestCacheCheckpoint | None = None,
        stop_signal: threading.Event | None = None,
    ) -> None:
        if (
            not isinstance(run_scope_id, str)
            or re.fullmatch(_SAFE_ID_PATTERN, run_scope_id) is None
            or _contains_reserved_secret_text((run_scope_id,))
        ):
            raise ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.UNSAFE_CACHE_IDENTITY
            )
        self._coordinator = coordinator
        self._artifact_store = artifact_store
        self._run_scope_id = run_scope_id
        self._stop_signal = stop_signal or threading.Event()
        self._lock = threading.Lock()
        self._results: dict[str, Future[ProviderSubrequestResult]] = {}
        self._keys: dict[str, ProviderSubrequestKey] = {}
        self._financial_eligibility_context_contract_version: str | None = None
        self._financial_eligibility_context_identity: str | None = None
        self._interrupted = False
        self._closed = False
        if checkpoint is not None:
            self.restore_checkpoint(checkpoint)

    def is_bound_to(
        self,
        *,
        coordinator: ProviderRequestCoordinator,
        artifact_store: ProviderSubrequestArtifactStore,
    ) -> bool:
        """Report exact run-authority sharing without exposing local paths."""

        return (
            self._coordinator is coordinator
            and self._artifact_store is artifact_store
        )

    def bind_financial_eligibility_context(
        self,
        *,
        context_contract_version: str,
        context_identity: str,
    ) -> None:
        """Single-assign the safe context identity governing this run cache."""

        if (
            context_contract_version
            != FINANCIAL_ELIGIBILITY_CONTEXT_CONTRACT_VERSION
            or re.fullmatch(
                r"financial-eligibility-acquisition-context:v1:[0-9a-f]{64}",
                context_identity,
            )
            is None
            or _contains_reserved_secret_text(
                (context_contract_version, context_identity)
            )
        ):
            raise ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.CONTEXT_MISMATCH
            )
        with self._lock:
            if self._closed or self._interrupted or self._stop_signal.is_set():
                raise ProviderSubrequestCacheError(
                    ProviderSubrequestCacheFailureReason.UNSAFE_CHECKPOINT_BOUNDARY
                )
            existing = (
                self._financial_eligibility_context_contract_version,
                self._financial_eligibility_context_identity,
            )
            requested = (context_contract_version, context_identity)
            if existing != (None, None) and existing != requested:
                raise ProviderSubrequestCacheError(
                    ProviderSubrequestCacheFailureReason.CONTEXT_MISMATCH
                )
            (
                self._financial_eligibility_context_contract_version,
                self._financial_eligibility_context_identity,
            ) = requested

    def checkpoint(self) -> dict[str, object]:
        entries: list[ProviderSubrequestCheckpointEntry] = []
        with self._lock:
            for subrequest_key, shared in sorted(self._results.items()):
                if not shared.done():
                    raise ProviderSubrequestCacheError(
                        ProviderSubrequestCacheFailureReason.UNSAFE_CHECKPOINT_BOUNDARY
                    )
                try:
                    result = shared.result()
                except BaseException as exc:
                    raise ProviderSubrequestCacheError(
                        ProviderSubrequestCacheFailureReason.UNSAFE_CHECKPOINT_BOUNDARY
                    ) from exc
                if result.key.subrequest_key != subrequest_key:
                    raise ProviderSubrequestCacheError(
                        ProviderSubrequestCacheFailureReason.TERMINAL_CONTRADICTION
                    )
                entries.append(
                    ProviderSubrequestCheckpointEntry(
                        key=result.key,
                        artifact=result.artifact,
                        outcome=result.outcome,
                        sequence_id=result.sequence_id,
                        attempt_events=result.attempt_events,
                    )
                )
            context_contract_version = (
                self._financial_eligibility_context_contract_version
            )
            context_identity = self._financial_eligibility_context_identity
        return ProviderSubrequestCacheCheckpoint(
            contract_version=(
                PROVIDER_SUBREQUEST_CACHE_CONTEXT_VERSION
                if context_identity is not None
                else "1.0"
            ),
            run_scope_id=self._run_scope_id,
            financial_eligibility_context_contract_version=(
                context_contract_version
            ),
            financial_eligibility_context_identity=context_identity,
            entries=tuple(entries),
        ).model_dump(mode="json")

    def restore_checkpoint(
        self,
        checkpoint: Mapping[str, object] | ProviderSubrequestCacheCheckpoint,
    ) -> None:
        """Restore one complete terminal projection before the cache is callable."""

        with self._lock:
            if (
                self._closed
                or self._interrupted
                or self._stop_signal.is_set()
                or self._results
                or self._keys
            ):
                raise ProviderSubrequestCacheError(
                    ProviderSubrequestCacheFailureReason.UNSAFE_CHECKPOINT_BOUNDARY
                )
            try:
                restored = ProviderSubrequestCacheCheckpoint.model_validate(checkpoint)
            except (TypeError, ValueError) as exc:
                raise ProviderSubrequestCacheError(
                    ProviderSubrequestCacheFailureReason.MALFORMED_CHECKPOINT
                ) from exc
            if restored.run_scope_id != self._run_scope_id:
                raise ProviderSubrequestCacheError(
                    ProviderSubrequestCacheFailureReason.RUN_SCOPE_MISMATCH
                )
            expected_context = (
                self._financial_eligibility_context_contract_version,
                self._financial_eligibility_context_identity,
            )
            restored_context = (
                restored.financial_eligibility_context_contract_version,
                restored.financial_eligibility_context_identity,
            )
            if expected_context != restored_context:
                raise ProviderSubrequestCacheError(
                    ProviderSubrequestCacheFailureReason.CONTEXT_MISMATCH
                )
            keys: set[str] = set()
            restored_results: dict[str, Future[ProviderSubrequestResult]] = {}
            restored_keys: dict[str, ProviderSubrequestKey] = {}
            for entry in restored.entries:
                subrequest_key = entry.key.subrequest_key
                if subrequest_key in keys:
                    raise ProviderSubrequestCacheError(
                        ProviderSubrequestCacheFailureReason.MALFORMED_CHECKPOINT
                    )
                keys.add(subrequest_key)
                value = (
                    self._artifact_store.read(entry.artifact)
                    if entry.artifact is not None
                    else None
                )
                if (
                    entry.artifact is not None
                    and entry.artifact.media_type == "application/json"
                ):
                    try:
                        json.loads(value)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ProviderSubrequestCacheError(
                            ProviderSubrequestCacheFailureReason.ARTIFACT_CORRUPT
                        ) from exc
                result = ProviderSubrequestResult(
                    key=entry.key,
                    value=value,
                    artifact=entry.artifact,
                    outcome=entry.outcome,
                    sequence_id=entry.sequence_id,
                    attempt_events=entry.attempt_events,
                )
                shared: Future[ProviderSubrequestResult] = Future()
                shared.set_result(result)
                restored_results[subrequest_key] = shared
                restored_keys[subrequest_key] = entry.key
            if self._stop_signal.is_set():
                raise ProviderSubrequestCacheError(
                    ProviderSubrequestCacheFailureReason.UNSAFE_CHECKPOINT_BOUNDARY
                )
            self._results.update(restored_results)
            self._keys.update(restored_keys)

    def _abandoned_result(self, key: ProviderSubrequestKey) -> ProviderSubrequestResult:
        digest = sha256(
            f"provider-subrequest-abandoned:{self._run_scope_id}:{key.subrequest_key}".encode()
        ).hexdigest()
        return ProviderSubrequestResult(
            key=key,
            value=None,
            artifact=None,
            outcome=ProviderSubrequestOutcome(
                kind=ProviderSubrequestOutcomeKind.ABANDONED,
                retryable=False,
            ),
            sequence_id=f"provider-request-sequence=sha256:{digest}",
            attempt_events=(),
        )

    def interrupt(self) -> None:
        """Stop accepting work and terminalize every pending cache future once."""

        self._stop_signal.set()
        with self._lock:
            if self._interrupted:
                return
            self._interrupted = True
            for subrequest_key, shared in self._results.items():
                if not shared.done():
                    shared.set_result(
                        self._abandoned_result(self._keys[subrequest_key])
                    )

    def close(self) -> None:
        """Close this run cache idempotently without changing completed entries."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.interrupt()

    def execute(
        self,
        key: ProviderSubrequestKey,
        *,
        owner_id: str,
        priority: RequestPriority,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
        max_physical_attempts: int = 1,
        operation: str,
        media_type: str,
        physical_request: Callable[[], bytes | PhysicalAttemptNotMade[bytes]],
    ) -> ProviderSubrequestResult:
        if (
            not isinstance(max_physical_attempts, int)
            or isinstance(max_physical_attempts, bool)
            or max_physical_attempts < 1
        ):
            raise ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.EXECUTION_INVALID
            )
        with self._lock:
            if self._closed or self._interrupted or self._stop_signal.is_set():
                return self._abandoned_result(key)
            shared = self._results.get(key.subrequest_key)
            is_leader = shared is None
            if shared is None:
                shared = Future()
                self._results[key.subrequest_key] = shared
                self._keys[key.subrequest_key] = key
        if not is_leader:
            return shared.result()

        try:
            def typed_physical_request() -> (
                dict[str, object] | PhysicalAttemptNotMade[dict[str, object]]
            ):
                if self._stop_signal.is_set():
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.ABANDONED,
                        retryable=False,
                        error_code="run_interrupted",
                    )
                try:
                    physical_value = physical_request()
                except PhysicalAttemptFailure:
                    raise
                except Exception as exc:
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
                        retryable=False,
                        error_code="provider_error",
                    ) from exc
                no_io = isinstance(physical_value, PhysicalAttemptNotMade)
                value = physical_value.value if no_io else physical_value
                if not isinstance(value, bytes):
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                        retryable=False,
                        error_code="malformed_transport_result",
                    )
                if not value:
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
                        retryable=False,
                        error_code="empty_transport_result",
                    )
                if self._stop_signal.is_set():
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.ABANDONED,
                        retryable=False,
                        error_code="run_interrupted",
                    )
                artifact = self._artifact_store.install(
                    value,
                    media_type=media_type,
                )
                artifact_payload = artifact.model_dump(mode="json")
                if no_io:
                    return PhysicalAttemptNotMade(artifact_payload)
                return artifact_payload

            coordinated = self._coordinator.execute_retry_sequence(
                request_key=key.subrequest_key,
                upstream_service_id=key.upstream_service_id,
                owner_id=owner_id,
                priority=priority,
                now=now,
                sleep=sleep,
                lease_duration=lease_duration,
                max_physical_attempts=max_physical_attempts,
                operation=operation,
                physical_attempt=lambda _attempt_index: typed_physical_request(),
                cooldown_scope=key.capacity_scope,
                _wait_for_initial_pacing=True,
            )
            artifact = ProviderSubrequestArtifactRef.model_validate(
                coordinated.value
            )
            value = self._artifact_store.read(artifact)
            result = ProviderSubrequestResult(
                key=key,
                value=value,
                artifact=artifact,
                outcome=ProviderSubrequestOutcome(
                    kind=ProviderSubrequestOutcomeKind.AVAILABLE
                ),
                sequence_id=coordinated.sequence_id,
                attempt_events=tuple(
                    ProviderSubrequestAttemptEvent.from_coordinator(event)
                    for event in coordinated.attempt_events
                ),
            )
        except PhysicalAttemptBudgetExhausted as exc:
            failure = exc.failure
            result = ProviderSubrequestResult(
                key=key,
                value=None,
                artifact=None,
                outcome=ProviderSubrequestOutcome(
                    kind=_outcome_from_physical_failure(failure.outcome),
                    retryable=failure.retryable,
                    status_code=failure.status_code,
                    retry_after_seconds=failure.retry_after_seconds,
                ),
                sequence_id=exc.sequence_id,
                attempt_events=tuple(
                    ProviderSubrequestAttemptEvent.from_coordinator(event)
                    for event in exc.attempt_events
                ),
            )
        except BaseException as exc:
            safe_error = ProviderSubrequestCacheError(
                ProviderSubrequestCacheFailureReason.EXECUTION_INVALID
            )
            with self._lock:
                if shared.done():
                    return shared.result()
                shared.set_exception(safe_error)
            raise safe_error from exc
        with self._lock:
            if shared.done():
                return shared.result()
            if self._stop_signal.is_set():
                result = self._abandoned_result(key)
            shared.set_result(result)
            return result


__all__ = [
    "PROVIDER_SUBREQUEST_KEY_VERSION",
    "ProviderSubrequestArtifactRef",
    "ProviderSubrequestArtifactStore",
    "ProviderSubrequestAttemptEvent",
    "ProviderSubrequestCache",
    "ProviderSubrequestCacheCheckpoint",
    "ProviderSubrequestCacheError",
    "ProviderSubrequestCacheFailureReason",
    "ProviderSubrequestCheckpointEntry",
    "ProviderSubrequestInstrumentIdentity",
    "ProviderSubrequestKey",
    "ProviderSubrequestOutcome",
    "ProviderSubrequestOutcomeKind",
    "ProviderSubrequestResult",
]

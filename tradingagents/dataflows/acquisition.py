"""Deterministic, run-scoped source acquisition policy."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from tradingagents.evidence import (
    ACQUISITION_TOKEN_PATTERN,
    AcquisitionUnavailableReason,
    SourceAcquisitionAvailable,
    SourceAcquisitionOutcome,
    SourceAcquisitionUnavailable,
    SourceArtifact,
)

_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
T = TypeVar("T")


class AcquisitionRequest(BaseModel):
    """Provider-independent identity for one requested source capability."""

    model_config = _CLOSED_MODEL_CONFIG

    capability: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    source_ref: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)


class RetryPolicy(BaseModel):
    """Explicit retry limits; the safe default performs one attempt."""

    model_config = _CLOSED_MODEL_CONFIG

    max_attempts_per_provider: int = Field(default=1, ge=1)
    backoff_seconds: float = Field(default=0, ge=0)


class AcquisitionFailure(Exception):
    """Typed provider failure; raw provider text is deliberately not accepted."""

    def __init__(
        self,
        *,
        reason: AcquisitionUnavailableReason,
        status_code: int | None = None,
        error_code: str | None = None,
        retry_after_seconds: float | None = None,
        retryable: bool | None = None,
    ) -> None:
        if retryable is not None and not isinstance(retryable, bool):
            raise TypeError("acquisition failure retryability must be a boolean")
        if error_code is not None and (
            not isinstance(error_code, str)
            or re.fullmatch(ACQUISITION_TOKEN_PATTERN, error_code) is None
        ):
            raise ValueError("acquisition failure error code must be a bounded token")
        super().__init__(reason.value)
        self.reason = reason
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = retryable


@dataclass(frozen=True)
class AcquisitionResult(Generic[T]):
    value: T | None
    artifact: SourceArtifact | None
    outcomes: tuple[SourceAcquisitionOutcome, ...]
    provider: str | None = None


Provider = Callable[[AcquisitionRequest], object]
Validator = Callable[[object], T]
Serializer = Callable[[T], str]
CandidateAcceptance = Callable[[T], bool]
FallbackCandidateIndex = Callable[[tuple[T, ...]], int]
AcquisitionOutcomeObserver = Callable[
    [AcquisitionRequest, SourceAcquisitionOutcome],
    None,
]


class AcquisitionController:
    """Own ordered fallback for one analysis run."""

    def __init__(
        self,
        *,
        providers: tuple[tuple[str, Provider], ...],
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        retry_policy: RetryPolicy | None = None,
        outcome_observer: AcquisitionOutcomeObserver | None = None,
    ) -> None:
        self._providers = providers
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or (lambda _seconds: None)
        self._retry_policy = retry_policy or RetryPolicy()
        self._outcome_observer = outcome_observer
        self._open_circuits: set[tuple[str, str]] = set()

    def _record_outcome(
        self,
        outcomes: list[SourceAcquisitionOutcome],
        request: AcquisitionRequest,
        outcome: SourceAcquisitionOutcome,
    ) -> None:
        outcomes.append(outcome)
        if self._outcome_observer is not None:
            self._outcome_observer(request, outcome)

    def acquire(
        self,
        request: AcquisitionRequest,
        *,
        validator: Validator[T],
        serializer: Serializer[T],
        providers: tuple[tuple[str, Provider], ...] | None = None,
        accept_candidate: CandidateAcceptance[T] | None = None,
        fallback_candidate_index: FallbackCandidateIndex[T] | None = None,
    ) -> AcquisitionResult[T]:
        outcomes: list[SourceAcquisitionOutcome] = []
        candidates: list[tuple[str, T, SourceArtifact]] = []
        effective_providers = self._providers if providers is None else providers
        for provider_order, (provider_name, provider) in enumerate(effective_providers):
            circuit_key = (provider_name, request.tool_name)
            if circuit_key in self._open_circuits:
                self._record_outcome(
                    outcomes,
                    request,
                    SourceAcquisitionUnavailable(
                        provider=provider_name,
                        provider_order=provider_order,
                        capability=request.capability,
                        source_ref=request.source_ref,
                        attempt=1,
                        retrieved_at=self._timestamp(),
                        retryable=False,
                        reason=AcquisitionUnavailableReason.CIRCUIT_OPEN,
                    ),
                )
                continue
            for attempt in range(1, self._retry_policy.max_attempts_per_provider + 1):
                try:
                    raw_value = provider(request)
                    value = validator(raw_value)
                    raw_text = serializer(value)
                except AcquisitionFailure as failure:
                    retryable = (
                        failure.retryable
                        if failure.retryable is not None
                        else failure.reason
                        in {
                            AcquisitionUnavailableReason.RATE_LIMITED,
                            AcquisitionUnavailableReason.TIMEOUT,
                            AcquisitionUnavailableReason.PROVIDER_ERROR,
                        }
                    )
                    self._record_outcome(
                        outcomes,
                        request,
                        SourceAcquisitionUnavailable(
                            provider=provider_name,
                            provider_order=provider_order,
                            capability=request.capability,
                            source_ref=request.source_ref,
                            attempt=attempt,
                            retrieved_at=self._timestamp(),
                            retryable=retryable,
                            reason=failure.reason,
                            error_code=failure.error_code,
                            retry_after_seconds=failure.retry_after_seconds,
                            http_status=failure.status_code,
                        ),
                    )
                    if (
                        retryable
                        and attempt < self._retry_policy.max_attempts_per_provider
                    ):
                        delay = (
                            failure.retry_after_seconds
                            if failure.retry_after_seconds is not None
                            else self._retry_policy.backoff_seconds
                        )
                        self._sleeper(delay)
                        continue
                    self._open_circuits.add(circuit_key)
                    break
                except Exception:
                    self._record_outcome(
                        outcomes,
                        request,
                        SourceAcquisitionUnavailable(
                            provider=provider_name,
                            provider_order=provider_order,
                            capability=request.capability,
                            source_ref=request.source_ref,
                            attempt=attempt,
                            retrieved_at=self._timestamp(),
                            retryable=False,
                            reason=AcquisitionUnavailableReason.PROVIDER_ERROR,
                        ),
                    )
                    self._open_circuits.add(circuit_key)
                    break

                artifact = SourceArtifact(
                    artifact_sha256=sha256(raw_text.encode("utf-8")).hexdigest(),
                    source_ref=request.source_ref,
                    tool_call_id=request.tool_call_id,
                    tool_name=request.tool_name,
                    raw_text=raw_text,
                )
                available = SourceAcquisitionAvailable(
                    provider=provider_name,
                    provider_order=provider_order,
                    capability=request.capability,
                    source_ref=request.source_ref,
                    attempt=attempt,
                    retrieved_at=self._timestamp(),
                    artifact=artifact,
                )
                self._record_outcome(outcomes, request, available)
                if accept_candidate is None or accept_candidate(value):
                    return AcquisitionResult(
                        value=value,
                        artifact=artifact,
                        outcomes=tuple(outcomes),
                        provider=provider_name,
                    )
                candidates.append((provider_name, value, artifact))
                break
        if candidates and fallback_candidate_index is not None:
            selected = fallback_candidate_index(
                tuple(candidate[1] for candidate in candidates)
            )
            provider_name, value, artifact = candidates[selected]
            return AcquisitionResult(
                value=value,
                artifact=artifact,
                outcomes=tuple(outcomes),
                provider=provider_name,
            )
        return AcquisitionResult(
            value=None, artifact=None, outcomes=tuple(outcomes), provider=None
        )

    def _timestamp(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

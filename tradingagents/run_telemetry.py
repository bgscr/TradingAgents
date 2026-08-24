"""Closed, run-scoped telemetry contracts shared by every runner."""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Mapping
from threading import Lock
from typing import Any, Literal

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.evidence import (
    ACQUISITION_TOKEN_PATTERN,
    AcquisitionUnavailableReason,
    SourceAcquisitionAvailable,
    SourceAcquisitionOutcome,
    SourceAcquisitionUnavailable,
)

RUN_TELEMETRY_CONTRACT_VERSION = "1.1"
ACQUISITION_TELEMETRY_EVENT_CONTRACT_VERSION = "1.0"
MODEL_TRANSPORT_ATTEMPT_CONTRACT_VERSION = "1.0"
MAX_RETAINED_MODEL_TRANSPORT_ATTEMPTS = 256
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
RunTelemetryTerminalRoute = Literal[
    "trading_decision",
    "analysis_outcome",
    "operational_failure",
]
ModelTransportAttemptDisposition = Literal[
    "success",
    "retryable_transport_failure",
    "terminal_transport_failure",
    "status_failure",
]


class ModelTransportAttempt(BaseModel):
    """Closed safe projection of one physical OpenAI-compatible request."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = MODEL_TRANSPORT_ATTEMPT_CONTRACT_VERSION
    logical_call_id: str = Field(pattern=r"^model-call-[0-9]{6,}$")
    attempt_ordinal: int = Field(ge=0)
    configured_max_retries: int = Field(ge=0)
    retry_index: int | None = Field(default=None, ge=0)
    provider_family: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    model_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$",
    )
    endpoint_host: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]*$",
    )
    elapsed_seconds: float = Field(ge=0)
    disposition: ModelTransportAttemptDisposition
    exception_class: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    exception_family: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @model_validator(mode="after")
    def _failure_fields_match_disposition(self) -> ModelTransportAttempt:
        is_transport_failure = self.disposition in {
            "retryable_transport_failure",
            "terminal_transport_failure",
        }
        if is_transport_failure != (
            self.exception_class is not None and self.exception_family is not None
        ):
            raise ValueError(
                "transport failure disposition must match typed exception metadata"
            )
        if self.disposition == "status_failure":
            if self.http_status is None or self.http_status < 400:
                raise ValueError("status failure requires an HTTP failure status")
        elif self.disposition == "success" and (
            self.http_status is not None and self.http_status >= 400
        ):
            raise ValueError("successful transport attempt cannot carry failure status")
        if self.attempt_ordinal > self.configured_max_retries:
            raise ValueError("attempt ordinal exceeds configured retry budget")
        if (
            self.retry_index is not None
            and self.retry_index > self.configured_max_retries
        ):
            raise ValueError("SDK retry index exceeds configured retry budget")
        return self


class ModelTransportProviderModelAttempts(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    provider_family: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    model_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$",
    )
    physical_attempts: int = Field(ge=1)


class ModelTransportAttemptSummary(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    physical_attempts: int = Field(ge=0)
    retries_observed: int = Field(ge=0)
    successful_attempts: int = Field(ge=0)
    retryable_failures: int = Field(ge=0)
    terminal_failures: int = Field(ge=0)
    status_failures: int = Field(ge=0)
    retained_attempt_records: int = Field(ge=0)
    dropped_attempt_records: int = Field(ge=0)
    by_provider_model: tuple[ModelTransportProviderModelAttempts, ...] = ()

    @model_validator(mode="after")
    def _counts_are_internally_consistent(self) -> ModelTransportAttemptSummary:
        disposition_total = (
            self.successful_attempts
            + self.retryable_failures
            + self.terminal_failures
            + self.status_failures
        )
        if disposition_total != self.physical_attempts:
            raise ValueError("transport disposition totals must equal physical attempts")
        if (
            self.retained_attempt_records + self.dropped_attempt_records
            != self.physical_attempts
        ):
            raise ValueError("retained and dropped records must equal physical attempts")
        if self.retries_observed > self.physical_attempts:
            raise ValueError("observed retries cannot exceed physical attempts")
        if sum(item.physical_attempts for item in self.by_provider_model) != (
            self.physical_attempts
        ):
            raise ValueError("provider/model totals must equal physical attempts")
        provider_model_keys = {
            (item.provider_family, item.model_id) for item in self.by_provider_model
        }
        if len(provider_model_keys) != len(self.by_provider_model):
            raise ValueError("provider/model summary keys must be unique")
        return self


class ModelTransportAttemptProjection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = MODEL_TRANSPORT_ATTEMPT_CONTRACT_VERSION
    retention_limit: Literal[256] = MAX_RETAINED_MODEL_TRANSPORT_ATTEMPTS
    attempts: tuple[ModelTransportAttempt, ...] = ()
    summary: ModelTransportAttemptSummary

    @model_validator(mode="after")
    def _retained_records_match_summary(self) -> ModelTransportAttemptProjection:
        if len(self.attempts) != self.summary.retained_attempt_records:
            raise ValueError("retained attempt count must match the attempt records")
        identities = {
            (attempt.logical_call_id, attempt.attempt_ordinal)
            for attempt in self.attempts
        }
        if len(identities) != len(self.attempts):
            raise ValueError("retained model transport attempt identities must be unique")
        return self


class AcquisitionTelemetryOutcome(BaseModel):
    """Bounded acquisition metadata; provider payloads are deliberately absent."""

    model_config = _CLOSED_MODEL_CONFIG

    outcome: Literal["available", "unavailable"]
    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_order: int = Field(ge=0)
    capability: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    source_ref: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    attempt: int = Field(ge=1)
    retrieved_at: str = Field(min_length=1)
    retryable: bool | None = None
    reason: AcquisitionUnavailableReason | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)
    http_status: int | None = Field(default=None, ge=100, le=599)

    @model_validator(mode="after")
    def _availability_fields_match_outcome(self) -> AcquisitionTelemetryOutcome:
        if self.outcome == "available":
            if any(
                value is not None
                for value in (
                    self.retryable,
                    self.reason,
                    self.retry_after_seconds,
                    self.http_status,
                )
            ):
                raise ValueError("available acquisition telemetry cannot carry failure metadata")
        elif self.retryable is None or self.reason is None:
            raise ValueError("unavailable acquisition telemetry requires typed failure metadata")
        return self

    @classmethod
    def from_source_outcome(
        cls,
        outcome: SourceAcquisitionOutcome,
    ) -> AcquisitionTelemetryOutcome:
        common = {
            "outcome": outcome.outcome,
            "provider": outcome.provider,
            "provider_order": outcome.provider_order,
            "capability": outcome.capability,
            "source_ref": outcome.source_ref,
            "attempt": outcome.attempt,
            "retrieved_at": outcome.retrieved_at,
        }
        if isinstance(outcome, SourceAcquisitionAvailable):
            return cls(**common)
        assert isinstance(outcome, SourceAcquisitionUnavailable)
        return cls(
            **common,
            retryable=outcome.retryable,
            reason=outcome.reason,
            retry_after_seconds=outcome.retry_after_seconds,
            http_status=outcome.http_status,
        )


class AcquisitionTelemetryEvent(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = ACQUISITION_TELEMETRY_EVENT_CONTRACT_VERSION
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    outcome: AcquisitionTelemetryOutcome


class AcquisitionTelemetrySummary(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    attempts: int = Field(ge=0)
    available: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    retryable_unavailable: int = Field(ge=0)
    retry_events: int = Field(ge=0)
    circuit_breaker_events: int = Field(ge=0)
    unavailable_reasons: dict[str, int]


class AcquisitionTelemetryProjection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    events: tuple[AcquisitionTelemetryEvent, ...] = ()
    summary: AcquisitionTelemetrySummary


class TelemetryCost(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    available: bool
    amount_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _amount_matches_availability(self) -> TelemetryCost:
        if self.available != (self.amount_usd is not None):
            raise ValueError("telemetry cost availability must match its amount")
        return self


class StageTelemetry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    model_calls: int = Field(ge=0)
    model_seconds: float = Field(ge=0)
    model_tokens_in: int = Field(ge=0)
    model_tokens_out: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tool_seconds: float = Field(ge=0)
    cost: TelemetryCost


class RunTelemetryProjection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0", "1.1"] = RUN_TELEMETRY_CONTRACT_VERSION
    terminal_route: RunTelemetryTerminalRoute | None = None
    acquisition: AcquisitionTelemetryProjection
    stages: dict[str, StageTelemetry] = Field(default_factory=dict)
    model_transport: ModelTransportAttemptProjection | None = None

    @model_validator(mode="after")
    def _transport_matches_contract_version(self) -> RunTelemetryProjection:
        if self.contract_version == "1.0" and self.model_transport is not None:
            raise ValueError("run telemetry v1.0 cannot contain model transport data")
        if self.contract_version == "1.1" and self.model_transport is None:
            raise ValueError("run telemetry v1.1 requires model transport data")
        return self

    @classmethod
    def empty(
        cls,
        *,
        terminal_route: RunTelemetryTerminalRoute | None = None,
    ) -> RunTelemetryProjection:
        return cls(
            contract_version="1.0",
            terminal_route=terminal_route,
            acquisition=AcquisitionTelemetryProjection(
                summary=AcquisitionTelemetrySummary(
                    attempts=0,
                    available=0,
                    unavailable=0,
                    retryable_unavailable=0,
                    retry_events=0,
                    circuit_breaker_events=0,
                    unavailable_reasons={},
                )
            ),
        )


class RunTelemetryLedger:
    """Thread-safe event ledger owned by exactly one analysis run."""

    def __init__(self) -> None:
        self._events: list[AcquisitionTelemetryEvent] = []
        self._current_stage = "unassigned"
        self._stages: dict[str, dict[str, float | int]] = {}
        self._next_model_transport_call = 1
        self._model_transport_attempt_counts: dict[str, int] = {}
        self._pending_model_transport_failures: dict[str, ModelTransportAttempt] = {}
        self._model_transport_attempts: deque[ModelTransportAttempt] = deque(
            maxlen=MAX_RETAINED_MODEL_TRANSPORT_ATTEMPTS
        )
        self._model_transport_dispositions: Counter[str] = Counter()
        self._model_transport_provider_models: Counter[tuple[str, str]] = Counter()
        self._model_transport_physical_attempts = 0
        self._model_transport_retries_observed = 0
        self._model_transport_dropped_records = 0
        self._lock = Lock()

    def begin_model_transport_call(self) -> str:
        """Allocate a deterministic run-local identity for one logical model call."""

        with self._lock:
            logical_call_id = f"model-call-{self._next_model_transport_call:06d}"
            self._next_model_transport_call += 1
            self._model_transport_attempt_counts[logical_call_id] = 0
            return logical_call_id

    def begin_model_transport_attempt(self, logical_call_id: str) -> int:
        """Observe an actual SDK send and settle any preceding failure as retried."""

        with self._lock:
            pending = self._pending_model_transport_failures.pop(
                logical_call_id,
                None,
            )
            if pending is not None:
                self._retain_model_transport_attempt(
                    pending.model_copy(
                        update={"disposition": "retryable_transport_failure"}
                    )
                )
            try:
                attempt_ordinal = self._model_transport_attempt_counts[logical_call_id]
            except KeyError as exc:
                raise ValueError("unknown logical model transport call") from exc
            self._model_transport_attempt_counts[logical_call_id] = attempt_ordinal + 1
            return attempt_ordinal

    def record_model_transport_response(
        self,
        attempt: ModelTransportAttempt,
    ) -> None:
        if attempt.disposition not in {"success", "status_failure"}:
            raise ValueError("response attempt requires a response disposition")
        with self._lock:
            self._retain_model_transport_attempt(attempt)

    def record_model_transport_failure(
        self,
        attempt: ModelTransportAttempt,
    ) -> None:
        if attempt.disposition != "terminal_transport_failure":
            raise ValueError("pending transport failure must use terminal disposition")
        with self._lock:
            if attempt.logical_call_id in self._pending_model_transport_failures:
                raise ValueError("logical model call already has a pending failure")
            self._pending_model_transport_failures[attempt.logical_call_id] = attempt

    def finish_model_transport_call(self, logical_call_id: str) -> None:
        """Settle the last observed physical failure without deciding SDK retries."""

        with self._lock:
            pending = self._pending_model_transport_failures.pop(
                logical_call_id,
                None,
            )
            if pending is not None:
                self._retain_model_transport_attempt(pending)
            self._model_transport_attempt_counts.pop(logical_call_id, None)

    def _retain_model_transport_attempt(self, attempt: ModelTransportAttempt) -> None:
        if len(self._model_transport_attempts) == MAX_RETAINED_MODEL_TRANSPORT_ATTEMPTS:
            self._model_transport_dropped_records += 1
        self._model_transport_attempts.append(attempt)
        self._model_transport_physical_attempts += 1
        self._model_transport_retries_observed += int(attempt.attempt_ordinal > 0)
        self._model_transport_dispositions[attempt.disposition] += 1
        self._model_transport_provider_models[
            (attempt.provider_family, attempt.model_id)
        ] += 1

    def transition_stage(self, stage: str) -> None:
        normalized = str(stage).strip()
        if not normalized:
            raise ValueError("run telemetry stage must not be empty")
        with self._lock:
            self._current_stage = normalized

    def record_model_activity(
        self,
        *,
        stage: str | None = None,
        seconds: float,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        with self._lock:
            stage_name = self._current_stage if stage is None else str(stage).strip()
            if not stage_name:
                raise ValueError("run telemetry stage must not be empty")
            stage = self._stages.setdefault(
                stage_name,
                {
                    "model_calls": 0,
                    "model_seconds": 0.0,
                    "model_tokens_in": 0,
                    "model_tokens_out": 0,
                    "tool_calls": 0,
                    "tool_seconds": 0.0,
                },
            )
            stage["model_calls"] += 1
            stage["model_seconds"] += max(0.0, float(seconds))
            stage["model_tokens_in"] += max(0, int(tokens_in))
            stage["model_tokens_out"] += max(0, int(tokens_out))

    def record_tool_activity(
        self,
        *,
        seconds: float,
        stage: str | None = None,
    ) -> None:
        with self._lock:
            stage_name = self._current_stage if stage is None else str(stage).strip()
            if not stage_name:
                raise ValueError("run telemetry stage must not be empty")
            stage = self._stages.setdefault(
                stage_name,
                {
                    "model_calls": 0,
                    "model_seconds": 0.0,
                    "model_tokens_in": 0,
                    "model_tokens_out": 0,
                    "tool_calls": 0,
                    "tool_seconds": 0.0,
                },
            )
            stage["tool_calls"] += 1
            stage["tool_seconds"] += max(0.0, float(seconds))

    def record_acquisition(
        self,
        request: Any,
        outcome: SourceAcquisitionOutcome,
    ) -> None:
        self.record_source_outcome(
            tool_call_id=str(request.tool_call_id),
            tool_name=str(request.tool_name),
            outcome=outcome,
        )

    def record_source_outcome(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        outcome: SourceAcquisitionOutcome,
    ) -> None:
        """Record a typed outcome produced outside AcquisitionController."""

        event = AcquisitionTelemetryEvent(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            outcome=AcquisitionTelemetryOutcome.from_source_outcome(outcome),
        )
        with self._lock:
            self._events.append(event)

    def finalize(
        self,
        *,
        terminal_route: RunTelemetryTerminalRoute | None = None,
    ) -> RunTelemetryProjection:
        with self._lock:
            events = tuple(self._events)
            stages = {
                name: StageTelemetry(
                    **values,
                    cost=TelemetryCost(available=False, amount_usd=None),
                )
                for name, values in sorted(self._stages.items())
            }
            transport_attempts = tuple(self._model_transport_attempts)
            transport_dispositions = self._model_transport_dispositions.copy()
            transport_by_provider_model = self._model_transport_provider_models.copy()
            transport_physical_attempts = self._model_transport_physical_attempts
            transport_retries_observed = self._model_transport_retries_observed
            transport_dropped_records = self._model_transport_dropped_records
        reasons: Counter[str] = Counter()
        maximum_attempts: dict[tuple[str, str, str, str], int] = {}
        available = 0
        unavailable = 0
        retryable = 0
        circuit_events = 0
        for event in events:
            outcome = event.outcome
            if outcome.outcome == "available":
                available += 1
            else:
                unavailable += 1
                retryable += int(outcome.retryable is True)
                assert outcome.reason is not None
                reasons[outcome.reason.value] += 1
                circuit_events += int(
                    outcome.reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
                )
            retry_identity = (
                event.tool_call_id,
                outcome.provider,
                outcome.capability,
                outcome.source_ref,
            )
            maximum_attempts[retry_identity] = max(
                maximum_attempts.get(retry_identity, 0),
                outcome.attempt,
            )
        summary = AcquisitionTelemetrySummary(
            attempts=len(events),
            available=available,
            unavailable=unavailable,
            retryable_unavailable=retryable,
            retry_events=sum(max(0, attempt - 1) for attempt in maximum_attempts.values()),
            circuit_breaker_events=circuit_events,
            unavailable_reasons=dict(sorted(reasons.items())),
        )
        model_transport = ModelTransportAttemptProjection(
            attempts=transport_attempts,
            summary=ModelTransportAttemptSummary(
                physical_attempts=transport_physical_attempts,
                retries_observed=transport_retries_observed,
                successful_attempts=transport_dispositions["success"],
                retryable_failures=transport_dispositions[
                    "retryable_transport_failure"
                ],
                terminal_failures=transport_dispositions[
                    "terminal_transport_failure"
                ],
                status_failures=transport_dispositions["status_failure"],
                retained_attempt_records=len(transport_attempts),
                dropped_attempt_records=transport_dropped_records,
                by_provider_model=tuple(
                    ModelTransportProviderModelAttempts(
                        provider_family=provider_family,
                        model_id=model_id,
                        physical_attempts=count,
                    )
                    for (provider_family, model_id), count in sorted(
                        transport_by_provider_model.items()
                    )
                ),
            ),
        )
        return RunTelemetryProjection(
            terminal_route=terminal_route,
            acquisition=AcquisitionTelemetryProjection(
                events=events,
                summary=summary,
            ),
            stages=stages,
            model_transport=model_transport,
        )


def acquisition_summary_from_projection(
    value: RunTelemetryProjection | Mapping[str, Any],
) -> dict[str, Any]:
    projection = (
        value
        if isinstance(value, RunTelemetryProjection)
        else RunTelemetryProjection.model_validate(value)
    )
    return projection.acquisition.summary.model_dump(mode="json")


def telemetry_stage_from_callback_metadata(metadata: Any) -> str:
    node = ""
    if isinstance(metadata, Mapping):
        node = str(metadata.get("langgraph_node", ""))
    normalized = node.casefold().replace("_", " ")
    if any(
        marker in normalized
        for marker in (
            "market analyst",
            "sentiment analyst",
            "news analyst",
            "fundamentals analyst",
            "tools market",
            "tools social",
            "tools news",
            "tools fundamentals",
        )
    ):
        return "analysis"
    if any(
        marker in normalized
        for marker in ("bull researcher", "bear researcher", "research manager")
    ):
        return "research_debate"
    if "trader" in normalized:
        return "trading"
    if any(
        marker in normalized
        for marker in ("aggressive", "conservative", "neutral", "risk")
    ):
        return "risk_debate"
    if "portfolio" in normalized or "direction" in normalized:
        return "portfolio_synthesis"
    return node or "unassigned"


class RunTelemetryCallbackHandler(BaseCallbackHandler):
    """Programmatic-run callback that records bounded stage telemetry."""

    def __init__(
        self,
        ledger: RunTelemetryLedger,
        *,
        clock: Any = time.monotonic,
    ) -> None:
        super().__init__()
        self._ledger = ledger
        self._clock = clock
        self._lock = Lock()
        self._model_started: dict[Any, tuple[float, str]] = {}
        self._tool_started: dict[Any, tuple[float, str]] = {}

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        self._start("model", kwargs)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        self._start("model", kwargs)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        finished = self._finish("model", kwargs)
        if finished is None:
            return
        seconds, stage = finished
        usage: Mapping[str, Any] = {}
        try:
            generation = response.generations[0][0]
        except (AttributeError, IndexError, TypeError):
            generation = None
        message = getattr(generation, "message", None)
        if isinstance(message, AIMessage) and message.usage_metadata:
            usage = message.usage_metadata
        self._ledger.record_model_activity(
            stage=stage,
            seconds=seconds,
            tokens_in=int(usage.get("input_tokens", 0) or 0),
            tokens_out=int(usage.get("output_tokens", 0) or 0),
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        finished = self._finish("model", kwargs)
        if finished is None:
            return
        seconds, stage = finished
        self._ledger.record_model_activity(
            stage=stage,
            seconds=seconds,
            tokens_in=0,
            tokens_out=0,
        )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        self._start("tool", kwargs)

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        finished = self._finish("tool", kwargs)
        if finished is None:
            return
        seconds, stage = finished
        self._ledger.record_tool_activity(seconds=seconds, stage=stage)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self.on_tool_end(None, **kwargs)

    def _start(self, category: str, kwargs: Mapping[str, Any]) -> None:
        run_id = kwargs.get("run_id")
        if run_id is None:
            return
        started = (
            self._clock(),
            telemetry_stage_from_callback_metadata(kwargs.get("metadata")),
        )
        with self._lock:
            target = self._model_started if category == "model" else self._tool_started
            target[run_id] = started

    def _finish(
        self,
        category: str,
        kwargs: Mapping[str, Any],
    ) -> tuple[float, str] | None:
        run_id = kwargs.get("run_id")
        if run_id is None:
            return None
        with self._lock:
            target = self._model_started if category == "model" else self._tool_started
            started = target.pop(run_id, None)
        if started is None:
            return None
        started_at, stage = started
        return max(0.0, self._clock() - started_at), stage

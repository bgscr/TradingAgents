"""Closed, run-scoped telemetry contracts shared by every runner."""

from __future__ import annotations

import time
from collections import Counter
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

RUN_TELEMETRY_CONTRACT_VERSION = "1.0"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
RunTelemetryTerminalRoute = Literal[
    "trading_decision",
    "analysis_outcome",
    "operational_failure",
]


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

    contract_version: Literal["1.0"] = RUN_TELEMETRY_CONTRACT_VERSION
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

    contract_version: Literal["1.0"] = RUN_TELEMETRY_CONTRACT_VERSION
    terminal_route: RunTelemetryTerminalRoute | None = None
    acquisition: AcquisitionTelemetryProjection
    stages: dict[str, StageTelemetry] = Field(default_factory=dict)

    @classmethod
    def empty(
        cls,
        *,
        terminal_route: RunTelemetryTerminalRoute | None = None,
    ) -> RunTelemetryProjection:
        return cls(
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
        self._lock = Lock()

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
        return RunTelemetryProjection(
            terminal_route=terminal_route,
            acquisition=AcquisitionTelemetryProjection(
                events=events,
                summary=summary,
            ),
            stages=stages,
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

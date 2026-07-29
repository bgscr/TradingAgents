from __future__ import annotations

from datetime import datetime, timezone

from pydantic import TypeAdapter

from tradingagents.dataflows.acquisition import (
    AcquisitionController,
    AcquisitionFailure,
    AcquisitionRequest,
    RetryPolicy,
)
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
)


def test_primary_rate_limit_falls_back_without_creating_failed_artifact() -> None:
    calls: list[str] = []

    def primary(_request: AcquisitionRequest) -> object:
        calls.append("primary")
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.RATE_LIMITED,
            status_code=429,
            retry_after_seconds=30,
        )

    def secondary(_request: AcquisitionRequest) -> object:
        calls.append("secondary")
        return {"close": "10.25"}

    controller = AcquisitionController(
        providers=(("primary", primary), ("secondary", secondary)),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )

    result = controller.acquire(
        AcquisitionRequest(
            capability="daily_close",
            source_ref="market:600895.SS:2026-07-18",
            tool_call_id="call-1",
            tool_name="get_market_data",
        ),
        validator=lambda payload: payload,
        serializer=lambda payload: '{"close":"10.25"}',
    )

    assert calls == ["primary", "secondary"]
    assert result.value == {"close": "10.25"}
    assert len(result.outcomes) == 2
    first, second = result.outcomes
    assert isinstance(first, SourceAcquisitionUnavailable)
    assert first.model_dump(mode="json") == {
        "contract_version": "1.0",
        "outcome": "unavailable",
        "provider": "primary",
        "provider_order": 0,
        "capability": "daily_close",
        "source_ref": "market:600895.SS:2026-07-18",
        "attempt": 1,
        "retrieved_at": "2026-07-20T12:00:00Z",
        "retryable": True,
        "reason": "rate_limited",
        "error_code": None,
        "retry_after_seconds": 30.0,
        "http_status": 429,
        "calculation_readiness": None,
    }
    assert isinstance(second, SourceAcquisitionAvailable)
    assert second.provider == "secondary"
    assert second.artifact.raw_text == '{"close":"10.25"}'
    assert not hasattr(first, "artifact")


def test_open_circuit_is_shared_by_logical_capabilities_for_one_tool() -> None:
    calls: list[str] = []

    def provider(request: AcquisitionRequest) -> object:
        calls.append(request.capability)
        if request.capability == "news":
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.TIMEOUT,
                error_code="UPSTREAM_TIMEOUT",
            )
        return {"sentiment": "neutral"}

    controller = AcquisitionController(
        providers=(("vendor-a", provider),),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )
    first = AcquisitionRequest(
        capability="news",
        source_ref="news:first-phrasing",
        tool_call_id="call-1",
        tool_name="get_news",
    )
    rephrased = first.model_copy(
        update={"source_ref": "news:different-arguments", "tool_call_id": "call-2"}
    )
    other_capability = first.model_copy(
        update={
            "capability": "sentiment",
            "source_ref": "sentiment:600895.SS",
            "tool_call_id": "call-3",
        }
    )
    other_tool = other_capability.model_copy(
        update={"tool_call_id": "call-4", "tool_name": "get_sentiment"}
    )

    first_result = controller.acquire(
        first, validator=lambda value: value, serializer=lambda _value: "{}"
    )
    skipped_result = controller.acquire(
        rephrased, validator=lambda value: value, serializer=lambda _value: "{}"
    )
    other_capability_result = controller.acquire(
        other_capability,
        validator=lambda value: value,
        serializer=lambda _value: '{"sentiment":"neutral"}',
    )
    other_tool_result = controller.acquire(
        other_tool,
        validator=lambda value: value,
        serializer=lambda _value: '{"sentiment":"neutral"}',
    )

    assert calls == ["news", "sentiment"]
    assert first_result.artifact is None
    assert skipped_result.artifact is None
    assert len(skipped_result.outcomes) == 1
    skipped = skipped_result.outcomes[0]
    assert isinstance(skipped, SourceAcquisitionUnavailable)
    assert skipped.reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
    assert skipped.retryable is False
    assert skipped.http_status is None
    assert other_capability_result.artifact is None
    assert (
        other_capability_result.outcomes[0].reason
        is AcquisitionUnavailableReason.CIRCUIT_OPEN
    )
    assert other_capability_result.outcomes[0].capability == "sentiment"
    assert other_tool_result.value == {"sentiment": "neutral"}


def test_retry_after_precedes_configured_backoff_and_attempts_are_bounded() -> None:
    provider_calls = 0
    sleeps: list[float] = []

    def provider(_request: AcquisitionRequest) -> object:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.TIMEOUT,
                retry_after_seconds=7,
            )
        return [1, 2, 3]

    controller = AcquisitionController(
        providers=(("vendor-a", provider),),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        sleeper=sleeps.append,
        retry_policy=RetryPolicy(
            max_attempts_per_provider=2,
            backoff_seconds=2,
        ),
    )
    result = controller.acquire(
        AcquisitionRequest(
            capability="market_history",
            source_ref="history:600895.SS",
            tool_call_id="call-4",
            tool_name="get_market_history",
        ),
        validator=lambda value: value,
        serializer=lambda _value: "[1,2,3]",
    )

    assert provider_calls == 2
    assert sleeps == [7.0]
    assert [outcome.attempt for outcome in result.outcomes] == [1, 2]
    assert [outcome.outcome for outcome in result.outcomes] == [
        "unavailable",
        "available",
    ]
    assert result.value == [1, 2, 3]


def test_nonretryable_failures_have_no_artifact_and_preserve_fallback_order() -> None:
    calls: list[str] = []

    def malformed(_request: AcquisitionRequest) -> object:
        calls.append("malformed")
        return {"unexpected": "shape"}

    def no_data(_request: AcquisitionRequest) -> object:
        calls.append("no-data")
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.NO_DATA)

    def auth(_request: AcquisitionRequest) -> object:
        calls.append("auth")
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.AUTHENTICATION,
            status_code=401,
        )

    def fallback(_request: AcquisitionRequest) -> object:
        calls.append("fallback")
        return {"close": "9.90"}

    def validate(payload: object) -> object:
        if payload == {"unexpected": "shape"}:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE,
                error_code="SCHEMA_MISMATCH",
            )
        return payload

    controller = AcquisitionController(
        providers=(
            ("malformed", malformed),
            ("no-data", no_data),
            ("auth", auth),
            ("fallback", fallback),
        ),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        retry_policy=RetryPolicy(max_attempts_per_provider=3, backoff_seconds=1),
    )
    result = controller.acquire(
        AcquisitionRequest(
            capability="daily_close",
            source_ref="market:601658.SS:2026-07-18",
            tool_call_id="call-5",
            tool_name="get_market_data",
        ),
        validator=validate,
        serializer=lambda _value: '{"close":"9.90"}',
    )

    assert calls == ["malformed", "no-data", "auth", "fallback"]
    assert [outcome.provider for outcome in result.outcomes] == calls
    assert [outcome.outcome for outcome in result.outcomes] == [
        "unavailable",
        "unavailable",
        "unavailable",
        "available",
    ]
    assert [outcome.reason for outcome in result.outcomes[:-1]] == [
        AcquisitionUnavailableReason.MALFORMED_RESPONSE,
        AcquisitionUnavailableReason.NO_DATA,
        AcquisitionUnavailableReason.AUTHENTICATION,
    ]
    assert all(not hasattr(outcome, "artifact") for outcome in result.outcomes[:-1])
    assert result.artifact is result.outcomes[-1].artifact


def test_unexpected_provider_error_is_sanitized_before_fallback() -> None:
    secret = "api_token=do-not-log"

    def broken(_request: AcquisitionRequest) -> object:
        raise RuntimeError(secret)

    controller = AcquisitionController(
        providers=(("broken", broken), ("fallback", lambda _request: {"ok": True})),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )
    result = controller.acquire(
        AcquisitionRequest(
            capability="news",
            source_ref="news:600895.SS",
            tool_call_id="call-6",
            tool_name="get_news",
        ),
        validator=lambda value: value,
        serializer=lambda _value: '{"ok":true}',
    )

    failed = result.outcomes[0]
    assert isinstance(failed, SourceAcquisitionUnavailable)
    assert failed.reason is AcquisitionUnavailableReason.PROVIDER_ERROR
    assert failed.retryable is False
    assert failed.http_status is None
    assert "RuntimeError" not in failed.model_dump_json()
    assert secret not in failed.model_dump_json()
    assert result.value == {"ok": True}


def test_unconfigured_provider_is_never_called() -> None:
    calls: list[str] = []

    def configured(_request: AcquisitionRequest) -> object:
        calls.append("configured")
        return {"ok": True}

    def unconfigured(_request: AcquisitionRequest) -> object:
        calls.append("unconfigured")
        return {"ok": False}

    controller = AcquisitionController(providers=(("configured", configured),))
    result = controller.acquire(
        AcquisitionRequest(
            capability="identity",
            source_ref="identity:600895.SS",
            tool_call_id="call-7",
            tool_name="resolve_identity",
        ),
        validator=lambda value: value,
        serializer=lambda _value: '{"ok":true}',
    )

    assert calls == ["configured"]
    assert result.value == {"ok": True}
    assert unconfigured is not configured


def test_acquisition_outcomes_round_trip_through_json_contract() -> None:
    controller = AcquisitionController(
        providers=(
            (
                "limited",
                lambda _request: (_ for _ in ()).throw(
                    AcquisitionFailure(
                        reason=AcquisitionUnavailableReason.RATE_LIMITED,
                        status_code=429,
                        retry_after_seconds=4,
                    )
                ),
            ),
            ("fallback", lambda _request: {"close": "8.80"}),
        ),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )
    result = controller.acquire(
        AcquisitionRequest(
            capability="daily_close",
            source_ref="market:600895.SS",
            tool_call_id="call-8",
            tool_name="get_market_data",
        ),
        validator=lambda value: value,
        serializer=lambda _value: '{"close":"8.80"}',
    )
    adapter = TypeAdapter(list[SourceAcquisitionAvailable | SourceAcquisitionUnavailable])
    payload = adapter.dump_json(list(result.outcomes))

    assert tuple(adapter.validate_json(payload)) == result.outcomes


def test_circuit_state_does_not_leak_between_controller_instances() -> None:
    calls = 0

    def provider(_request: AcquisitionRequest) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AcquisitionFailure(reason=AcquisitionUnavailableReason.TIMEOUT)
        return {"ok": True}

    request = AcquisitionRequest(
        capability="news",
        source_ref="news:601658.SS",
        tool_call_id="call-9",
        tool_name="get_news",
    )
    first_run = AcquisitionController(providers=(("vendor-a", provider),))
    second_run = AcquisitionController(providers=(("vendor-a", provider),))

    failed = first_run.acquire(
        request, validator=lambda value: value, serializer=lambda _value: "{}"
    )
    skipped = first_run.acquire(
        request.model_copy(update={"tool_call_id": "call-10"}),
        validator=lambda value: value,
        serializer=lambda _value: "{}",
    )
    succeeded = second_run.acquire(
        request, validator=lambda value: value, serializer=lambda _value: '{"ok":true}'
    )

    assert calls == 2
    assert failed.artifact is None
    assert skipped.outcomes[0].reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
    assert succeeded.value == {"ok": True}

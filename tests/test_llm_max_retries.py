"""Configurable LLM SDK retry budget (#1090/#1091).

A single transient 429 burst used to kill an otherwise-healthy multi-agent run
because each provider SDK's max_retries (default 2) was not exposed. This adds an
opt-in llm_max_retries knob forwarded to every provider chat client.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import httpx
import openai
import pytest
from langchain_core.messages import HumanMessage

import tradingagents.default_config as default_config_module
from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run
from tradingagents.graph.trading_graph import TradingAgentsGraph, _coerce_max_retries
from tradingagents.llm_clients.openai_client import OpenAIClient
from tradingagents.run_telemetry import RunTelemetryCallbackHandler


class _IncompleteResponseBody(httpx.SyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    def __iter__(self):
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)",
            request=self.request,
        )
        yield b""  # pragma: no cover - keeps this a generator


class _ScriptedOpenAITransport(httpx.BaseTransport):
    def __init__(
        self,
        outcomes: list[str],
        *,
        response_content: str = "safe result",
        response_tool_payload: str | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.response_content = response_content
        self.response_tool_payload = response_tool_payload
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes[len(self.requests) - 1]
        if outcome == "incomplete_body":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_IncompleteResponseBody(request),
                request=request,
            )
        if outcome == "status_500":
            return httpx.Response(
                500,
                headers={
                    "content-type": "application/json",
                    "x-request-id": f"safe-request-{len(self.requests)}",
                },
                json={
                    "error": {
                        "message": "safe upstream failure",
                        "type": "server_error",
                    }
                },
                request=request,
            )
        message = {"role": "assistant", "content": self.response_content}
        if self.response_tool_payload is not None:
            message["tool_calls"] = [
                {
                    "id": "safe-tool-call",
                    "type": "function",
                    "function": {
                        "name": "safe_tool",
                        "arguments": json.dumps(
                            {"payload": self.response_tool_payload}
                        ),
                    },
                }
            ]
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "x-request-id": f"safe-request-{len(self.requests)}",
            },
            content=json.dumps(
                {
                    "id": "chatcmpl-safe",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode(),
            request=request,
        )


class _AsyncIncompleteResponseBody(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    async def __aiter__(self):
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)",
            request=self.request,
        )
        yield b""  # pragma: no cover - keeps this an async generator


class _ScriptedAsyncOpenAITransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_AsyncIncompleteResponseBody(request),
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-safe",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "async safe"},
                        "finish_reason": "stop",
                    }
                ],
            },
            request=request,
        )


def _invoke_with_transport(
    transport: _ScriptedOpenAITransport,
    *,
    max_retries: int,
    api_key: str = "safe-test-key",
    base_url: str | None = None,
):
    with authoritative_snapshot_run() as run:
        http_client = httpx.Client(transport=transport)
        llm = OpenAIClient(
            model="deepseek-chat",
            base_url=base_url,
            provider="deepseek",
            api_key=api_key,
            max_retries=max_retries,
            callbacks=[RunTelemetryCallbackHandler(run.telemetry_ledger)],
            http_client=http_client,
        ).get_llm()
        try:
            yield llm, run.telemetry_ledger
        finally:
            http_client.close()

# --- coercion / validation -------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [(0, 0), (2, 2), (10, 10), ("6", 6)])
def test_coerce_accepts_non_negative_ints_and_numeric_strings(value, expected):
    assert _coerce_max_retries(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize("bad", [-1, "-3"])
def test_coerce_rejects_negative(bad):
    with pytest.raises(ValueError, match=">= 0"):
        _coerce_max_retries(bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", [True, False])
def test_coerce_rejects_booleans(bad):
    with pytest.raises(ValueError, match="boolean"):
        _coerce_max_retries(bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["abc", "1.5", None])
def test_coerce_rejects_non_integers(bad):
    with pytest.raises(ValueError, match="integer"):
        _coerce_max_retries(bad)


# --- forwarding into provider kwargs --------------------------------------

def _bare_graph(config):
    g = object.__new__(TradingAgentsGraph)
    g.config = config
    return g


@pytest.mark.unit
def test_not_forwarded_when_unset():
    kwargs = _bare_graph({"llm_provider": "openai", "llm_max_retries": None})._get_provider_kwargs()
    assert "max_retries" not in kwargs


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_forwarded_across_providers(provider):
    kwargs = _bare_graph({"llm_provider": provider, "llm_max_retries": 6})._get_provider_kwargs()
    assert kwargs["max_retries"] == 6


@pytest.mark.unit
def test_forwarded_env_string_is_coerced():
    # env vars arrive as strings; the consumer coerces (like temperature)
    kwargs = _bare_graph({"llm_provider": "openai", "llm_max_retries": "4"})._get_provider_kwargs()
    assert kwargs["max_retries"] == 4


@pytest.mark.unit
def test_invalid_config_value_fails_loudly():
    with pytest.raises(ValueError):
        _bare_graph({"llm_provider": "openai", "llm_max_retries": -1})._get_provider_kwargs()


# --- env overlay -----------------------------------------------------------

def _reload_with_env(monkeypatch, **overrides):
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


@pytest.mark.unit
def test_default_is_none(monkeypatch):
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["llm_max_retries"] is None


@pytest.mark.unit
def test_env_override_sets_config(monkeypatch):
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_LLM_MAX_RETRIES="8")
    # None-default key: env value arrives as a string and is coerced downstream.
    assert dc.DEFAULT_CONFIG["llm_max_retries"] == "8"
    assert _coerce_max_retries(dc.DEFAULT_CONFIG["llm_max_retries"]) == 8


@pytest.mark.unit
def test_incomplete_response_body_retains_all_three_physical_attempts():
    transport = _ScriptedOpenAITransport(["incomplete_body"] * 3)

    for llm, telemetry_ledger in _invoke_with_transport(transport, max_retries=2):
        with pytest.raises(openai.APIConnectionError) as raised:
            llm.invoke([HumanMessage(content="safe prompt")])
        projection = telemetry_ledger.finalize(terminal_route="operational_failure")

    cause = raised.value.__cause__
    seen = set()
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, httpx.RemoteProtocolError):
            break
        seen.add(id(cause))
        cause = cause.__cause__
    assert isinstance(cause, httpx.RemoteProtocolError)
    assert len(transport.requests) == 3
    assert len({id(request) for request in transport.requests}) == 3
    assert [request.headers["x-stainless-retry-count"] for request in transport.requests] == [
        "0",
        "1",
        "2",
    ]
    assert sum(stage.model_calls for stage in projection.stages.values()) == 1
    assert projection.model_transport is not None
    assert projection.model_transport.summary.physical_attempts == 3
    assert [event.attempt_ordinal for event in projection.model_transport.attempts] == [
        0,
        1,
        2,
    ]
    assert [event.retry_index for event in projection.model_transport.attempts] == [
        0,
        1,
        2,
    ]
    assert [event.disposition for event in projection.model_transport.attempts] == [
        "retryable_transport_failure",
        "retryable_transport_failure",
        "terminal_transport_failure",
    ]
    assert all(
        event.configured_max_retries == 2
        and event.exception_class == "RemoteProtocolError"
        and event.exception_family == "remote_protocol"
        and event.http_status == 200
        and event.provider_request_id is None
        and event.logical_call_id == "model-call-000001"
        for event in projection.model_transport.attempts
    )
    assert [
        item.model_dump(mode="json")
        for item in projection.model_transport.summary.by_provider_model
    ] == [
        {
            "provider_family": "deepseek",
            "model_id": "deepseek-chat",
            "physical_attempts": 3,
        }
    ]


@pytest.mark.unit
def test_retryable_transport_failure_then_success_retains_both_attempts():
    transport = _ScriptedOpenAITransport(["incomplete_body", "success"])

    for llm, telemetry_ledger in _invoke_with_transport(transport, max_retries=2):
        result = llm.invoke([HumanMessage(content="safe prompt")])
        projection = telemetry_ledger.finalize(terminal_route="analysis_outcome")

    assert result.content == "safe result"
    assert len(transport.requests) == 2
    assert sum(stage.model_calls for stage in projection.stages.values()) == 1
    assert projection.model_transport is not None
    assert projection.model_transport.summary.physical_attempts == 2
    assert projection.model_transport.summary.retries_observed == 1
    assert projection.model_transport.summary.successful_attempts == 1
    assert [event.disposition for event in projection.model_transport.attempts] == [
        "retryable_transport_failure",
        "success",
    ]
    assert projection.model_transport.attempts[1].provider_request_id == (
        "safe-request-2"
    )


@pytest.mark.unit
def test_immediate_success_retains_one_physical_attempt():
    transport = _ScriptedOpenAITransport(["success"])

    for llm, telemetry_ledger in _invoke_with_transport(transport, max_retries=2):
        result = llm.invoke([HumanMessage(content="safe prompt")])
        projection = telemetry_ledger.finalize(terminal_route="analysis_outcome")

    assert result.content == "safe result"
    assert len(transport.requests) == 1
    assert sum(stage.model_calls for stage in projection.stages.values()) == 1
    assert projection.model_transport is not None
    assert projection.model_transport.summary.physical_attempts == 1
    assert projection.model_transport.summary.retries_observed == 0
    assert [event.disposition for event in projection.model_transport.attempts] == [
        "success"
    ]


@pytest.mark.unit
def test_zero_retry_budget_retains_one_terminal_physical_attempt():
    transport = _ScriptedOpenAITransport(["incomplete_body"])

    for llm, telemetry_ledger in _invoke_with_transport(transport, max_retries=0):
        with pytest.raises(openai.APIConnectionError):
            llm.invoke([HumanMessage(content="safe prompt")])
        projection = telemetry_ledger.finalize(terminal_route="operational_failure")

    assert len(transport.requests) == 1
    assert sum(stage.model_calls for stage in projection.stages.values()) == 1
    assert projection.model_transport is not None
    assert projection.model_transport.summary.physical_attempts == 1
    assert projection.model_transport.summary.retries_observed == 0
    assert projection.model_transport.summary.retryable_failures == 0
    assert projection.model_transport.summary.terminal_failures == 1
    assert [event.disposition for event in projection.model_transport.attempts] == [
        "terminal_transport_failure"
    ]


@pytest.mark.unit
def test_transport_failure_evidence_excludes_secrets_and_model_payloads(tmp_path):
    from cli.runtime_artifacts import RuntimeArtifactWriter

    runtime_canary = "_".join(("TICKET07", "API", "KEY", "SENTINEL"))
    sentinels = {
        "api_key": runtime_canary,
        "authorization": "TICKET07_AUTHORIZATION_SENTINEL",
        "prompt": "TICKET07_PROMPT_SENTINEL",
        "request_body": "TICKET07_REQUEST_BODY_SENTINEL",
        "response": "TICKET07_RESPONSE_SENTINEL",
        "tool_payload": "TICKET07_TOOL_PAYLOAD_SENTINEL",
        "url_path": "TICKET07_URL_PATH_SENTINEL",
        "url_query": "TICKET07_URL_QUERY_SENTINEL",
    }
    transport = _ScriptedOpenAITransport(
        ["success", "incomplete_body"],
        response_content=sentinels["response"],
        response_tool_payload=sentinels["tool_payload"],
    )
    api_key = sentinels["api_key"] + sentinels["authorization"]

    for llm, telemetry_ledger in _invoke_with_transport(
        transport,
        max_retries=0,
        api_key=api_key,
        base_url=(
            "https://api.deepseek.com/"
            + sentinels["url_path"]
            + "?safe="
            + sentinels["url_query"]
        ),
    ):
        result = llm.invoke(
            [
                HumanMessage(
                    content=sentinels["prompt"] + sentinels["request_body"]
                )
            ]
        )
        with pytest.raises(openai.APIConnectionError):
            llm.invoke([HumanMessage(content=sentinels["prompt"])])
        projection = telemetry_ledger.finalize(terminal_route="operational_failure")

    assert sentinels["response"] in result.content
    assert sentinels["tool_payload"] in json.dumps(result.tool_calls)
    assert sentinels["api_key"] in transport.requests[0].headers["authorization"]
    assert sentinels["authorization"] in transport.requests[0].headers["authorization"]
    assert sentinels["prompt"] in transport.requests[0].content.decode()
    assert sentinels["request_body"] in transport.requests[0].content.decode()
    assert sentinels["url_path"] in str(transport.requests[0].url)
    assert sentinels["url_query"] in str(transport.requests[0].url)

    metrics_path = tmp_path / "runtime_metrics.json"
    writer = RuntimeArtifactWriter(
        artifact_root=tmp_path / "runtime_artifacts",
        log_paths=[tmp_path / "message_tool.log"],
        metrics_path=metrics_path,
    )
    writer.record_terminal_summary(
        terminal_route="operational_failure",
        run_telemetry=projection.model_dump(mode="json"),
    )
    writer.close()

    retained_bytes = projection.model_dump_json().encode() + b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    for sentinel in sentinels.values():
        assert sentinel.encode() not in retained_bytes


@pytest.mark.unit
def test_http_failure_retains_only_status_and_safe_request_id():
    transport = _ScriptedOpenAITransport(["status_500"])

    for llm, telemetry_ledger in _invoke_with_transport(transport, max_retries=0):
        with pytest.raises(openai.InternalServerError):
            llm.invoke([HumanMessage(content="safe prompt")])
        projection = telemetry_ledger.finalize(terminal_route="operational_failure")

    assert projection.model_transport is not None
    assert projection.model_transport.summary.physical_attempts == 1
    assert projection.model_transport.summary.status_failures == 1
    attempt = projection.model_transport.attempts[0]
    assert attempt.disposition == "status_failure"
    assert attempt.http_status == 500
    assert attempt.provider_request_id == "safe-request-1"
    assert attempt.exception_class is None
    assert attempt.exception_family is None


@pytest.mark.unit
def test_async_retry_then_success_uses_the_same_physical_attempt_contract():
    async def run_scenario():
        transport = _ScriptedAsyncOpenAITransport()
        with authoritative_snapshot_run() as run:
            http_client = httpx.AsyncClient(transport=transport)
            llm = OpenAIClient(
                model="deepseek-chat",
                provider="deepseek",
                api_key="test-key",
                max_retries=2,
                callbacks=[RunTelemetryCallbackHandler(run.telemetry_ledger)],
                http_async_client=http_client,
            ).get_llm()
            try:
                result = await llm.ainvoke([HumanMessage(content="safe prompt")])
                projection = run.telemetry_ledger.finalize(
                    terminal_route="analysis_outcome"
                )
            finally:
                await http_client.aclose()
        return result, projection, transport

    result, projection, transport = asyncio.run(run_scenario())

    assert result.content == "async safe"
    assert len(transport.requests) == 2
    assert sum(stage.model_calls for stage in projection.stages.values()) == 1
    assert projection.model_transport is not None
    assert [attempt.disposition for attempt in projection.model_transport.attempts] == [
        "retryable_transport_failure",
        "success",
    ]
    assert [attempt.retry_index for attempt in projection.model_transport.attempts] == [
        0,
        1,
    ]

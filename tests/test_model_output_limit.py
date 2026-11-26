"""An explicit output cap reaches the real SDK request, without live I/O."""
# ruff: noqa: SIM117 -- distinguish expected terminal errors from budget lifetime.
import inspect
import json
import sqlite3
import ssl
import threading
from types import SimpleNamespace

import pytest

from tests.test_analysis_budget import config, httpx_families, install_network_denial
from tradingagents.analysis_budget import (
    AnalysisBudgetStopped,
    analysis_budget_scope,
    checkpoint_budget_binding,
    validate_budget_config,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.openai_client import OpenAIClient


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    signatures = {transport: inspect.signature(transport.__init__)
                  for module in httpx_families()
                  for transport in (module.HTTPTransport, module.AsyncHTTPTransport)}
    install_network_denial(monkeypatch)
    # Retain transport settings in the network-only fake. The production
    # admission policy itself is never replaced or made test-aware.
    def settings_constructor(original, signature):
        def initialize(transport, *args, **kwargs):
            original(transport, *args, **kwargs)
            values = signature.bind(transport, *args, **kwargs)
            values.apply_defaults()
            verify = values.arguments["verify"]
            transport._pool._ssl_context = SimpleNamespace(
                verify_mode=ssl.CERT_NONE if verify is False else ssl.CERT_REQUIRED,
                check_hostname=verify is not False)
            transport._pool._proxy_url = values.arguments["proxy"]
            transport._pool._uds = values.arguments["uds"]
        return initialize
    for transport, signature in signatures.items():
        monkeypatch.setattr(transport, "__init__", settings_constructor(transport.__init__, signature))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setenv("NO_PROXY", "*")


def fake_model_wire(monkeypatch):
    bodies = []
    for module in httpx_families():
        def send(transport, request, family=module):
            bodies.append(json.loads(request.content))
            return family.Response(200, request=request, json={
                "id": "offline", "object": "chat.completion", "created": 1,
                "model": "deepseek-flash",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "synthetic"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            })
        monkeypatch.setattr(module.HTTPTransport, "handle_request", send)
    return bodies


def test_graph_cap_reaches_actual_deepseek_request(monkeypatch):
    bodies = fake_model_wire(monkeypatch)
    settings = config(llm_max_output_tokens=64, llm_max_retries=0)
    graph = object.__new__(TradingAgentsGraph)
    graph.config = settings
    model = OpenAIClient("deepseek-flash", provider="deepseek",
                         **graph._get_provider_kwargs()).get_llm()
    try:
        with analysis_budget_scope(settings) as budget:
            result = model.invoke("Synthetic only")
            assert result.content == "synthetic"
            assert result.usage_metadata["input_tokens"] == 7
            assert result.usage_metadata["output_tokens"] == 3
        assert bodies[0].get("max_tokens") == 64
        assert "max_completion_tokens" not in bodies[0]
        assert budget.receipt()["model_attempts"] == len(bodies) == 1
    finally:
        model.root_client.close()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "bad", "64.0", 393217])
def test_invalid_config_cap_rejected_before_client_construction(value):
    with pytest.raises(ValueError):
        validate_budget_config(config(llm_max_output_tokens=value))


@pytest.mark.parametrize("value", [1, 393216, "64"])
def test_graph_forwards_valid_cap(value):
    graph = object.__new__(TradingAgentsGraph)
    graph.config = config(llm_max_output_tokens=value)
    assert graph._get_provider_kwargs()["max_tokens"] == int(value)


def test_default_none_preserves_existing_binding_and_provider_kwargs():
    settings = config()
    assert checkpoint_budget_binding(settings, "graph") == checkpoint_budget_binding(
        {**settings, "llm_max_output_tokens": None}, "graph")
    graph = object.__new__(TradingAgentsGraph)
    graph.config = settings
    assert "max_tokens" not in graph._get_provider_kwargs()
    assert checkpoint_budget_binding(settings, "graph") != checkpoint_budget_binding(
        {**settings, "llm_max_output_tokens": 64}, "graph")


def test_output_cap_environment_is_applied_without_profile_read(monkeypatch):
    import os

    from tradingagents.default_config import _apply_env_overrides

    monkeypatch.setattr(os, "environ", {"TRADINGAGENTS_LLM_MAX_OUTPUT_TOKENS": "64"})
    settings = _apply_env_overrides(config(llm_max_output_tokens=None))
    graph = object.__new__(TradingAgentsGraph)
    graph.config = settings
    assert graph._get_provider_kwargs()["max_tokens"] == 64


@pytest.mark.parametrize("kwargs", [
    {"max_tokens": 65}, {"max_completion_tokens": 65},
    {"extra_body": {"max_tokens": 1}}, {"extra_body": {"max_completion_tokens": 1}},
])
def test_sdk_per_call_output_override_cannot_escape_config(monkeypatch, kwargs):
    bodies = fake_model_wire(monkeypatch)
    model = OpenAIClient("deepseek-flash", provider="deepseek", max_tokens=64, max_retries=0).get_llm()
    try:
        with analysis_budget_scope(config(llm_max_output_tokens=64)) as budget:
            with pytest.raises(ValueError):
                model.invoke("Synthetic only", **kwargs)
        assert not bodies and budget.receipt()["model_attempts"] == 0
    finally:
        model.root_client.close()


@pytest.mark.parametrize("body", [{}, {"max_tokens": 65}, {"max_tokens": True},
                                  {"max_tokens": 1, "max_completion_tokens": 1}])
def test_actual_final_body_is_checked_before_sync_send(monkeypatch, body):
    bodies = fake_model_wire(monkeypatch)
    for module in httpx_families():
        with module.Client(trust_env=False) as client:
            client._tradingagents_budget_provider = "deepseek"
            with pytest.raises(AnalysisBudgetStopped, match="model_output_limit_unbound"):
                with analysis_budget_scope(config(llm_max_output_tokens=64)) as budget:
                    client.post("https://api.deepseek.com/chat/completions", json=body)
            assert budget.receipt()["model_attempts"] == 0
    assert not bodies


def test_actual_async_send_cap_and_override(monkeypatch):
    import sniffio

    for module in httpx_families():
        calls = []

        async def send(_transport, request, family=module, captured=calls):
            captured.append(json.loads(request.content))
            return family.Response(200, request=request, json={"synthetic": True})

        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", send)

        async def run(family=module):
            async with family.AsyncClient(trust_env=False) as client:
                client._tradingagents_budget_provider = "deepseek"
                with pytest.raises(AnalysisBudgetStopped, match="model_output_limit_unbound"):
                    with analysis_budget_scope(config(llm_max_output_tokens=64)) as budget:
                        await client.post("https://api.deepseek.com/chat/completions", json={"max_tokens": 64})
                        await client.post("https://api.deepseek.com/chat/completions", json={"max_tokens": 65})
                assert budget.receipt()["model_attempts"] == 1

        # The real async HTTPX request path completes inline with this fake wire.
        # No event loop or extra socket is required for the transport assertion.
        coroutine = run()
        token = sniffio.current_async_library_cvar.set("asyncio")
        try:
            with pytest.raises(StopIteration):
                coroutine.send(None)
        finally:
            coroutine.close()
            sniffio.current_async_library_cvar.reset(token)
        assert calls == [{"max_tokens": 64}]


def test_model_same_host_redirect_is_terminal(monkeypatch):
    for module in httpx_families():
        calls = []

        def send(_transport, request, family=module, captured=calls):
            captured.append(request)
            return family.Response(307, request=request,
                                   headers={"location": "https://api.deepseek.com/v1/chat/completions"})

        monkeypatch.setattr(module.HTTPTransport, "handle_request", send)
        with module.Client(trust_env=False, follow_redirects=True) as client:
            client._tradingagents_budget_provider = "deepseek"
            with pytest.raises(AnalysisBudgetStopped, match="model_redirect_forbidden"):
                with analysis_budget_scope(config(llm_max_output_tokens=64)) as budget:
                    client.post("https://api.deepseek.com/chat/completions", json={"max_tokens": 64})
        assert len(calls) == budget.receipt()["model_attempts"] == 1


@pytest.mark.parametrize("setting", ["proxy", "tls", "uds"])
def test_model_transport_cannot_change_recipient_or_disable_tls(monkeypatch, setting):
    bodies = fake_model_wire(monkeypatch)
    for module in httpx_families():
        kwargs = {"proxy": {"proxy": "http://proxy.invalid"},
                  "tls": {"verify": False}, "uds": {"uds": "synthetic.sock"}}[setting]
        transport = module.HTTPTransport(**kwargs)
        with module.Client(transport=transport, trust_env=False) as client:
            client._tradingagents_budget_provider = "deepseek"
            with pytest.raises(AnalysisBudgetStopped, match="uncovered_model_transport_settings"):
                with analysis_budget_scope(config(llm_max_output_tokens=64)) as budget:
                    client.post("https://api.deepseek.com/chat/completions", json={"max_tokens": 64})
            assert budget.receipt()["model_attempts"] == 0
    assert not bodies


def test_output_cap_change_cannot_reset_existing_checkpoint_budget(tmp_path, monkeypatch):
    bodies = fake_model_wire(monkeypatch)
    path = tmp_path / "checkpoint.sqlite3"
    settings = config(checkpoint_enabled=True, llm_max_output_tokens=64)
    with sqlite3.connect(path) as db, pytest.raises(KeyboardInterrupt):
        with analysis_budget_scope(settings) as budget:
            with budget.checkpoint_session(db, threading.RLock(), "same-thread", "same-graph", has_checkpoint=False):
                module = httpx_families()[0]
                with module.Client(trust_env=False) as client:
                    client._tradingagents_budget_provider = "deepseek"
                    client.post("https://api.deepseek.com/chat/completions", json={"max_tokens": 64})
                raise KeyboardInterrupt("synthetic interruption")
    before = path.read_bytes()
    with sqlite3.connect(path) as db, pytest.raises(AnalysisBudgetStopped, match="checkpoint_budget_mismatch"):
        with analysis_budget_scope({**settings, "llm_max_output_tokens": 128}) as resumed:
            with resumed.checkpoint_session(db, threading.RLock(), "same-thread", "same-graph", has_checkpoint=True):
                pytest.fail("changed cap accepted")
    assert path.read_bytes() == before
    assert len(bodies) == 1

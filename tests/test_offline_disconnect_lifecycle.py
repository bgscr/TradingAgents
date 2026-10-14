"""HTTP body failure through the real SDK and CLI terminal-artifact boundary."""

import json

import httpx
import openai
import pytest
from langchain_core.messages import HumanMessage

from cli import main as cli_main
from tests.test_cli_run_status import NoOpDisplay, _run_selections
from tests.test_llm_max_retries import _ScriptedOpenAITransport
from tradingagents.graph.propagation import Propagator
from tradingagents.llm_clients.openai_client import OpenAIClient


def test_five_body_failures_end_cli_run_without_outer_retry(tmp_path, monkeypatch):
    transport = _ScriptedOpenAITransport(["incomplete_body"] * 5)
    monkeypatch.setattr(cli_main, "get_user_selections", _run_selections)
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", dict(
        cli_main.DEFAULT_CONFIG, results_dir=str(tmp_path), checkpoint_enabled=False,
    ))
    display = NoOpDisplay()
    monkeypatch.setattr(cli_main, "create_run_display", lambda *a, **k: display)

    class OfflineGraph:
        def __init__(self, *args, **kwargs):
            self.propagator = Propagator()
            self.graph = self

        def create_initial_state(self, *args, **kwargs):
            return {"messages": []}

        def resolve_evidence_state(self, *args, **kwargs):
            return None

        def stream(self, *args, config, **kwargs):
            with httpx.Client(transport=transport) as http_client:
                llm = OpenAIClient(
                    "deepseek-flash", provider="deepseek", api_key="offline-test",
                    http_client=http_client, max_retries=4,
                ).get_llm()
                # Skip elapsed backoff in this deterministic transport probe;
                # the SDK request loop and production retry budget stay real.
                monkeypatch.setattr(llm.root_client, "_sleep_for_retry", lambda **kwargs: None)
                llm.invoke([HumanMessage(content="Synthetic fundamentals")], config=config)
            yield {}  # pragma: no cover - transport must fail before any update

    monkeypatch.setattr(cli_main, "TradingAgentsGraph", OfflineGraph)
    with pytest.raises(openai.APIConnectionError) as error:
        cli_main.run_analysis()

    # LangChain versions may add their own connection-error wrapper around
    # the SDK exception. The transport cause must survive the full chain.
    cause = error.value
    seen = set()
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, httpx.RemoteProtocolError):
            break
        seen.add(id(cause))
        cause = cause.__cause__
    assert isinstance(cause, httpx.RemoteProtocolError)
    assert len(transport.requests) == 5
    assert [r.headers["x-stainless-retry-count"] for r in transport.requests] == ["0", "1", "2", "3", "4"]
    status_path, = tmp_path.rglob("run_status.json")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["current_phase"] is None
    assert status["failed_phase"] == "graph_stream"
    assert status["operational_error_category"] == "graph_execution"
    assert display.closed
    metrics = json.loads((status_path.parent / "runtime_metrics.json").read_text(encoding="utf-8"))
    attempts = metrics["terminal"]["model_transport"]["attempts"]
    assert len(attempts) == 5
    assert {attempt["http_status"] for attempt in attempts} == {200}
    assert {attempt["exception_class"] for attempt in attempts} == {"RemoteProtocolError"}
    assert [a["disposition"] for a in attempts] == [
        "retryable_transport_failure", "retryable_transport_failure",
        "retryable_transport_failure", "retryable_transport_failure", "terminal_transport_failure",
    ]
    assert len({a["logical_call_id"] for a in attempts}) == 1

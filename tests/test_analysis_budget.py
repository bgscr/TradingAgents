"""Offline physical-boundary tests; all responses and credentials are fixed fakes."""
# ruff: noqa: SIM117 -- keep terminal exception scopes distinct from run scopes.

import io
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

import httpx
import pytest
import requests
from openai import OpenAI
from urllib3.response import HTTPResponse

from tradingagents.analysis_budget import (
    AnalysisBudget,
    AnalysisBudgetStopped,
    active_budget,
    analysis_budget_scope,
    require_data_vendor,
    validate_budget_config,
)
from tradingagents.dataflows.current_statement_cache import (
    CurrentStatementCache,
    StatementCacheScope,
)


def config(**overrides):
    return {
        "analysis_budget_enabled": True,
        "analysis_max_calls": 3,
        "analysis_timeout_seconds": 60,
        "llm_provider": "deepseek",
        "market_history_mode": "current",
        "mainland_capability_routing_mode": "legacy",
        "checkpoint_enabled": False,
        **overrides,
    }


def model_client():
    client = httpx.Client(trust_env=False)
    client._tradingagents_budget_provider = "deepseek"
    return client


def data_get(url):
    with requests.Session() as session:
        session.trust_env = False
        return session.get(url)


def fake_httpx(monkeypatch, statuses=(200,)):
    calls = []

    def send(_transport, request):
        calls.append(request)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        return httpx.Response(status, request=request, headers={"retry-after-ms": "1"},
                              json={"id": "fake", "choices": [{"index": 0, "message": {
                                  "role": "assistant", "content": "fake"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    return calls


def fake_requests(monkeypatch, statuses=(200,)):
    from urllib3.connectionpool import HTTPConnectionPool

    calls = []

    def send(_pool, method, url, *args, **kwargs):
        calls.append((method, url))
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        return HTTPResponse(body=io.BytesIO(b"fake"), status=status,
                            headers={"location": "https://fake.invalid/redirect"} if status == 302 else {})

    monkeypatch.setattr(HTTPConnectionPool, "urlopen", send)
    return calls


def test_mixed_physical_sends_share_exact_limit(monkeypatch):
    models, data = fake_httpx(monkeypatch), fake_requests(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped, match="call_limit_exceeded"):
        with analysis_budget_scope(config()) as budget, model_client() as client:
            client.post("https://fake.invalid/model")
            data_get("https://fake.invalid/data")
            client.post("https://fake.invalid/repair")
            data_get("https://fake.invalid/excess")
    assert (len(models), len(data)) == (2, 1)
    assert budget.receipt()["admitted_attempts"] == 3
    assert active_budget() is None


def test_sdk_retries_are_physical_and_stop_signal_is_not_retried(monkeypatch):
    calls = fake_httpx(monkeypatch, (500,))
    with model_client() as http:
        client = OpenAI(api_key="test", organization="", project="", max_retries=4,
                        http_client=http, base_url="https://fake.invalid")
        with pytest.raises(AnalysisBudgetStopped, match="call_limit_exceeded"):
            with analysis_budget_scope(config(analysis_max_calls=2)) as budget:
                client.chat.completions.create(model="fake", messages=[{"role": "user", "content": "fake"}])
    assert len(calls) == 2
    assert budget.receipt()["model_attempts"] == 2


def test_structured_repair_and_freetext_fallback_use_same_limit(monkeypatch):
    from tradingagents.agents.utils.structured import invoke_required_structured

    calls = fake_httpx(monkeypatch)
    with model_client() as client:
        class Structured:
            def invoke(self, prompt):
                client.post("https://fake.invalid/structured")
                return {"parsed": None, "parsing_error": None, "raw": None}

        with analysis_budget_scope(config(analysis_max_calls=2)) as budget:
            result = invoke_required_structured(Structured(), "fake", "fake")
            assert result.attempts == 2
        assert budget.receipt()["model_attempts"] == len(calls) == 2
        with pytest.raises(AnalysisBudgetStopped, match="call_limit_exceeded"):
            with analysis_budget_scope(config(analysis_max_calls=1)):
                invoke_required_structured(Structured(), "fake", "fake")
    assert len(calls) == 3


def test_concurrent_physical_sends_admit_only_limit(monkeypatch):
    calls = fake_httpx(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped), analysis_budget_scope(config(analysis_max_calls=5)) as budget:
        with model_client() as client, ThreadPoolExecutor(max_workers=12) as executor:
            def send(_):
                try:
                    client.post("https://fake.invalid/model")
                except AnalysisBudgetStopped:
                    return False
                return True

            results = list(executor.map(send, range(30)))
        assert sum(results) <= 5
        assert len(calls) == budget.receipt()["admitted_attempts"] == 5
        # A caught terminal signal must also terminate the outer scope.
        with pytest.raises(AnalysisBudgetStopped):
            budget.check()


def test_redirects_each_consume_send(monkeypatch):
    calls = fake_requests(monkeypatch, (302, 200))
    with pytest.raises(AnalysisBudgetStopped):
        with analysis_budget_scope(config(analysis_max_calls=1)):
            data_get("https://fake.invalid/start")
    assert len(calls) == 1


def test_deadline_prevents_send_and_late_result(monkeypatch):
    calls = fake_httpx(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped, match="deadline_exceeded"):
        with analysis_budget_scope(config()) as budget, model_client() as client:
            budget._started -= 61
            client.get("https://fake.invalid")
    assert calls == []

    def late(_transport, request):
        active_budget()._started -= 61
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", late)
    with pytest.raises(AnalysisBudgetStopped, match="deadline_exceeded"):
        with analysis_budget_scope(config()) as budget, model_client() as client:
            client.get("https://fake.invalid")
    assert budget.receipt()["admitted_attempts"] == 1


def test_deadline_timeout_is_clamped(monkeypatch):
    calls = fake_httpx(monkeypatch)
    with analysis_budget_scope(config(analysis_timeout_seconds=2)), model_client() as client:
        client.get("https://fake.invalid", timeout=100)
    assert all(0 < value <= 2 for value in calls[0].extensions["timeout"].values())


def test_async_actual_send_and_limit(monkeypatch):
    import sniffio

    calls = []

    async def send(_transport, request):
        calls.append(request)
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)

    async def close_fake_pool(*args):
        # Fake handle_async_request never opens a pool connection. Avoid real
        # loop cancellation machinery solely for this socket-free coroutine test.
        pass

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__aexit__", close_fake_pool)

    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            client._tradingagents_budget_provider = "deepseek"
            with analysis_budget_scope(config(analysis_max_calls=1)):
                await client.get("https://fake.invalid")
                await client.get("https://fake.invalid")

    # Fake async transport completes inline: no real Windows socketpair/loop.
    coroutine = run()
    token = sniffio.current_async_library_cvar.set("asyncio")
    try:
        with pytest.raises(AnalysisBudgetStopped, match="call_limit_exceeded"):
            coroutine.send(None)
    finally:
        coroutine.close()
        sniffio.current_async_library_cvar.reset(token)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["httpx", "native", "socket", "vendor", "adapter"])
def test_uncovered_transports_stop_before_send(kind):
    with pytest.raises(AnalysisBudgetStopped, match="uncovered"):
        with analysis_budget_scope(config()):
            if kind == "httpx":
                with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("sent"))) as client:
                    client.get("https://fake.invalid")
            elif kind == "native":
                from curl_cffi import Curl
                curl = Curl()
                try:
                    curl.perform()
                finally:
                    curl.close()
            elif kind == "socket":
                with socket.socket() as sock:
                    sock.connect(("127.0.0.1", 1))
            elif kind == "vendor":
                require_data_vendor("baostock")
            else:
                with requests.Session() as session:
                    session.trust_env = False
                    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=3))
                    session.get("https://fake.invalid")


def test_disabled_retains_unlimited_transport_and_restores_hooks(monkeypatch):
    calls = fake_httpx(monkeypatch)
    original = httpx.Client._send_single_request
    with analysis_budget_scope({}) as budget, model_client() as client:
        for _ in range(5):
            client.get("https://fake.invalid")
    assert budget is None and len(calls) == 5
    assert original is httpx.Client._send_single_request


@pytest.mark.parametrize("overrides", [
    {"analysis_max_calls": True}, {"analysis_max_calls": 0},
    {"analysis_timeout_seconds": float("nan")}, {"analysis_timeout_seconds": -1},
    {"llm_provider": "openai"}, {"checkpoint_enabled": True},
    {"financial_cache_only": True}, {"analysis_budget_enabled": "false"},
])
def test_invalid_configuration_rejected(overrides):
    with pytest.raises(ValueError):
        validate_budget_config(config(**overrides))


def test_latched_stop_and_concurrent_run_do_not_reset_budget():
    with analysis_budget_scope(config()) as first:
        with pytest.raises(AnalysisBudgetStopped, match="concurrent_analysis"):
            with analysis_budget_scope(config()):
                pytest.fail("nested budget")
        assert active_budget() is first
        first.check()
    clock = [1.0]
    budget = AnalysisBudget(1, 1, clock=lambda: clock[0])
    budget.admit("data")
    clock[0] += 2
    with pytest.raises(AnalysisBudgetStopped, match="deadline"):
        budget.check()


@pytest.mark.parametrize("first,second", [(False, True), (True, False), (True, True)])
def test_process_exclusivity_in_both_start_orders(first, second):
    with analysis_budget_scope(config(analysis_budget_enabled=first)):
        with pytest.raises(AnalysisBudgetStopped, match="concurrent_analysis"), analysis_budget_scope(
            config(analysis_budget_enabled=second)
        ):
            pytest.fail("overlapping run admitted")


def test_freetext_fallback_counts_physical_send(monkeypatch):
    from types import SimpleNamespace

    from tradingagents.agents.utils.structured import invoke_structured_or_freetext

    calls = fake_httpx(monkeypatch)
    with model_client() as client:
        class Structured:
            def invoke(self, prompt):
                client.post("https://fake.invalid/structured")
                return None

        class Plain:
            def invoke(self, prompt):
                client.post("https://fake.invalid/fallback")
                return SimpleNamespace(content="fake")

        with analysis_budget_scope(config(analysis_max_calls=2)):
            assert invoke_structured_or_freetext(Structured(), Plain(), "fake", str, "fake") == "fake"
        with pytest.raises(AnalysisBudgetStopped), analysis_budget_scope(config(analysis_max_calls=1)):
            invoke_structured_or_freetext(Structured(), Plain(), "fake", str, "fake")
    assert len(calls) == 3


def test_only_existing_loop_wakeup_socket_is_exempt(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    writes = []
    with socket.socket() as internal, socket.socket() as unrelated:
        monkeypatch.setattr(socket.socket, "send", lambda sock, value: writes.append(sock) or len(value))
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: SimpleNamespace(_ssock=internal, _csock=internal))
        with pytest.raises(AnalysisBudgetStopped), analysis_budget_scope(config()) as budget:
            assert internal.send(b"wake") == 4
            unrelated.send(b"external")
    assert writes == [internal]
    assert budget.receipt()["admitted_attempts"] == 0


def test_sleep_is_clamped_and_deadline_latched(monkeypatch):
    import time

    delays = []

    def elapsed(delay):
        delays.append(delay)
        active_budget()._started -= 61

    monkeypatch.setattr(time, "sleep", elapsed)
    with pytest.raises(AnalysisBudgetStopped, match="deadline"), analysis_budget_scope(config()):
        time.sleep(600)
    assert len(delays) == 1 and 0 < delays[0] <= 60


def test_deepseek_factory_client_is_instrumented(monkeypatch):
    import os

    from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run
    from tradingagents.llm_clients.openai_client import OpenAIClient

    calls = fake_httpx(monkeypatch)
    # A fixed synthetic mapping, never the caller's environment.
    monkeypatch.setattr(os, "environ", {"DEEPSEEK_API_KEY": "test", "NO_PROXY": "*"})
    llm = OpenAIClient("deepseek-chat", provider="deepseek", max_retries=0).get_llm()
    try:
        with authoritative_snapshot_run() as snapshot:
            with pytest.raises(AnalysisBudgetStopped), analysis_budget_scope(config(analysis_max_calls=1)):
                assert llm.invoke("fake").content == "fake"
                llm.invoke("fake")
        assert snapshot.telemetry_ledger.finalize().model_transport.summary.physical_attempts == 1
    finally:
        llm.root_client.close()
    assert len(calls) == 1


def test_explicit_financial_https_stops_before_second_post(monkeypatch):
    import json

    from tradingagents import tushare_financial_https as financial

    calls = []

    class Response:
        status = 200

        def read(self, limit):
            return json.dumps({"code": 0, "data": {"fields": ["ts_code", "ann_date", "end_date"], "items": []}}).encode()

    class Connection:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs["timeout"]

        def request(self, method, path, **kwargs):
            calls.append((method, path))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(financial.http.client, "HTTPSConnection", Connection)
    client = financial._Client("https://api.waditu.com/dataapi", "test")
    with pytest.raises(AnalysisBudgetStopped), analysis_budget_scope(config(analysis_max_calls=1)) as budget:
        for _ in range(2):
            client.query("income", ts_code="600895.SH", start_date="20200101", end_date="20261016", report_type="1")
    assert calls == [("POST", "/dataapi/income")]
    assert budget.receipt()["data_attempts"] == 1


def test_routed_fallback_is_rejected_before_uncovered_vendor(monkeypatch):
    from tradingagents.dataflows import interface

    calls = fake_requests(monkeypatch)

    def first(*args, **kwargs):
        data_get("https://fake.invalid/data")
        raise ValueError("fake source failure")

    monkeypatch.setattr(interface, "get_vendor", lambda *args: "akshare,yfinance")
    monkeypatch.setitem(interface.VENDOR_METHODS, "get_news", {
        "akshare": first, "yfinance": lambda *a, **k: pytest.fail("unsupported fallback sent"),
    })
    with pytest.raises(AnalysisBudgetStopped, match="uncovered_data_vendor"), analysis_budget_scope(config()) as budget:
        interface.route_to_vendor("get_news", "600895.SS", "2026-10-15", "2026-10-16")
    assert len(calls) == budget.receipt()["data_attempts"] == 1


def test_cli_status_retains_safe_budget_receipt(tmp_path):
    import json

    from cli.main import _update_run_status, _write_run_status

    path = tmp_path / "run_status.json"
    _write_run_status(path, {})
    budget = AnalysisBudget(1, 60)
    budget.admit("model")
    with pytest.raises(AnalysisBudgetStopped):
        budget.admit("data")
    _update_run_status({"status_file": path, "analysis_budget": budget}, status="failed")
    receipt = json.loads(path.read_text(encoding="utf-8"))["analysis_budget"]
    assert receipt["admitted_attempts"] == 1
    assert receipt["stop_reason"] == "call_limit_exceeded"
    assert receipt["financial_cache_hits"] == []


def test_programmatic_propagate_owns_budget_and_failure_receipt(monkeypatch):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    calls = fake_httpx(monkeypatch)
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = config(analysis_max_calls=1)
    graph.asset_type = "stock"
    graph.asset_configuration = None
    monkeypatch.setattr(graph, "_configure_asset_type", lambda _: None)
    monkeypatch.setattr(graph, "interrupt_qualified_routing_runtime", lambda: None)
    monkeypatch.setattr(graph, "close_qualified_routing_runtime", lambda: None)

    def run(*args, **kwargs):
        with model_client() as client:
            client.get("https://fake.invalid/first")
            client.get("https://fake.invalid/excess")

    monkeypatch.setattr(graph, "_run_graph", run)
    with pytest.raises(AnalysisBudgetStopped):
        graph.propagate("600895.SS", "2026-10-16")
    assert len(calls) == graph.last_analysis_budget["admitted_attempts"] == 1


def test_new_environment_flags_cannot_silently_disable_on_typo(monkeypatch):
    import os

    from tradingagents.default_config import _apply_env_overrides

    monkeypatch.setattr(os, "environ", {"TRADINGAGENTS_ANALYSIS_BUDGET_ENABLED": "tru"})
    with pytest.raises(ValueError):
        _apply_env_overrides(config())


@pytest.mark.parametrize("change", ["same", "missing", "stale", "changed"])
def test_rehydrated_financial_ledger_revalidates_original_cache(tmp_path, monkeypatch, change):
    import sqlite3

    from tests.test_current_tushare_statements import (
        TOOLS,
        current_statements,
        dispatcher,
        request,
        runtime_config,
        statement_rows,
    )

    settings = runtime_config(tmp_path)
    settings["tushare_financial_https_endpoint"] = "https://api.tushare.pro"

    class Client:
        def query(self, endpoint, **params):
            return statement_rows(endpoint)

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Client())
    seed = dispatcher(settings)
    for tool in TOOLS:
        assert seed.dispatch(request(tool)).artifact is not None
    settings["financial_cache_only"] = True
    settings.update(config())
    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: pytest.fail("network refresh"))
    with analysis_budget_scope(settings) as budget:
        first = dispatcher(settings)
        original = first.dispatch(request("get_balance_sheet"))
        assert original.artifact is not None
        ledger = first.checkpoint_ledger()
    assert budget.receipt()["admitted_attempts"] == 0
    assert len(budget.receipt()["financial_cache_hits"]) == 1
    path = tmp_path / "cache" / "current-financial-statements.sqlite3"
    # Mutate only this test's fake database to simulate expiry/removal/revision.
    with sqlite3.connect(path) as connection:
        if change == "missing":
            connection.execute("DELETE FROM active_windows")
        elif change in {"stale", "changed"}:
            observed = datetime.now(timezone.utc) - timedelta(days=2 if change == "stale" else 0, seconds=5)
            connection.execute("UPDATE observations SET checked_at=?", (observed.isoformat(),))
    before = sha256(path.read_bytes()).hexdigest()
    if change == "same":
        with analysis_budget_scope(settings) as budget:
            restored = dispatcher(settings, checkpoint_ledger=ledger)
            assert restored.dispatch(request("get_balance_sheet")).value == original.value
        assert len(budget.receipt()["financial_cache_hits"]) == 1
    else:
        with pytest.raises(AnalysisBudgetStopped, match="financial_cache"), analysis_budget_scope(settings):
            restored = dispatcher(settings, checkpoint_ledger=ledger)
            restored.dispatch(request("get_balance_sheet"))
    assert sha256(path.read_bytes()).hexdigest() == before


SCOPE = StatementCacheScope("fake", "identity", "income", "transport", "v1")
START, END = date(2020, 1, 1), date(2026, 10, 16)


@pytest.mark.parametrize("age,tail,reason", [
    (timedelta(days=2), False, "stale"),
    (timedelta(0), True, "missing"),
    (timedelta(days=-1), False, "stale"),
])
def test_cache_only_real_clock_and_window_stop_without_refresh(tmp_path, age, tail, reason):
    path = tmp_path / "fake.sqlite3"
    CurrentStatementCache(path).read_through(
        SCOPE, START, END, datetime.now(timezone.utc) - age, lambda *_: b'{"fake":1}')
    before = sha256(path.read_bytes()).hexdigest()
    with pytest.raises(AnalysisBudgetStopped, match=reason):
        CurrentStatementCache(path, read_only=True).read_through(
            SCOPE, START, END + timedelta(days=int(tail)),
            # Deliberately old, caller-supplied time cannot keep a cache fresh.
            datetime(2026, 10, 15, tzinfo=timezone.utc), lambda *_: pytest.fail("refresh"))
    assert sha256(path.read_bytes()).hexdigest() == before


def test_absent_cache_only_does_not_create_directory(tmp_path):
    path = tmp_path / "absent" / "fake.sqlite3"
    with pytest.raises(AnalysisBudgetStopped):
        CurrentStatementCache(path, read_only=True).read_through(
            SCOPE, START, END, datetime.now(timezone.utc), lambda *_: pytest.fail("refresh"))
    assert not path.parent.exists()


def test_cache_only_preserves_bytes_and_original_timestamps(tmp_path):
    path = tmp_path / "fake.sqlite3"
    now = datetime.now(timezone.utc)
    original = CurrentStatementCache(path).read_through(SCOPE, START, END, now, lambda *_: b"[]")
    before = sha256(path.read_bytes()).hexdigest()
    result = CurrentStatementCache(path, read_only=True).read_through(
        SCOPE, START, END, now - timedelta(days=100), lambda *_: pytest.fail("refresh"))
    assert result.chunks == original.chunks
    assert result.fetched_ranges == ()
    assert sha256(path.read_bytes()).hexdigest() == before

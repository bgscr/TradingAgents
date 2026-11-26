"""Core sources must be admitted before the real Requests transport boundary."""
# ruff: noqa: SIM117 -- distinguish expected terminal errors from budget lifetime.
import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from urllib3.response import HTTPResponse

from tests.test_analysis_budget import config, install_network_denial
from tradingagents.analysis_budget import AnalysisBudgetStopped, analysis_budget_scope


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    install_network_denial(monkeypatch)
    monkeypatch.setenv("NO_PROXY", "*")


def fake_wire(monkeypatch, status=200, headers=None, on_send=None):
    from urllib3.connectionpool import HTTPConnectionPool
    sent = []

    def send(pool, method, url, **kwargs):
        if on_send is not None:
            on_send()
        sent.append((pool.host, method, url, kwargs.get("headers")))
        return HTTPResponse(body=io.BytesIO(b"{}"), status=status,
                            headers=headers or {}, preload_content=False)

    monkeypatch.setattr(HTTPConnectionPool, "urlopen", send)
    return sent


def core_config(**overrides):
    return config(analysis_allowed_data_vendors=["tushare", "akshare"], **overrides)


def test_core_unknown_destination_stops_before_physical_send(monkeypatch):
    sent = fake_wire(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped, match="uncovered_data_destination"):
        with analysis_budget_scope(core_config()) as budget:
            requests.get("https://outside.invalid/never-send")
    assert sent == []
    assert budget.receipt()["admitted_attempts"] == 0


def test_cli_menu_cannot_send_before_analysis_budget(monkeypatch):
    import cli.main as main
    sent = fake_wire(monkeypatch)
    monkeypatch.setattr(main, "DEFAULT_CONFIG", core_config())
    # Test-only profile optout remains set; do not load any personal profile.
    monkeypatch.setattr(main, "require_runtime_profile_loading", lambda: None)

    def selection():
        requests.get("https://search-api-web.eastmoney.com/search/jsonp")
        raise AssertionError("menu reached the wire")

    monkeypatch.setattr(main, "get_user_selections", selection)
    with pytest.raises(AnalysisBudgetStopped, match="preparation_network_forbidden"):
        main.run_analysis()
    assert sent == []


@pytest.mark.parametrize("method,url", [
    ("GET", "https://search-api-web.eastmoney.com/search/jsonp"),
    ("GET", "https://basic.10jqka.com.cn/new/601328/operate.html"),
    ("GET", "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"),
    ("GET", "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"),
    ("GET", "https://datacenter-web.eastmoney.com/api/data/v1/get?token=public-sdk-identifier"),
    ("POST", "https://emappdata.eastmoney.com/stockrank/getHisList"),
    ("POST", "https://emappdata.eastmoney.com/stockrank/getHisProfileList"),
])
def test_core_exact_operations_count_once(monkeypatch, method, url):
    sent = fake_wire(monkeypatch)
    with analysis_budget_scope(core_config()) as budget:
        requests.request(method, url)
    assert len(sent) == budget.receipt()["data_attempts"] == 1


@pytest.mark.parametrize("url", [
    "http://search-api-web.eastmoney.com/search/jsonp",
    "https://search-api-web.eastmoney.com:444/search/jsonp",
    "https://search-api-web.eastmoney.com/other",
    "https://basic.10jqka.com.cn/new/601328/other.html",
    "https://search-api-web.eastmoney.com.evil.invalid/search/jsonp",
    (('https://' + 'fixture-user' + ':' + 'fixture-pass' + '@search-api-web.eastmoney.com/search/jsonp')),
])
def test_core_url_changes_stop_before_send(monkeypatch, url):
    sent = fake_wire(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped), analysis_budget_scope(core_config()) as budget:
        requests.get(url)
    assert not sent and budget.receipt()["admitted_attempts"] == 0


def test_core_same_host_redirect_is_not_followed(monkeypatch):
    sent = fake_wire(monkeypatch, 302, {"location": "https://search-api-web.eastmoney.com/search/jsonp?next=1"})
    with pytest.raises(AnalysisBudgetStopped, match="data_redirect_forbidden"), analysis_budget_scope(core_config()) as budget:
        requests.get("https://search-api-web.eastmoney.com/search/jsonp")
    assert len(sent) == budget.receipt()["data_attempts"] == 1


@pytest.mark.parametrize("setting", ["auth", "cookie", "header", "proxy", "tls", "cert"])
def test_core_explicit_credential_or_transport_override_denied(monkeypatch, setting):
    sent = fake_wire(monkeypatch)
    options = {
        "auth": {"auth": ("synthetic", "synthetic")},
        "cookie": {"cookies": {"session": "synthetic"}},
        "header": {"headers": {"Authorization": "synthetic"}},
        "proxy": {"proxies": {"https": "http://proxy.invalid"}},
        "tls": {"verify": False},
        "cert": {"cert": "synthetic-not-a-file"},
    }[setting]
    with pytest.raises(AnalysisBudgetStopped), analysis_budget_scope(core_config()) as budget:
        requests.get("https://search-api-web.eastmoney.com/search/jsonp", **options)
    assert not sent and budget.receipt()["admitted_attempts"] == 0


def test_core_ignores_implicit_netrc_and_environment_proxy(monkeypatch):
    sent = fake_wire(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", (('http://' + 'fixture-user' + ':' + 'fixture-pass' + '@proxy.invalid')))
    monkeypatch.delenv("NO_PROXY")
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda *a, **k: pytest.fail("implicit credential lookup"))
    session = requests.Session()
    with analysis_budget_scope(core_config()) as budget:
        session.get("https://search-api-web.eastmoney.com/search/jsonp")
    assert session.trust_env is True
    assert len(sent) == budget.receipt()["data_attempts"] == 1


def test_direct_session_send_still_checks_transport(monkeypatch):
    sent = fake_wire(monkeypatch)
    request = requests.Request("GET", "https://search-api-web.eastmoney.com/search/jsonp").prepare()
    with pytest.raises(AnalysisBudgetStopped), analysis_budget_scope(core_config()) as budget:
        requests.Session().send(request, proxies={"https": "http://proxy.invalid"})
    assert not sent and budget.receipt()["admitted_attempts"] == 0


def test_actual_news_parser_routes_without_sdk_cookie(monkeypatch):
    from copy import deepcopy

    from urllib3.connectionpool import HTTPConnectionPool

    from tradingagents.dataflows import config as runtime
    from tradingagents.dataflows.interface import route_to_vendor

    sent = []
    def send(pool, method, url, **kwargs):
        sent.append(dict(kwargs["headers"]))
        payload = {"result": {"cmsArticleWebOld": [{
            "date": "2026-10-14 12:00:00", "mediaName": "Synthetic", "code": "synthetic",
            "title": "601328 Synthetic news", "content": "Synthetic", "image": ""}]}}
        data = "jQuery35101792940631092459_1764599530165(" + json.dumps(payload) + ")"
        return HTTPResponse(body=io.BytesIO(data.encode()), status=200, preload_content=False)

    monkeypatch.setattr(HTTPConnectionPool, "urlopen", send)
    monkeypatch.setattr(runtime, "_config", deepcopy(runtime.get_config()))
    runtime.set_config({"tool_vendors": {"get_news": "akshare"}})
    with analysis_budget_scope(core_config()) as budget:
        result = route_to_vendor("get_news", "601328.SH", "2026-10-01", "2026-10-16")
    assert "Synthetic news" in result
    assert len(sent) == budget.receipt()["data_attempts"] == 1
    assert not {key.lower() for key in sent[0]} & {"cookie", "authorization"}


def test_preparation_transition_uses_one_counter_and_preserves_limit(monkeypatch):
    from tradingagents.analysis_budget import analysis_preparation_scope
    sent = fake_wire(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped, match="call_limit_exceeded"):
        with analysis_preparation_scope(core_config()):
            with analysis_budget_scope(core_config(analysis_max_calls=1)) as budget:
                requests.get("https://search-api-web.eastmoney.com/search/jsonp")
                assert budget.receipt()["data_attempts"] == 1
                requests.get("https://search-api-web.eastmoney.com/search/jsonp")
    assert len(sent) == 1


def test_swallowed_preparation_rejection_cannot_start_analysis(monkeypatch):
    from tradingagents.analysis_budget import analysis_preparation_scope
    sent = fake_wire(monkeypatch)
    with pytest.raises(AnalysisBudgetStopped, match="preparation_network_forbidden"):
        with analysis_preparation_scope(core_config()):
            try:  # noqa: SIM105 -- deliberately simulate a swallowed terminal signal.
                requests.get("https://search-api-web.eastmoney.com/search/jsonp")
            except AnalysisBudgetStopped:
                pass
            with analysis_budget_scope(core_config()):
                pytest.fail("rejection reset")
    assert not sent


def test_other_thread_cannot_take_preparation_ownership():
    from tradingagents.analysis_budget import analysis_preparation_scope
    def other():
        with pytest.raises(AnalysisBudgetStopped, match="concurrent_analysis_unsupported"):
            with analysis_budget_scope(core_config()):
                pytest.fail("foreign ownership")
    with analysis_preparation_scope(core_config()):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(other).result()


@pytest.mark.parametrize("debug", [False, True])
def test_admitted_async_dns_keeps_context_in_executor(monkeypatch, debug):
    import asyncio
    import socket

    from tradingagents.analysis_budget import admitted_transport

    resolved = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: resolved.append(a[:2]) or [])

    class Completed:
        def __init__(self, value):
            self.value = value

        def __await__(self):
            if False:
                yield
            return self.value

    def executor_call(loop, executor, function, *args):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return Completed(pool.submit(function, *args).result())

    # Actual stdlib getaddrinfo -> run_in_executor -> a fresh thread.
    # Only the resolver itself is synthetic; it never sends a DNS packet.
    monkeypatch.setattr(asyncio.BaseEventLoop, "run_in_executor", executor_call)
    loop = asyncio.BaseEventLoop()
    loop.set_debug(debug)
    try:
        with analysis_budget_scope(core_config()) as budget, admitted_transport("model"):
            coroutine = loop.getaddrinfo("api.deepseek.com", 443)
            with pytest.raises(StopIteration):
                coroutine.send(None)
            coroutine.close()
        assert resolved == [("api.deepseek.com", 443)]
        assert budget.receipt()["model_attempts"] == 1
    finally:
        loop.close()


def test_unadmitted_dns_still_stops_before_resolver(monkeypatch):
    import socket
    resolved = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: resolved.append(a))
    with pytest.raises(AnalysisBudgetStopped, match="uncovered_socket_transport"):
        with analysis_budget_scope(core_config()):
            socket.getaddrinfo("synthetic.invalid", 443)
    assert resolved == []


def test_unrelated_executor_does_not_inherit_transport_authority(monkeypatch):
    import asyncio
    import socket

    from tradingagents.analysis_budget import admitted_transport

    resolved = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: resolved.append(a))

    def executor_call(loop, executor, function, *args):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(function, *args).result()

    monkeypatch.setattr(asyncio.BaseEventLoop, "run_in_executor", executor_call)
    loop = asyncio.BaseEventLoop()
    try:
        with pytest.raises(AnalysisBudgetStopped, match="uncovered_socket_transport"):
            with analysis_budget_scope(core_config()), admitted_transport("model"):
                loop.run_in_executor(None, lambda: socket.getaddrinfo("synthetic.invalid", 443))
    finally:
        loop.close()
    assert resolved == []


def test_persistent_count_precedes_send_and_reopen_does_not_refund(tmp_path, monkeypatch):
    from contextlib import closing

    from tradingagents.analysis_budget import analysis_preparation_scope
    path = tmp_path / "checkpoint.sqlite3"
    committed_at_send = []

    def observe_committed_reservation():
        # A distinct connection must observe the committed reservation while
        # execution is at the fake physical wire, before its response exists.
        with closing(sqlite3.connect(path)) as audit:
            state = audit.execute("SELECT state FROM analysis_budget_v1 WHERE thread=?", ("fixed-thread",)).fetchone()[0]
            committed_at_send.append(json.loads(state)["counts"]["data"])

    sent = fake_wire(monkeypatch, on_send=observe_committed_reservation)
    settings = core_config(checkpoint_enabled=True, analysis_max_calls=2)
    for expected in (1, 2):
        with sqlite3.connect(path) as db, analysis_preparation_scope(settings):
            with pytest.raises(KeyboardInterrupt), analysis_budget_scope(settings) as budget:
                with budget.checkpoint_session(db, threading.RLock(), "fixed-thread", "fixed-policy", has_checkpoint=expected > 1):
                    requests.get("https://search-api-web.eastmoney.com/search/jsonp")
                    assert budget.receipt()["data_attempts"] == expected
                    raise KeyboardInterrupt("synthetic interruption")
    with sqlite3.connect(path) as db, pytest.raises(AnalysisBudgetStopped, match="call_limit_exceeded"), analysis_budget_scope(settings) as budget:
        with budget.checkpoint_session(db, threading.RLock(), "fixed-thread", "fixed-policy", has_checkpoint=True):
            requests.get("https://search-api-web.eastmoney.com/search/jsonp")
    assert len(sent) == 2
    assert committed_at_send == [1, 2]

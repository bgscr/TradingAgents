"""Offline CLI-to-report source-policy regression with real product boundaries.

Only external HTTP/model transport is substituted. All calendar, financial cache,
history, checkpoint and output paths are fresh synthetic test paths. This does not
exercise native report publication or qualify real supplier/history coverage.
"""

from __future__ import annotations

import http.client
import inspect
import io
import json
import sqlite3
import ssl
from contextlib import closing
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from urllib3.response import HTTPResponse

from tests.test_analysis_budget import httpx_families
from tests.test_analysis_budget_e2e import (
    AS_OF,
    MODELS,
    SYMBOL,
    TOOLS,
    FakeWire,
    TradingAgentsGraph,
    _assert_cache_unchanged,
    _close,
    prepare,
)


def _market_payload(request):
    """Synthetic rows in the actual installed SDK's endpoint/column envelope."""
    endpoint, params = request["api_name"], request["params"]
    assert endpoint in {"daily", "adj_factor", "suspend_d", "trade_cal"}
    end = datetime.strptime(params["end_date"], "%Y%m%d").date()
    start = datetime.strptime(params.get("start_date", "20200101"), "%Y%m%d").date()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if endpoint == "trade_cal":
        assert params["exchange"] == "SSE"
        rows = [{"exchange": "SSE", "cal_date": day.strftime("%Y%m%d"),
                 "is_open": int(day.weekday() < 5),
                 "pretrade_date": (day - timedelta(days=1)).strftime("%Y%m%d")}
                for day in days]
    else:
        assert params["ts_code"] == "601328.SH"
        rows = []
        for day in days:
            if day.weekday() >= 5 or endpoint == "suspend_d":
                continue
            identity = {"ts_code": "601328.SH", "trade_date": day.strftime("%Y%m%d")}
            if endpoint == "adj_factor":
                rows.append({**identity, "adj_factor": 1})
            else:
                price = 10 + (day - date(2020, 1, 1)).days / 1000
                rows.append({**identity, "open": price, "high": price + 1,
                             "low": price - 1, "close": price, "pre_close": price,
                             "change": 0, "pct_chg": 0, "vol": 1000, "amount": 10000})
    fields = request["fields"].split(",")
    offset, limit = params.get("offset", 0), params.get("limit", 5000)
    return {"code": 0, "data": {"fields": fields,
            "items": [[row[field] for field in fields] for row in rows[offset:offset + limit]]}}


def _install_core_wire(monkeypatch, wire, native_https, *, news_available):
    """Keep the native class required by budget admission; replace wire methods."""
    calls = []

    def request(connection, method, path, *, body, headers):
        assert connection.host == "api.waditu.com", "unexpected financial refresh/fallback"
        assert method == "POST"
        payload = json.loads(body)
        assert path == "/dataapi/" + payload["api_name"]
        calls.append((payload["api_name"], dict(payload["params"]), connection.timeout))
        connection.synthetic_payload = _market_payload(payload)

    def response(connection):
        content = json.dumps(connection.synthetic_payload).encode()
        return SimpleNamespace(status=200, read=lambda maximum: content[:maximum])

    def urllib3(pool, method, url, **kwargs):
        if pool.host != "search-api-web.eastmoney.com":
            return FakeWire.urllib3(wire, pool, method, url, **kwargs)
        wire.data_calls.append((pool.host, method, url))
        rows = [{"date": "2026-10-14 12:00:00", "mediaName": "Synthetic",
                 "code": "202610140001", "title": "601328 synthetic stock article",
                 "content": "601328 synthetic content."}] if news_available else []
        content = ("other_callback(" + json.dumps({"result": {"cmsArticleWebOld": rows}}) + ");").encode()
        return HTTPResponse(body=io.BytesIO(content), status=200, preload_content=False,
                            headers={"content-type": "application/javascript"})

    # prepare() used a fake constructor only for financial fixture seeding.
    # Market admission verifies native class identity, so restore it for the run.
    monkeypatch.setattr(http.client, "HTTPSConnection", native_https)
    monkeypatch.setattr(native_https, "request", request)
    monkeypatch.setattr(native_https, "getresponse", response)
    monkeypatch.setattr(wire, "urllib3", urllib3)
    return calls


def _retain_model_transport_settings(monkeypatch, signatures):
    """Give the offline transport the caller's real TLS/proxy settings.

    The reused lifecycle-only pool predates these guard checks. No native trust
    store or connection is needed for fake replies; insecure options still fail
    the unmodified product guard.
    """
    def constructor(original, signature):
        def initialize(transport, *args, **kwargs):
            bound = signature.bind(transport, *args, **kwargs)
            bound.apply_defaults()
            original(transport, *args, **kwargs)
            verify = bound.arguments["verify"]
            if isinstance(verify, ssl.SSLContext):
                context = verify
            else:
                assert type(verify) is bool, "unplanned fake TLS configuration"
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                if not verify:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
            transport._pool._ssl_context = context
            transport._pool._proxy_url = bound.arguments.get("proxy")
            transport._pool._uds = bound.arguments.get("uds")
        return initialize

    for transport, signature in signatures.items():
        monkeypatch.setattr(transport, "__init__", constructor(transport.__init__, signature))


def _render(state):
    from tradingagents import reporting

    terminal = reporting.TerminalContract.model_validate(state["terminal_contract"])
    evidence = reporting.EvidenceState.model_validate(state["evidence_state"])
    financial = reporting.TypeAdapter(reporting.FinancialDispatchAuditProjection).validate_python(
        state["financial_dispatch_audit_projection"])
    if state["terminal_outcome_kind"] == "trading_decision":
        decision = reporting.TradingDecisionContract.model_validate(state["trading_decision"])
        rendered = reporting.render_decision_report(decision, terminal, evidence, financial)
    else:
        assert state["terminal_outcome_kind"] == "analysis_outcome"
        outcome = reporting.AnalysisOutcome.model_validate(state["analysis_outcome_contract"])
        rendered = reporting.render_analysis_outcome_report(outcome, terminal, evidence, financial)
    rendered += "\n" + reporting.budget_data_gap_markdown(state)
    for _, title, _, content in reporting.advisory_sections(state):
        rendered += "\n" + reporting.advisory_markdown(title, content, "English")
    return rendered


@pytest.mark.parametrize("news_available", [True, False], ids=["alternate-callback", "no-usable-news"])
def test_mainland_core_cli_graph_tools_checkpoint_and_report(tmp_path, monkeypatch, news_available):
    import cli.main as cli

    native_https = http.client.HTTPSConnection
    signatures = {transport: inspect.signature(transport.__init__)
                  for module in httpx_families()
                  for transport in (module.HTTPTransport, module.AsyncHTTPTransport)}
    settings, wire, cache, before = prepare(tmp_path, monkeypatch)
    _retain_model_transport_settings(monkeypatch, signatures)
    # PITConfig eagerly evaluates Path.home() even when a cache env is present.
    # prepare() deliberately discards the real environment; supply a fake home.
    synthetic_home = tmp_path / "synthetic-home"
    synthetic_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(synthetic_home))
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(tmp_path / "cache"))
    core_calls = _install_core_wire(monkeypatch, wire, native_https, news_available=news_available)
    checkpoint_base = tmp_path / "checkpoint-state"
    settings.update(analysis_skip_optional_fundamentals=True, analysis_max_calls=60,
                    llm_max_output_tokens=8192, llm_max_retries=0,
                    checkpoint_data_dir=str(checkpoint_base))
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", settings)
    selected = cli._build_run_config({
        "ticker": SYMBOL, "analysis_date": AS_OF.isoformat(), "research_depth": 1,
        "shallow_thinker": MODELS[0], "deep_thinker": MODELS[1],
        "backend_url": "https://api.deepseek.com", "llm_provider": "DeepSeek",
        "analysts": ["market", "social", "news", "fundamentals"],
        "china_a_enhancement_preset": "basic",
    }, True, mainland_core_sources=True)
    assert selected["market_data_vendors"]["cn_a"]["core_stock_apis"] == "tushare"
    assert selected["analysis_allowed_data_vendors"] == ["tushare", "akshare"]
    assert selected["analysis_skip_optional_fundamentals"] is True
    assert selected["financial_cache_only"] is True
    assert selected["checkpoint_enabled"] is True
    assert selected["analysis_max_calls"] == 60
    assert selected["llm_max_output_tokens"] == 8192
    assert selected["llm_max_retries"] == 0

    graph = TradingAgentsGraph(config=selected, instrument_symbol=SYMBOL)
    try:
        state = {}
        try:
            state, _signal = graph.propagate(SYMBOL, AS_OF.isoformat())
        finally:
            (tmp_path / "pre-render-observation.json").write_text(json.dumps({
                "state": state, "budget": graph.last_analysis_budget,
                "core_calls": core_calls, "data_calls": wire.data_calls,
                "model_phases": dict(wire.phases),
            }, indent=2, default=str), encoding="utf-8")
        rendered = _render(state)
        tool_text = "\n".join(str(message.get("content", ""))
                              for call in wire.model_calls for message in call["messages"]
                              if message.get("role") == "tool")
        receipt = graph.last_analysis_budget
        reasons = {item["reason"] for item in receipt["data_gaps"]}
        for reason in ("optional_business_omitted", "optional_financial_summary_omitted"):
            assert reason in reasons
            assert reason in tool_text
            assert reason in rendered
        if news_available:
            assert "601328 synthetic stock article" in tool_text
            assert "Business status: not_supplied" in tool_text
            assert "news_window_coverage_unknown" in tool_text
            assert "news_window_coverage_unknown" in reasons
            assert "news_window_coverage_unknown" in rendered
        else:
            assert "601328 synthetic stock article" not in tool_text
            assert "news_unavailable" in reasons
            assert "news_unavailable" in rendered
            assert "DATA_UNAVAILABLE" in tool_text

        assert wire.tools_requested == {
            "get_stock_data", "get_indicators", "get_verified_market_snapshot",
            "get_news", "get_global_news", "get_macro_indicators", "get_prediction_markets",
            "get_fundamentals", *TOOLS,
        }
        assert wire.phases["fundamentals_finalizer"] == 1
        assert len(wire.financial_calls) == 3  # Only fixture seeding, never refresh.
        assert len(receipt["financial_cache_hits"]) == 3
        _assert_cache_unchanged(cache, before)
        assert {endpoint for endpoint, _, _ in core_calls} == {"daily", "adj_factor", "suspend_d", "trade_cal"}
        hosts = {host for host, _, _ in wire.data_calls}
        assert "search-api-web.eastmoney.com" in hosts
        assert not hosts.intersection({"basic.10jqka.com.cn", "quotes.sina.cn", "www.alphavantage.co",
                                       "api.stlouisfed.org", "gamma-api.polymarket.com"})
        assert not any("yahoo" in host for host in hosts)
        assert wire.legacy_news_calls == 0
        assert receipt["persistent"] is True and receipt["stop_reason"] == "completed"
        assert receipt["max_calls"] == 60 and receipt["max_output_tokens"] == 8192
        assert receipt["admitted_attempts"] == len(wire.model_calls) + len(wire.data_calls) + len(core_calls)
        assert receipt["data_attempts"] == len(wire.data_calls) + len(core_calls)
        assert receipt["model_attempts"] == len(wire.model_calls)
        assert receipt["admitted_attempts"] <= 60
        assert all(call.get("max_tokens", call.get("max_completion_tokens")) == 8192 for call in wire.model_calls)
        assert graph.quick_thinking_llm.max_retries == graph.deep_thinking_llm.max_retries == 0

        databases = list((checkpoint_base / "checkpoints").glob("*.db"))
        assert len(databases) == 1
        with closing(sqlite3.connect(databases[0].as_uri() + "?mode=ro", uri=True)) as db:
            persisted = db.execute("SELECT policy, state FROM analysis_budget_v1").fetchall()
        assert len(persisted) == 1
        policy, journal = map(json.loads, persisted[0])
        assert policy["maximum"] == 60 and policy["max_output_tokens"] == 8192
        assert policy["skip_optional_fundamentals"] is True
        assert policy["vendors"] == ["akshare", "tushare"]
        assert journal["counts"] == {"model": receipt["model_attempts"], "data": receipt["data_attempts"]}
        (tmp_path / "rendered-optional-source-report.md").write_text(rendered, encoding="utf-8")
        (tmp_path / "optional-source-observation.json").write_text(json.dumps({
            "budget": receipt, "core_endpoints": [call[0] for call in core_calls],
            "data_hosts": sorted(hosts), "news_available_fixture": news_available,
            "terminal_outcome_kind": state["terminal_outcome_kind"], "policy": policy,
        }, indent=2), encoding="utf-8")
    finally:
        _close(graph)

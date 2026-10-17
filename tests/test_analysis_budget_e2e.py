"""Full production graph tests with synthetic fixtures and transport-only replies.

No graph, ToolNode, evidence gate, model factory or schema parser is replaced.
The report content renderer is exercised, not the native report publication
boundary. The calendar and statements are synthetic local fixture stores,
never historical qualification evidence or a user's cache.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from urllib3.response import HTTPResponse

from tests.test_analysis_budget import httpx_families, install_network_denial
from tests.test_current_tushare_statements import (
    TOOLS,
    dispatcher,
    request,
    runtime_config,
    statement_rows,
)
from tradingagents.analysis_budget import AnalysisBudgetStopped, active_budget
from tradingagents.graph.trading_graph import TradingAgentsGraph

AS_OF = date(2026, 10, 15)
FIRST = date(2021, 10, 15)
SYMBOL = "600895.SS"
MODELS = ("deepseek-flash", "deepseek-v4-pro")


def _days():
    return [FIRST + timedelta(days=n) for n in range((AS_OF - FIRST).days + 1)]


def _seed_calendar(settings):
    """Publish synthetic membership through the actual calendar/store contract."""
    from tradingagents.market_history import calendar as cal
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.coordinator import upstream_service_identity_for_provider
    from tradingagents.market_history.models import ProvenanceClass, ProviderDatasetSpec
    from tradingagents.market_history.store import MarketHistoryStore

    zone = ZoneInfo("Asia/Shanghai")
    sessions = tuple(cal.MarketSession(
        day, cal.MarketSessionStatus.OPEN if day.weekday() < 5 else cal.MarketSessionStatus.CLOSED,
        datetime.combine(day, time(9, 30), zone) if day.weekday() < 5 else None,
        datetime.combine(day, time(15), zone) if day.weekday() < 5 else None,
    ) for day in _days())
    upstream_id, upstream_name = upstream_service_identity_for_provider("baostock")
    provider = ProviderDatasetSpec(
        upstream_service_id=upstream_id, upstream_service_name=upstream_name,
        provider_dataset_id=cal._provider_dataset_identity(upstream_id),
        provider_name="baostock", dataset_name=cal.MAINLAND_SESSION_CALENDAR_DATASET,
        adjustment_methodology="not-applicable", strict_history_qualified=False,
    )
    payload = {
        "contract_version": cal.MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT,
        "dataset_name": provider.dataset_name, "endpoint": "query_trade_dates",
        "membership_source": "provider_returned",
        "normalizer_version": cal.MAINLAND_SESSION_CALENDAR_NORMALIZER,
        "provenance_class": "observed_point_in_time", "provider": "baostock",
        "provider_dataset_id": provider.provider_dataset_id, "reference_market": "mainland-cn",
        "session_time_contract": cal.MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
        "session_time_source": "contract_derived", "sessions": cal._session_fields(sessions),
        "timezone_name": "Asia/Shanghai", "upstream_service_id": upstream_id,
        "upstream_service_name": upstream_name,
    }
    publication = cal.MarketSessionCalendarPublication(
        provider, "mainland-cn", "Asia/Shanghai", datetime.now(timezone.utc),
        ProvenanceClass.OBSERVED_POINT_IN_TIME, cal._canonical_payload_bytes(payload), sessions,
        endpoint="query_trade_dates", normalizer_version=cal.MAINLAND_SESSION_CALENDAR_NORMALIZER,
        payload_contract=cal.MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT,
        session_time_contract=cal.MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
        membership_source="provider_returned", session_time_source="contract_derived",
    )
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(settings)) as store:
        published = store.publish_session_calendar(publication)
        assert store.read_session_calendar_revision(published.calendar_revision_id).session_count == len(sessions)


class FakeWire:
    """Fixed fake provider wire responses, after production budget admission."""

    def __init__(self):
        self.model_calls = []
        self.data_calls = []
        self.financial_calls = []
        self.phases = Counter()
        self.seeding = True
        self.expire_on_model = False
        self.legacy_news_calls = 0

    def legacy_news(self, _session, method, url, **kwargs):
        """Default SDK's native request boundary, fully fake and never sent."""
        assert active_budget() is None, "budgeted news must use admitted Requests"
        assert method == "GET" and urlsplit(url).hostname == "search-api-web.eastmoney.com"
        self.legacy_news_calls += 1
        reply = self.urllib3(SimpleNamespace(host=urlsplit(url).hostname), method, url)
        return SimpleNamespace(text=reply.data.decode(), status_code=200)

    def httpx(self, _transport, request, module):
        assert request.url.host == "api.deepseek.com"
        body = json.loads(request.content)
        assert body["model"] in MODELS
        self.model_calls.append(body)
        if self.expire_on_model:
            active_budget()._started -= 1000
        text = "\n".join(str(message.get("content") or "") for message in body["messages"])
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        message = {"role": "assistant", "content": "Synthetic debate evidence.",
                   "reasoning_content": "Synthetic reasoning."}
        phase = "debate"

        def tool_response(name, args):
            message.update(content="", tool_calls=[{
                "id": f"fake-{len(self.model_calls)}-{name}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }])

        if "ResearchPlan" in names:
            phase = "research_plan"
            tool_response("ResearchPlan", {"recommendation": "Buy", "rationale": "Synthetic debate reviewed.",
                                           "strategic_actions": "Synthetic plan only."})
        elif "TraderProposal" in names:
            phase = "trader"
            tool_response("TraderProposal", {"action": "Buy", "reasoning": "Synthetic proposal."})
        elif body.get("response_format", {}).get("type") == "json_object":
            if "matching SentimentReport" in text:
                phase = "sentiment"
                result = {"overall_band": "Neutral", "overall_score": 5, "confidence": "low",
                          "narrative": "Synthetic sentiment report.", "material_claims": []}
            else:
                match = re.search(r"Finalize the (\w+) analyst", text)
                assert match, text[:300]
                analyst = match.group(1)
                phase = analyst + "_finalizer"
                if analyst == "fundamentals" and not self.phases[phase]:
                    result = {"invalid": "one deliberate schema failure"}
                else:
                    result = {"report_markdown": f"# Synthetic {analyst} report\nVerified fixture data.",
                              "material_claims": []}
            message["content"] = json.dumps(result)
        elif "get_balance_sheet" in names:
            phase = "fundamentals_tools"
            if not any(m.get("role") == "tool" for m in body["messages"]):
                message.update(content="", tool_calls=[{
                    "id": f"fake-{len(self.model_calls)}-{name}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps({
                        "ticker": SYMBOL, "freq": "quarterly", "curr_date": AS_OF.isoformat(),
                    })},
                } for name in TOOLS])
            else:
                message["content"] = "Synthetic fundamentals draft for required finalization."
        elif "get_verified_market_snapshot" in names:
            phase = "market_tools"
            if not any(m.get("role") == "tool" for m in body["messages"]):
                tool_response("get_verified_market_snapshot", {"symbol": SYMBOL, "curr_date": AS_OF.isoformat()})
            else:
                message["content"] = "Synthetic market draft for required finalization."
        elif "get_news" in names:
            phase = "news_tools"
            if not any(m.get("role") == "tool" for m in body["messages"]):
                tool_response("get_news", {"ticker": SYMBOL, "start_date": "2026-10-08", "end_date": AS_OF.isoformat()})
            else:
                message["content"] = "Synthetic news draft for required finalization."
        self.phases[phase] += 1
        return module.Response(200, request=request, json={
            "id": "synthetic-completion", "object": "chat.completion", "created": 1,
            "model": body["model"], "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        })

    def urllib3(self, pool, method, url, **kwargs):
        self.data_calls.append((pool.host, method, url))
        parsed = urlsplit(url)
        if pool.host == "push2his.eastmoney.com" and parsed.path.endswith("/kline/get"):
            rows = []
            for index, day in enumerate(day for day in _days() if day.weekday() < 5):
                price = 10 + index / 100
                rows.append(f"{day},{price},{price},{price+1},{price-1},1000,10000,1,1,1,1")
            payload = json.dumps({"data": {"klines": rows}}).encode()
        elif pool.host == "search-api-web.eastmoney.com":
            payload = ("jQuery35101792940631092459_1764599530165(" + json.dumps({"result": {
                "cmsArticleWebOld": [{"date": "2026-10-14 12:00:00", "mediaName": "Synthetic",
                    "code": "synthetic", "title": "Synthetic fixture news", "content": "Synthetic content", "image": ""}],
            }}) + ")").encode()
        elif pool.host in {"emappdata.eastmoney.com", "datacenter-web.eastmoney.com", "datacenter.eastmoney.com"}:
            # Fixed empty optional sentiment data exercises the actual SDK parser
            # and degradation path; it never grants fallback to another vendor.
            payload = b'{"data":[],"result":{"pages":0,"data":[]}}'
        else:
            pytest.fail(f"unplanned fake provider request: {pool.host}{parsed.path}")
        return HTTPResponse(body=io.BytesIO(payload), status=200, preload_content=False,
                            headers={"content-type": "application/json"})

    def connection_type(self):
        wire = self

        class Connection:
            status = 200

            def __init__(self, host, **kwargs):
                assert host == "api.tushare.pro"
                self.timeout = kwargs["timeout"]

            def request(self, method, path, *, body, headers):
                assert wire.seeding, "cache-only graph attempted a financial network refresh"
                self.payload = json.loads(body)
                wire.financial_calls.append(self.payload["api_name"])

            def getresponse(self):
                return self

            def read(self, limit):
                frame = statement_rows(self.payload["api_name"])
                return json.dumps({"code": 0, "data": {
                    "fields": list(frame.columns), "items": frame.values.tolist(),
                }}).encode()

            def close(self):
                pass

        return Connection


def prepare(tmp_path, monkeypatch, *, enabled=True, cache_only=True, cache_damage=None):
    install_network_denial(monkeypatch)
    from curl_cffi import requests as curl_requests
    from urllib3.connectionpool import HTTPConnectionPool

    from tradingagents import tushare_financial_https

    _clear_client_cache()
    settings = runtime_config(tmp_path)
    settings.update(
        llm_provider="deepseek", quick_think_llm=MODELS[0], deep_think_llm=MODELS[1],
        backend_url="https://api.deepseek.com", llm_max_retries=0,
        analysis_budget_enabled=enabled, analysis_max_calls=30, analysis_timeout_seconds=600,
        financial_cache_only=False, checkpoint_enabled=False,
        tushare_financial_https_endpoint="https://api.tushare.pro",
        results_dir=str(tmp_path / "results"), memory_log_path=str(tmp_path / "memory.jsonl"),
        china_a_enhancement_preset="basic",
    )
    settings["market_data_vendors"]["cn_a"] = dict.fromkeys(
        ("core_stock_apis", "technical_indicators", "fundamental_data", "news_data"), "akshare"
    )
    # Supply only fixed synthetic keys; never copy the caller's environment.
    monkeypatch.setattr(os, "environ", {"DEEPSEEK_API_KEY": "synthetic", "TUSHARE_TOKEN": "synthetic",
                                        "NO_PROXY": "*", "PYTHON_DOTENV_DISABLED": "1"})
    wire = FakeWire()
    for module in httpx_families():
        monkeypatch.setattr(module.HTTPTransport, "handle_request",
                            lambda transport, request, family=module: wire.httpx(transport, request, family))
    monkeypatch.setattr(HTTPConnectionPool, "urlopen",
                        lambda pool, method, url, **kwargs: wire.urllib3(pool, method, url, **kwargs))
    monkeypatch.setattr(curl_requests.Session, "request",
                        lambda session, method, url, **kwargs: wire.legacy_news(session, method, url, **kwargs))
    monkeypatch.setattr(tushare_financial_https.http.client, "HTTPSConnection", wire.connection_type())
    _seed_calendar(settings)
    seeder = dispatcher(settings)
    for tool in TOOLS:
        assert seeder.dispatch(request(tool)).artifact is not None
    assert len(wire.financial_calls) == 3
    wire.seeding = False
    settings["financial_cache_only"] = cache_only
    path = tmp_path / "cache" / "current-financial-statements.sqlite3"
    if cache_damage == "missing":
        path.unlink()
    elif cache_damage == "stale":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE observations SET checked_at=?", ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),))
    elif cache_damage == "wal":
        Path(str(path) + "-wal").write_bytes(b"synthetic pending WAL")
    elif cache_damage == "corrupt":
        path.write_bytes(b"synthetic not a sqlite database")
    elif cache_damage == "hash":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE observations SET payload_sha256=?", ("0" * 64,))
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in path.parent.glob("current-financial-statements.sqlite3*")}
    return settings, wire, path, before


def _assert_cache_unchanged(path, before):
    assert {p.name: sha256(p.read_bytes()).hexdigest() for p in path.parent.glob("current-financial-statements.sqlite3*")} == before


def _close(graph):
    for llm in (graph.quick_thinking_llm, graph.deep_thinking_llm):
        llm.root_client.close()
    _clear_client_cache()


def _clear_client_cache():
    # LangChain shares these SDK-owned clients across graph instances. Each
    # fixture must build a fresh real client, never reuse one closed by a test.
    from langchain_openai.chat_models import _client_utils

    _client_utils._cached_sync_httpx_client.cache_clear()
    _client_utils._cached_async_httpx_client.cache_clear()


@pytest.mark.parametrize("enabled", [True, False], ids=["budget-cache-only", "legacy-defaults"])
def test_complete_graph_and_rendered_report(tmp_path, monkeypatch, enabled):
    from tradingagents import reporting

    settings, wire, path, before = prepare(tmp_path, monkeypatch, enabled=enabled, cache_only=enabled)
    if not enabled:
        for key in ("analysis_budget_enabled", "analysis_max_calls", "analysis_timeout_seconds", "financial_cache_only"):
            settings.pop(key)
    graph = TradingAgentsGraph(config=settings, instrument_symbol=SYMBOL)
    try:
        state, signal = graph.propagate(SYMBOL, AS_OF.isoformat())
        (tmp_path / "e2e-observation.json").write_text(json.dumps({
            "phases": dict(wire.phases), "models": [item["model"] for item in wire.model_calls],
            "data_calls": wire.data_calls, "budget": graph.last_analysis_budget,
            "terminal": state["terminal_contract"],
            "investment_debate": state.get("investment_debate_state"),
            "risk_debate": state.get("risk_debate_state"),
        }, indent=2), encoding="utf-8")
        assert state["terminal_outcome_kind"] == "trading_decision", state.get("analysis_outcome")
        assert state["evidence_preflight"]["passed"] is True
        assert state["admission_gate"]["admitted"] is True
        assert signal in {"Buy", "Hold", "Sell"}
        assert {body["model"] for body in wire.model_calls} == set(MODELS)
        assert wire.phases["fundamentals_finalizer"] == 2
        assert wire.phases["research_plan"] == wire.phases["trader"] == 1
        assert any(host == "search-api-web.eastmoney.com" for host, _, _ in wire.data_calls)
        assert wire.legacy_news_calls == (0 if enabled else 2)
        assert all(state.get(name) for name in ("market_report", "sentiment_report", "news_report", "fundamentals_report"))
        investment = state["investment_debate_state"]
        assert all(investment.get(key) for key in ("bull_history", "bear_history", "judge_decision"))
        risk = state["risk_debate_state"]
        assert all(risk.get(key) for key in ("aggressive_history", "conservative_history", "neutral_history", "judge_decision"))
        # ReportStore's Windows native handle boundary is intentionally not
        # substituted. This validates the actual renderer, not publication.
        decision = reporting.TradingDecisionContract.model_validate(state["trading_decision"])
        terminal = reporting.TerminalContract.model_validate(state["terminal_contract"])
        evidence = reporting.EvidenceState.model_validate(state["evidence_state"])
        financial = reporting.TypeAdapter(reporting.FinancialDispatchAuditProjection).validate_python(
            state["financial_dispatch_audit_projection"]
        )
        rendered = reporting.render_decision_report(decision, terminal, evidence, financial)
        for _, title, _, content in reporting.advisory_sections(state):
            rendered += "\n" + reporting.advisory_markdown(title, content, "English")
        report = tmp_path / "rendered-report-only.md"
        report.write_text(rendered, encoding="utf-8")
        assert len(rendered) > 1000 and "# Validated Trading Decision" in rendered
        assert decision.decision_id in rendered
        assert len(wire.financial_calls) == 3
        if enabled:
            receipt = graph.last_analysis_budget
            assert receipt["admitted_attempts"] == len(wire.model_calls) + len(wire.data_calls)
            assert receipt["model_attempts"] == len(wire.model_calls)
            assert receipt["data_attempts"] == len(wire.data_calls)
            assert len(receipt["financial_cache_hits"]) == 3
            assert receipt["stop_reason"] is None
        else:
            assert graph.last_analysis_budget is None
        _assert_cache_unchanged(path, before)
    finally:
        _close(graph)


@pytest.mark.parametrize("damage", ["missing", "stale", "wal", "corrupt", "hash"])
def test_complete_graph_cache_failure_stops_before_debate(tmp_path, monkeypatch, damage):
    settings, wire, path, before = prepare(tmp_path, monkeypatch, cache_damage=damage)
    graph = TradingAgentsGraph(config=settings, instrument_symbol=SYMBOL)
    try:
        with pytest.raises(AnalysisBudgetStopped, match="financial_cache"):
            graph.propagate(SYMBOL, AS_OF.isoformat())
        assert wire.phases["fundamentals_tools"] == 1
        assert wire.phases["research_plan"] == wire.phases["debate"] == 0
        assert len(wire.financial_calls) == 3
        assert not list((tmp_path / "results").rglob("*.md"))
        assert graph.last_analysis_budget["stop_reason"].startswith("financial_cache")
        _assert_cache_unchanged(path, before)
    finally:
        _close(graph)


@pytest.mark.parametrize("stop", ["count", "deadline"])
def test_complete_graph_budget_stops_following_physical_sends(tmp_path, monkeypatch, stop):
    settings, wire, path, before = prepare(tmp_path, monkeypatch)
    settings["analysis_max_calls"] = 2 if stop == "count" else 30
    wire.expire_on_model = stop == "deadline"
    graph = TradingAgentsGraph(config=settings, instrument_symbol=SYMBOL)
    try:
        expected_reason = "call_limit_exceeded" if stop == "count" else "deadline_exceeded"
        with pytest.raises(AnalysisBudgetStopped, match=expected_reason):
            graph.propagate(SYMBOL, AS_OF.isoformat())
        receipt = graph.last_analysis_budget
        assert receipt["admitted_attempts"] == len(wire.model_calls) + len(wire.data_calls) == 2
        assert receipt["stop_reason"] == expected_reason
        assert wire.phases["research_plan"] == 0
        _assert_cache_unchanged(path, before)
    finally:
        _close(graph)

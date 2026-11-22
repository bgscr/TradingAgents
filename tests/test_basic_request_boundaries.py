"""Synthetic send-boundary probes; no live service, credentials, or saved bodies."""

import copy
import io
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests
from langchain_core.messages import ToolMessage
from urllib3.response import HTTPResponse

from tests.test_multiyear_trend import ASOF, SYMBOL, synthetic_evidence
from tradingagents.agents.analysts import market_analyst
from tradingagents.dataflows import akshare_data, config
from tradingagents.dataflows.acquisition import AcquisitionFailure
from tradingagents.evidence import EvidenceState
from tradingagents.graph.propagation import Propagator
from tradingagents.llm_clients.openai_client import OpenAIClient
from tradingagents.multiyear_trend import multiyear_trend_markdown
from tradingagents.run_diagnostics import diagnostic_scope


def assert_market_wire(monkeypatch, module, language, *, invalid_report=False):
    """Exercise full node, SDK JSON serialization, tool follow-up and finalizer."""
    # OpenAIClient checks provider env before applying explicit kwargs. This is
    # fabricated test input, independent of tests/conftest.py and real credentials.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only")
    monkeypatch.setitem(config._config, "output_language", language)
    evidence = synthetic_evidence()
    state = Propagator().create_initial_state(
        SYMBOL, ASOF, evidence_state=evidence, instrument_context="Synthetic identity"
    )
    before = copy.deepcopy(state["evidence_state"])
    trend = multiyear_trend_markdown(before, symbol=SYMBOL, as_of=ASOF, language=language)
    assert "100.00%" in trend and "50.00%" in trend
    assert evidence.market_snapshot.snapshot_id in trend
    bodies = []
    report = "Synthetic model deliberately omits multi-year observations."
    tool_args = {"symbol": SYMBOL, "start_date": "2025-04-01", "end_date": ASOF}
    replies = [
        {"content": "", "tool_calls": [{"id": "synthetic-tool", "type": "function",
         "function": {"name": "get_stock_data", "arguments": json.dumps(tool_args)}}]},
        {"content": "Synthetic prose draft"},
        {"content": json.dumps({"report_markdown": report,
                                "material_claims": [], "evidence_notes": []})},
    ]
    if invalid_report:
        # The retained run records a tool_calls response before a finalizer.
        # Its exact arguments were not retained: this is a synthetic schema error.
        replies[1] = {"content": "Synthetic invalid report", "tool_calls": [{
            "id": "synthetic-invalid", "type": "function",
            "function": {"name": "AnalystEvidenceReport", "arguments": "{}"},
        }]}

    def respond(request):
        assert request.url.host == "api.deepseek.com"
        assert request.method == "POST" and request.url.path == "/chat/completions"
        # Read only synthetic JSON in memory. Never persist headers or request bytes.
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert len(bodies) < len(replies), "unexpected retry/repair"
        bodies.append(body)
        reply = replies[len(bodies) - 1]
        return httpx.Response(200, json={
            "id": "synthetic-boundary", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0,
                "finish_reason": "tool_calls" if "tool_calls" in reply else "stop",
                "message": {"role": "assistant", "reasoning_content": "synthetic", **reply}}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        llm = OpenAIClient("deepseek-flash", provider="deepseek", api_key="synthetic-only",
                           http_client=client, max_retries=0).get_llm()
        node = module.create_market_analyst(llm)
        first = node(state)
        assert len(bodies) == 1 and first["market_report"] == ""
        # Do not execute the requested data tool. Supply explicitly synthetic output.
        state["messages"].extend(first["messages"])
        state["messages"].append(ToolMessage(
            name="get_stock_data", tool_call_id="synthetic-tool",
            content="synthetic head\n" + "x" * 8000 + "\nsynthetic tail",
        ))
        final = node(state)

    assert len(bodies) == 3
    for body in bodies[:2]:
        systems = [m["content"] for m in body["messages"] if m["role"] == "system"]
        assert len(systems) == 1 and trend in systems[0]
        assert "They are auxiliary context" in systems[0]
        assert "tools" in body and "tool_choice" not in body
    assert len(bodies[1]["messages"][-1]["content"]) > 8000
    finalizer = bodies[2]
    assert finalizer["response_format"] == {"type": "json_object"}
    assert "tools" not in finalizer and "tool_choice" not in finalizer
    text = "\n".join(m["content"] for m in finalizer["messages"])
    assert trend in text and "... [middle omitted] ..." in text
    assert "x" * 8000 not in text  # Source preview cap, not auxiliary-context truncation.
    assert final["market_report"] == report  # Transport success does not enforce use.
    after = EvidenceState.model_validate(final.get("evidence_state", before))
    assert not after.source_facts and not after.material_claims
    assert state["evidence_state"] == before


@pytest.mark.parametrize("language", ["Chinese", "English"])
def test_current_market_summary_survives_real_sdk_serialization(monkeypatch, language):
    assert_market_wire(monkeypatch, market_analyst, language)


def test_invalid_report_repair_keeps_summary_at_send_boundary(monkeypatch):
    assert_market_wire(monkeypatch, market_analyst, "Chinese", invalid_report=True)


@pytest.mark.parametrize("variant", ["missing_collection", "different_collection", "explicit_error"])
def test_news_prepared_query_and_distinct_envelopes(monkeypatch, variant):
    """Different synthetic bodies reproduce coarse diagnostics, not historical cause."""
    calls, closed, events = [], [], []
    payload = {"code": 0, "result": {}}
    if variant == "different_collection":
        payload.update(msg="synthetic alternate envelope", result={"syntheticOther": []})
    if variant == "explicit_error":
        payload["error"] = "synthetic rejection"

    original_init = requests.Session.__init__

    def isolated_session(session):
        original_init(session)
        session.trust_env = False  # Also prevents implicit .netrc credential reads.

    def send(adapter, request, **kwargs):
        assert not calls, "production path attempted again"
        calls.append(True)
        assert request.method == "GET"
        url = urlsplit(request.url)
        assert (url.scheme, url.netloc, url.path) == (
            "https", "search-api-web.eastmoney.com", "/search/jsonp")
        query = parse_qs(url.query)
        assert set(query) == {"cb", "param", "_"}
        assert query["cb"] == ["jQuery35101792940631092459_1764599530165"]
        assert query["_"] == ["1764599530176"]
        inner = json.loads(query["param"][0])  # One URL decode yields JSON, not escaped JSON.
        assert inner == {"uid": "", "keyword": "600919", "type": ["cmsArticleWebOld"],
                         "client": "web", "clientType": "web", "clientVersion": "curr",
                         "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                            "pageIndex": 1, "pageSize": 10, "preTag": "<em>", "postTag": "</em>"}}}
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        assert kwargs["timeout"] == 15
        raw = HTTPResponse(body=io.BytesIO(
            ("syntheticDifferentCallback(" + json.dumps(payload) + ")").encode()
        ), status=200, preload_content=False, decode_content=False)
        response = adapter.build_response(request, raw)
        original_close = response.close

        def close():
            closed.append(True)
            return original_close()

        response.close = close
        return response

    monkeypatch.setattr(requests.Session, "__init__", isolated_session)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    # The isolated runner clears real environment. No proxy, credential or service lookup.
    with diagnostic_scope(events.append), pytest.raises(AcquisitionFailure) as caught:
        akshare_data.get_news("600919.SS", "2026-11-06", ASOF)
    expected = "news_business_failure" if variant == "explicit_error" else "news_business_status_unverified"
    assert caught.value.error_code == expected
    assert len(calls) == len(closed) == 1
    shape = next(e["payload"] for e in events if e["kind"] == "news_response_shape")
    assert shape["callback_matches"] is False and shape["target_collection"] == "missing"
    assert shape["article_count"] is None
    assert not any(e["kind"] == "news_selection" for e in events)
    diagnostic = next(e["payload"] for e in events if e["kind"] == "news_exception")
    assert diagnostic["http_status"] == 200 and diagnostic["stage"] == "response_parse"
    assert diagnostic["error_code"] == expected

"""Personal CLI with the real DeepSeek SDK and synthetic HTTP/data boundaries."""

import json
import re
from collections import Counter

import pytest

from scripts import personal_full
from tests.test_personal_bootstrap import install_graph_boundary
from tradingagents.analysis_budget import httpx_transport_modules

REASONING = "fixture-private-reasoning-never-persist"
PARTIAL = "fixture-partial-report-never-publish"
NO_NEWS_REPORT = "发行人新闻来源不可用，无法核实本窗口内的发行人事件。"
ANALYSTS = {"market", "sentiment", "news", "fundamentals"}
DAY = "2024-02-29"


def install_personal_deepseek_wire(monkeypatch, transport_trace, scenario, wire_trace=None):
    """Replace only data and the HTTP send boundary, and retain selected wire fields."""
    from tradingagents.graph import trading_graph

    factory = trading_graph.create_llm_client
    install_graph_boundary(monkeypatch, "600895.SS", transport_trace)
    monkeypatch.setattr(trading_graph, "create_llm_client", factory)
    if scenario == "unavailable_news":
        from scripts import personal_observations

        def unavailable_page(ticker, day, remaining, admit):
            assert (ticker, day) == ("600895.SS", DAY)
            remaining()
            admit()
            with transport_trace.open("a", encoding="utf8") as out:
                out.write("personal:public_news_unavailable\n")
            raise OSError("synthetic issuer news transport failure")

        monkeypatch.setattr(personal_observations, "get_page", unavailable_page)
    requests = []

    def send(request, family):
        assert (
            request.method == "POST"
            and str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        )
        assert request.headers.get("authorization") == "Bearer synthetic-example"
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        tool_result = any(msg["role"] == "tool" for msg in body["messages"])
        finish = "stop"
        message = {"role": "assistant", "content": "合成讨论。", "reasoning_content": REASONING}
        analyst = None
        phase = "debate_or_risk"

        def tool(name, args):
            message.update(
                content="",
                tool_calls=[
                    {
                        "id": f"fixture-{len(requests)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            )
            return "tool_calls"

        if "get_verified_market_snapshot" in names:
            analyst, phase = "market", "tools"
            if not tool_result:
                finish = tool(
                    "get_verified_market_snapshot", {"symbol": "600895.SS", "curr_date": DAY}
                )
            elif scenario == "length_tools":
                finish, message["content"] = "length", PARTIAL
        elif "get_news" in names:
            analyst, phase = "news", "tools"
            if scenario == "unavailable_news":
                if not tool_result:
                    finish = tool(
                        "get_news",
                        {"ticker": "600895.SS", "start_date": "2024-02-22", "end_date": DAY},
                    )
                else:
                    assert any(
                        msg["role"] == "tool"
                        and "DATA_UNAVAILABLE: news acquisition unavailable" in msg["content"]
                        for msg in body["messages"]
                    )
        elif "get_balance_sheet" in names:
            analyst, phase = "fundamentals", "tools"
        elif body.get("response_format") == {"type": "json_object"}:
            phase = "finalizer"
            prompt = "\n".join(msg.get("content") or "" for msg in body["messages"])
            match = re.search(r"Finalize the (market|news|fundamentals) analyst", prompt)
            if match:
                analyst = match[1]
                content = {
                    "report_markdown": "合成记录，已标注资料覆盖。",
                    "material_claims": [],
                    "evidence_notes": [],
                }
                if analyst == "news" and scenario == "incomplete":
                    content = {"material_claims": []}
                if analyst == "news" and scenario == "length_finalizer":
                    finish, content = "length", {"report_markdown": PARTIAL}
                if analyst == "news" and scenario == "unavailable_news":
                    content["report_markdown"] = NO_NEWS_REPORT
                message["content"] = json.dumps(content)
            else:
                assert "matching SentimentReport" in prompt
                analyst = "sentiment"
                message["content"] = json.dumps(
                    {
                        "overall_band": "Neutral",
                        "overall_score": 5,
                        "confidence": "low",
                        "narrative": "合成情绪材料。",
                        "material_claims": [],
                    }
                )
        elif "ResearchPlan" in names:
            phase = "research_plan"
            finish = tool(
                "ResearchPlan",
                {"recommendation": "Buy", "rationale": "fixture", "strategic_actions": "fixture"},
            )
        elif "TraderProposal" in names:
            phase = "trader"
            finish = tool("TraderProposal", {"action": "Buy", "reasoning": "fixture"})

        observation = {
            "analyst": analyst,
            "phase": phase,
            "model": body["model"],
            "thinking": body.get("thinking"),
            "has_thinking": "thinking" in body,
            "reasoning_effort": body.get("reasoning_effort"),
            "has_reasoning_effort": "reasoning_effort" in body,
            "max_tokens": body.get("max_tokens"),
            "alternate_cap": "max_completion_tokens" in body,
            "unmerged_extra_body": "extra_body" in body,
            "has_tool_choice": "tool_choice" in body,
            "tool_result": tool_result,
            "replayed_reasoning": any("reasoning_content" in msg for msg in body["messages"]),
            "sdk_retry": request.headers.get("x-stainless-retry-count"),
        }
        requests.append(observation)
        with transport_trace.open("a", encoding="utf8") as out:
            out.write("model\n")
        if wire_trace is not None:
            with wire_trace.open("a", encoding="utf8") as out:
                out.write(json.dumps(observation) + "\n")
        return family.Response(
            200,
            request=request,
            json={
                "id": "fixture-completion",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 12,
                    "total_tokens": 22,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    for family in httpx_transport_modules():
        monkeypatch.setattr(
            family.HTTPTransport,
            "handle_request",
            lambda self, request, module=family: send(request, module),
        )
    return requests


def assert_wire_policy(requests, *, full, quick, effort, limit):
    assert requests
    assert all(
        request["max_tokens"] == limit
        and not request["alternate_cap"]
        and not request["unmerged_extra_body"]
        for request in requests
    )
    assert all(request["sdk_retry"] == "0" for request in requests)
    analysts = [request for request in requests if request["analyst"]]
    assert {request["analyst"] for request in analysts} == ANALYSTS
    assert all(request["model"] == quick and not request["has_tool_choice"] for request in analysts)
    for request in requests:
        if full and request["analyst"]:
            assert request["thinking"] == {"type": "disabled"}
            assert not request["has_reasoning_effort"] and not request["replayed_reasoning"]
        else:
            assert not request["has_thinking"] and request["reasoning_effort"] == effort
            assert request["has_reasoning_effort"] is (effort is not None)
    market_followups = [r for r in analysts if r["analyst"] == "market" and r["tool_result"]]
    assert len(market_followups) == 1 and market_followups[0]["replayed_reasoning"] is (not full)


def assert_unavailable_news_outcome(receipt, run_dir, attempts):
    assert attempts.count("personal:public_news_unavailable") == 1
    assert "personal:public_news" not in attempts
    assert receipt["enhancements"]["news"] == "unavailable"
    assert receipt["source_evidence"]["news"] == {
        "status": "unavailable",
        "returned_to_news_tool": False,
    }
    assert receipt["news_content_verified"] is False
    events = receipt["analyst_output_diagnostics"]["events"]
    assert [
        e["observation"] for e in events if e["analyst"] == "news" and e["stage"] == "finalizer"
    ] == ["success"]
    assert NO_NEWS_REPORT in (run_dir / receipt["report_relative_path"]).read_text(encoding="utf8")

    audit = json.loads((run_dir / "report" / "decision-audit.json").read_text(encoding="utf8"))
    evidence = audit["evidence_state"]
    capabilities = {"get_news", "sentiment_news"}
    outcomes = [
        row for row in evidence["acquisition_outcomes"] if row["capability"] in capabilities
    ]
    assert {row["capability"] for row in outcomes} == capabilities
    assert all(row["outcome"] == "unavailable" and not row.get("artifact_ref") for row in outcomes)
    unavailable_ids = {row["source_id"] for row in outcomes}
    assert not evidence["material_claims"]
    assert all(fact["source_id"] not in unavailable_ids for fact in evidence["source_facts"])
    assert audit["admission_gate"]["admitted"] is True
    assert audit["admission_gate"]["readiness"] == "degraded"
    assert "optional_evidence_unavailable" in audit["admission_gate"]["diagnostic_codes"]


@pytest.mark.parametrize(
    "scenario,full,quick,effort,limit",
    [
        ("success", True, "deepseek-v4-flash", None, 4096),
        ("success", True, "deepseek-flash", "high", 2048),
        ("success", False, "deepseek-v4-flash", None, 4096),
        ("length_tools", True, "deepseek-v4-flash", None, 4096),
        ("length_finalizer", True, "deepseek-v4-flash", None, 4096),
        ("incomplete", True, "deepseek-v4-flash", None, 4096),
        ("unavailable_news", True, "deepseek-v4-flash", None, 4096),
    ],
)
def test_personal_entry_deepseek_wire(
    tmp_path, monkeypatch, capsys, caplog, scenario, full, quick, effort, limit
):
    from tradingagents import default_config

    monkeypatch.setattr(personal_full, "ROOT", tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", quick)
    monkeypatch.setattr(
        default_config,
        "DEFAULT_CONFIG",
        dict(
            default_config.DEFAULT_CONFIG,
            analysis_max_calls=60,
            llm_max_output_tokens=limit,
            deepseek_reasoning_effort=effort,
        ),
    )
    trace = tmp_path / "synthetic-transport.txt"
    requests = install_personal_deepseek_wire(monkeypatch, trace, scenario)
    args = ["--ticker", "600895.SH", "--run", "--tushare-points", "2000"]
    code = personal_full.main(args if full else [*args, "--basic"])
    output = capsys.readouterr()
    receipt = json.loads(output.out)
    valid_submission = scenario in {"success", "unavailable_news"}
    assert code == (0 if valid_submission else 3), receipt.get("error")
    assert receipt["status"] == "completed" and receipt["report_written"] and receipt["usage_final"]
    assert (
        receipt["model_attempts"] == len(requests) == trace.read_text().splitlines().count("model")
    )
    assert receipt["admitted_attempts"] == len(trace.read_text().splitlines()) <= 60
    assert receipt["financial_attempts"] == 3
    assert_wire_policy(requests, full=full, quick=quick, effort=effort, limit=limit)
    order = Counter((row["analyst"], row["phase"]) for row in requests)
    assert order["market", "tools"] == 2
    assert order["news", "finalizer"] == (2 if scenario == "incomplete" else 1)
    assert order["market", "finalizer"] == (0 if scenario == "length_tools" else 1)
    count_after_admission = sum(row["analyst"] is None for row in requests)
    assert bool(count_after_admission) is valid_submission
    if valid_submission:
        assert receipt["rating_status"] == "validated_decision"
        assert any(
            r["model"] == "deepseek-v4-pro" and r["phase"] == "research_plan" for r in requests
        )
        assert any(r["model"] == quick and r["phase"] == "debate_or_risk" for r in requests)
    else:
        assert receipt["rating_status"] == "no_rating"
    folder = tmp_path / "reports" / "personal" / receipt["run_id"]
    raw = (folder / "status.json").read_text(encoding="utf8")
    report_path = folder / receipt["report_relative_path"]
    report = report_path.read_text(encoding="utf8")
    assert json.loads(raw) == receipt
    assert all(
        token not in block
        for token in (REASONING, PARTIAL, "synthetic-example")
        for block in (output.out, output.err, caplog.text, raw, report)
    )
    if not valid_submission:
        assert "admission_blocked" in report and "required_evidence_unavailable" in report
    if scenario == "unavailable_news":
        assert order["news", "tools"] == 2
        assert_unavailable_news_outcome(receipt, folder, trace.read_text().splitlines())
    if scenario in {"length_tools", "length_finalizer"}:
        analyst, stage, observation = {
            "length_tools": ("market", "initial_response", "raw_finish_reason_length"),
            "length_finalizer": ("news", "finalizer", "sdk_length_finish_reason_error"),
        }[scenario]
        assert "analyst_output_limit" in report
        assert {
            "analyst": analyst,
            "stage": stage,
            "observation": observation,
            "completion_tokens": 12,
            "reasoning_tokens": 0,
        } in receipt["analyst_output_diagnostics"]["events"]


def test_analyst_policy_does_not_change_legacy_deepseek_or_openai_payloads():
    from langchain_core.messages import HumanMessage

    from tradingagents.llm_clients.openai_client import (
        DeepSeekChatOpenAI,
        NormalizedChatOpenAI,
        deepseek_analyst_non_thinking,
    )

    for client in (
        DeepSeekChatOpenAI(model="deepseek-reasoner", api_key="synthetic-example"),
        NormalizedChatOpenAI(model="gpt-4o", api_key="synthetic-example"),
    ):
        messages = [HumanMessage(content="Synthetic analyst input")]
        baseline = client._get_request_payload(messages)
        with deepseek_analyst_non_thinking():
            assert client._get_request_payload(messages) == baseline

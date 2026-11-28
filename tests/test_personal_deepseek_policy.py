"""Personal CLI with the real DeepSeek SDK and synthetic HTTP/data boundaries."""

import json
import re
from collections import Counter

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from scripts import personal_bootstrap, personal_full
from tests.test_current_tushare_statements import statement_rows
from tests.test_personal_bootstrap import install_graph_boundary
from tradingagents.agents.analysts.submission import (
    _finalization_prompt,
    collected_personal_statements,
    finalize_observed_unavailable_news,
    personal_full_prompt_scope,
)
from tradingagents.analysis_budget import httpx_transport_modules
from tradingagents.dataflows.financial_dispatch import FinancialToolMessageAuditEnvelope
from tradingagents.evidence import (
    SourceAcquisitionAvailable,
    ToolExecutionEvidenceEnvelope,
    make_source_acquisition_outcome,
    stable_acquisition_source_ref,
)

REASONING = "fixture-private-reasoning-never-persist"
PARTIAL = "fixture-partial-report-never-publish"
NO_NEWS_REPORT = "本次查询未取得可核验的发行人新闻正文。覆盖不足，无法判断期间是否发生发行人事件。"
PRIVATE_FIELD = "fixture-unknown-private-field"
STATEMENT_TOOLS = {"get_balance_sheet", "get_income_statement", "get_cashflow"}
ANALYSTS = {"market", "sentiment", "news", "fundamentals"}
DAY = "2024-02-29"


def install_personal_deepseek_wire(monkeypatch, transport_trace, scenario, wire_trace=None):
    """Replace only data and the HTTP send boundary, and retain selected wire fields."""
    from tradingagents.graph import trading_graph

    factory = trading_graph.create_llm_client
    install_graph_boundary(monkeypatch, "600895.SS", transport_trace)
    monkeypatch.setattr(trading_graph, "create_llm_client", factory)
    post = personal_bootstrap.post

    def statement_post(token, endpoint, params, fields, remaining, admit):
        response = post(token, endpoint, params, fields, remaining, admit)
        if endpoint in personal_bootstrap.FINANCE:
            frame = statement_rows(endpoint)
            frame["ann_date"] = frame["f_ann_date"] = ["20230420", "20230820"]
            frame["end_date"] = ["20221231", "20230630"]
            response["data"].update(fields=list(frame.columns), items=frame.values.tolist())
        return response

    monkeypatch.setattr(personal_bootstrap, "post", statement_post)
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
        scoped = body.get("thinking") == {"type": "disabled"}
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        tool_result = any(msg["role"] == "tool" for msg in body["messages"])
        finish = "stop"
        message = {"role": "assistant", "content": "合成讨论。", "reasoning_content": REASONING}
        analyst = None
        phase = "debate_or_risk"
        completion_tokens = 12
        verified_statement_prompt = None

        def tool(name, args):
            calls = message.setdefault("tool_calls", [])
            message["content"] = ""
            calls.append(
                {
                    "id": f"fixture-{len(requests)}-{len(calls)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
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
            if scoped:
                if tool_result or scenario == "fundamentals_length":
                    finish, message["content"], completion_tokens = "length", PARTIAL, 4096
                elif scenario != "fundamentals_missing":
                    for name in sorted(STATEMENT_TOOLS):
                        finish = tool(
                            name, {"ticker": "600895.SS", "curr_date": DAY, "freq": "quarterly"}
                        )
                    finish = tool(
                        "get_income_statement",
                        {"ticker": "600895.SS", "curr_date": DAY, "freq": "annual"},
                    )
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
                    content = {
                        "material_claims": [
                            {
                                "claim_id": "news.synthetic",
                                "statement": "测试",
                                "source_ref": "fixture-ref",
                            }
                        ],
                        PRIVATE_FIELD: PARTIAL,
                    }
                if analyst == "news" and scenario == "news_constraint":
                    content["material_claims"] = [
                        {
                            "claim_id": "wrong.namespace",
                            "statement": "测试",
                            "source_ref": "fixture-ref",
                            "source_quote": "测试",
                        }
                    ]
                if analyst == "news" and scenario == "news_false_claim":
                    content["material_claims"] = [
                        {
                            "claim_id": "news.false",
                            "statement": "发行人有一项公告。",
                            "source_ref": "fixture-unsupported-ref",
                            "source_quote": "不存在的引用",
                        }
                    ]
                if analyst == "news" and scenario == "length_finalizer":
                    finish, content = "length", {"report_markdown": PARTIAL}
                if analyst == "news" and scenario == "unavailable_news":
                    content["report_markdown"] = NO_NEWS_REPORT
                if analyst == "fundamentals" and scoped:
                    verified_statement_prompt = {
                        "fields": all(
                            field in prompt
                            for field in (
                                "report_markdown",
                                "material_claims",
                                "evidence_notes",
                                "source_quote",
                            )
                        ),
                        "date_and_currency": all(
                            value in prompt for value in ("2022-12-31", "2023-04-20", "CNY")
                        ),
                        "quarterly": all(
                            f"{title} data for 600895.SS (quarterly)" in prompt
                            for title in ("Balance Sheet", "Income Statement", "Cash Flow")
                        ),
                        "annual": "Income Statement data for 600895.SS (annual)" in prompt,
                        "no_draft": "Unvalidated prose draft" not in prompt,
                    }
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
            "has_report_tool": "AnalystEvidenceReport" in names,
            "verified_statement_prompt": verified_statement_prompt,
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
                    "completion_tokens": completion_tokens,
                    "total_tokens": 10 + completion_tokens,
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
    ] == ["deterministic_unavailable_news"]
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
        ("news_constraint", True, "deepseek-v4-flash", None, 4096),
        ("incomplete", False, "deepseek-v4-flash", None, 4096),
        ("news_false_claim", True, "deepseek-v4-flash", None, 4096),
        ("unavailable_news", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_length", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_missing", True, "deepseek-v4-flash", None, 4096),
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
    valid_submission = scenario in {"success", "unavailable_news", "news_false_claim"}
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
    assert order["news", "finalizer"] == {
        "unavailable_news": 0,
        "incomplete": 2,
        "news_constraint": 2,
    }.get(scenario, 1)
    assert order["market", "finalizer"] == (0 if scenario == "length_tools" else 1)
    assert order["fundamentals", "tools"] == 1
    assert order["fundamentals", "finalizer"] == (
        0 if scenario in {"fundamentals_length", "fundamentals_missing"} else 1
    )
    assert all(
        row["has_report_tool"] is (not full)
        for row in requests
        if row["analyst"] == "fundamentals" and row["phase"] == "tools"
    )
    for row in requests:
        if row["verified_statement_prompt"] is not None:
            assert all(row["verified_statement_prompt"].values()), row["verified_statement_prompt"]
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
        for token in (REASONING, PARTIAL, PRIVATE_FIELD, "synthetic-example")
        for block in (output.out, output.err, caplog.text, raw, report)
    )
    if not valid_submission:
        assert "admission_blocked" in report and "required_evidence_unavailable" in report
    if scenario == "unavailable_news":
        assert order["news", "tools"] == 1
        assert_unavailable_news_outcome(receipt, folder, trace.read_text().splitlines())
    if scenario in {"incomplete", "news_constraint"}:
        events = receipt["analyst_output_diagnostics"]["events"]
        news = [
            event
            for event in events
            if event["analyst"] == "news" and event["stage"] == "finalizer"
        ]
        validation = (
            {
                "validation_kind": "schema_validation",
                "validation_fields": ["material_claims[].source_quote", "report_markdown"],
            }
            if scenario == "incomplete"
            else {"validation_kind": "claim_namespace"}
        )
        assert len(news) == 2 and all(
            {k: v for k, v in e.items() if k.startswith("validation_")}
            == (validation if full else {})
            for e in news
        )
    if scenario == "news_false_claim":
        audit = json.loads((folder / "report" / "decision-audit.json").read_text(encoding="utf8"))
        assert any(
            row["status"] == "unsupported" for row in audit["evidence_state"]["claim_validations"]
        )
    if scenario in {"length_tools", "length_finalizer", "fundamentals_length"}:
        analyst, stage, observation = {
            "length_tools": ("market", "initial_response", "raw_finish_reason_length"),
            "length_finalizer": ("news", "finalizer", "sdk_length_finish_reason_error"),
            "fundamentals_length": ("fundamentals", "initial_response", "raw_finish_reason_length"),
        }[scenario]
        assert "analyst_output_limit" in report
        assert {
            "analyst": analyst,
            "stage": stage,
            "observation": observation,
            "completion_tokens": 4096 if scenario == "fundamentals_length" else 12,
            "reasoning_tokens": 0,
        } in receipt["analyst_output_diagnostics"]["events"]


@pytest.mark.parametrize(
    "variant", ["stale_ref", "duplicate_id", "pending", "malformed", "available"]
)
def test_news_shortcut_requires_an_exact_unavailable_tool_result(variant):
    ticker = "600895.SS"
    args = {"ticker": ticker, "start_date": "2024-02-22", "end_date": DAY}
    source_ref = stable_acquisition_source_ref(
        "news",
        ticker,
        "2024-02-23" if variant == "stale_ref" else args["start_date"],
        DAY,
    )
    call = {"name": "get_news", "args": args, "id": "news-test", "type": "tool_call"}
    calls = [call]
    if variant == "duplicate_id":
        calls.append(call.copy())
    if variant == "pending":
        calls.append(dict(call, id="pending"))
    content = (
        "Dated synthetic issuer observation." if variant == "available" else "DATA_UNAVAILABLE"
    )
    outcome = make_source_acquisition_outcome(
        provider="fixture",
        capability="get_news",
        attempt=1,
        retrieved_at=f"{DAY}T08:15:00+00:00",
        source_ref=source_ref,
        tool_call_id="news-test",
        tool_name="get_news",
        content=content,
        status="success" if variant == "available" else "unavailable",
    )
    artifact = ToolExecutionEvidenceEnvelope(
        tool_call_id="news-test",
        tool_name="get_news",
        source_ref=source_ref,
        capability="get_news",
        acquisition_outcomes=(outcome,),
        selected_artifact=outcome.artifact
        if isinstance(outcome, SourceAcquisitionAvailable)
        else None,
    ).model_dump(mode="json")
    if variant == "malformed":
        artifact[PRIVATE_FIELD] = PARTIAL
    messages = [
        AIMessage(content="", tool_calls=calls),
        ToolMessage(content=content, tool_call_id="news-test", name="get_news", artifact=artifact),
    ]
    assert finalize_observed_unavailable_news(messages, ticker, DAY) is None


def test_financial_finalizer_preserves_middle_rows_and_reports_typed_gaps():
    ticker = "600895.SS"
    middle = "2021-12-31,2022-04-28,CNY,reported,1.0000"
    long_statement = "A" * 2_050 + "\n" + middle + "\n" + "B" * 2_050
    calls, results = [], []
    plans = [(name, "quarterly") for name in sorted(STATEMENT_TOOLS)] + [("get_cashflow", "annual")]
    for index, (name, frequency) in enumerate(plans):
        call_id = f"statement-{index}"
        ref = f"financial-request:v1:{index:064x}"
        gap = frequency == "annual"
        calls.append(
            {
                "name": name,
                "args": {"ticker": ticker, "curr_date": DAY, "freq": frequency},
                "id": call_id,
                "type": "tool_call",
            }
        )
        content = PARTIAL if gap else long_statement
        outcome = make_source_acquisition_outcome(
            provider="fixture",
            capability="company_financials",
            attempt=1,
            retrieved_at=f"{DAY}T08:15:00+00:00",
            source_ref=ref,
            tool_call_id=call_id,
            tool_name=name,
            content=content,
            status="unavailable" if gap else "success",
        )
        artifact = FinancialToolMessageAuditEnvelope(
            tool_call_id=call_id,
            tool_name=name,
            request_ref=ref,
            disposition="executed",
            artifact_sha256=outcome.artifact.artifact_sha256
            if isinstance(outcome, SourceAcquisitionAvailable)
            else None,
            acquisition_outcomes=(outcome,),
        ).model_dump(mode="json")
        results.append(
            ToolMessage(content=content, name=name, tool_call_id=call_id, artifact=artifact)
        )
    messages = [AIMessage(content="", tool_calls=calls), *results]
    assert not collected_personal_statements(messages[:-1], ticker, DAY)
    assert collected_personal_statements(messages, ticker, DAY)
    repeat_call = dict(calls[0], id="repeat")
    repeat_result = results[0].model_copy(
        update={
            "tool_call_id": "repeat",
            "artifact": dict(
                results[0].artifact, tool_call_id="repeat", disposition="duplicate_suppressed"
            ),
        }
    )
    repeated = [AIMessage(content="", tool_calls=[*calls, repeat_call]), *results, repeat_result]
    assert collected_personal_statements(repeated, ticker, DAY)
    unmatched = [
        AIMessage(content="", tool_calls=[*calls[1:], repeat_call]),
        *results[1:],
        repeat_result,
    ]
    assert not collected_personal_statements(unmatched, ticker, DAY)
    wrong_window = dict(repeat_call, args=dict(repeat_call["args"], freq="annual"))
    mismatched = [AIMessage(content="", tool_calls=[*calls, wrong_window]), *results, repeat_result]
    assert not collected_personal_statements(mismatched, ticker, DAY)
    with personal_full_prompt_scope(True):
        prompt = _finalization_prompt(
            analyst="fundamentals",
            ticker=ticker,
            trade_date=DAY,
            draft="",
            messages=repeated,
            complete_statement_rows=True,
        )
    assert prompt.count(middle) == 3 and "[middle omitted]" not in prompt
    assert f"get_cashflow (annual) through {DAY}" in prompt and PARTIAL not in prompt


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

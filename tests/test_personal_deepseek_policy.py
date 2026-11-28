"""Personal CLI with the real DeepSeek SDK and synthetic HTTP/data boundaries."""

import gzip
import json
import re
from collections import Counter
from csv import reader
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from scripts import personal_bootstrap, personal_full
from tests.test_current_tushare_statements import statement_rows
from tests.test_personal_bootstrap import install_graph_boundary
from tradingagents.agents.analysts.submission import (
    _personal_statement_prompt,
    collected_personal_statements,
    finalize_observed_unavailable_news,
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
FINANCIAL_LIMITATION = "现金流资料缺失，无法形成现金流主张。"
STATEMENT_TOOLS = {"get_balance_sheet", "get_income_statement", "get_cashflow"}
ANALYSTS = {"market", "sentiment", "news", "fundamentals"}
DAY = "2024-02-29"
MARKET_EXTRA_FIELDS = {
    "market_wrong_value": {"statement": "fixture unsupported price 98765"},
    "market_wrong_date": {"effective_date": "2024-02-28"},
    "market_wrong_basis": {"adjustment_basis": "raw"},
    "market_wrong_window": {"window": 10},
}
MARKET_SOURCE_MISMATCHES = {
    "market_wrong_snapshot": ("- Adjustment basis: qfq", "- Adjustment basis: raw"),
    "market_wrong_snapshot_date": (f"- Latest trading row used: {DAY}", "- Latest trading row used: 2024-02-28"),
    "market_wrong_source_value": ("| Close | 12.00 |", "| Close | 98765.00 |"),
}


def _statement_claims(prompt):
    fields = {
        "Balance Sheet": "total_assets",
        "Income Statement": "total_revenue",
        "Cash Flow": "n_cashflow_act",
    }
    for title, table in re.findall(
        r"^# ([^\n]+) data for [^\n]+ \((?:annual|quarterly)\)\n(.*?)(?=\n# |\n\n|\Z)",
        prompt,
        re.MULTILINE | re.DOTALL,
    ):
        for content in table.splitlines():
            evidence_id, _, line = content.partition(" | ")
            if not evidence_id.startswith("R") or not line.startswith("2022-12-31,"):
                continue
            row = next(reader([line]))
            if row[5] == fields[title]:
                yield {"evidence_id": evidence_id}
                break


def _fundamental_wire_schema(prompt):
    _, found, raw = prompt.rpartition("\nJSON Schema:\n")
    schema = json.loads(raw) if found else {}
    fields = {"report_markdown", "material_claims", "evidence_notes"}
    citation = {"evidence_id"}
    return (
        "matching PersonalFundamentalsReport" in prompt
        and schema.get("additionalProperties") is False
        and set(schema.get("required", ())) == set(schema.get("properties", {})) == fields
        and schema["properties"]["report_markdown"]["maxLength"] == 480
        and schema["properties"]["material_claims"]["maxItems"] == 6
        and schema["properties"]["evidence_notes"]["maxItems"] == 3
        and {frozenset(entry["required"]) for entry in schema["$defs"].values()}
        == {frozenset(citation), frozenset({*citation, "statement", "kind"})}
        and all(
            entry["additionalProperties"] is False
            and set(entry["properties"]) == set(entry["required"])
            for entry in schema["$defs"].values()
        )
    )


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
            if scenario == "fundamentals_coverage_gap" and endpoint == "cashflow":
                frame = frame.iloc[:0]
            response["data"].update(fields=list(frame.columns), items=frame.values.tolist())
        return response

    monkeypatch.setattr(personal_bootstrap, "post", statement_post)
    if scenario in MARKET_SOURCE_MISMATCHES:
        from tradingagents.agents.utils import market_data_validation_tools

        render = market_data_validation_tools.build_verified_market_snapshot
        monkeypatch.setattr(
            market_data_validation_tools,
            "build_verified_market_snapshot",
            lambda *args: render(*args).replace(*MARKET_SOURCE_MISMATCHES[scenario]),
        )
    if scenario == "fundamentals_metadata":
        from tradingagents.dataflows import current_tushare_statements

        render = current_tushare_statements._render

        def render_large_statement(*args):
            title, _, body = render(*args).partition("\n")
            # Exercise the retention cutoff with otherwise unchanged, real CSV rows.
            return title + "\n# Synthetic comparison-size fixture " + "x" * 16_384 + "\n" + body

        monkeypatch.setattr(current_tushare_statements, "_render", render_large_statement)
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
        verified_market_prompt = None
        statement_output_chars = None

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
            if scenario == "length_tools":
                finish, message["content"] = "length", PARTIAL
            elif not tool_result and scoped and scenario == "fundamentals_metadata":
                finish = tool("get_stock_data", {
                    "symbol": "600895.SS", "start_date": "2023-11-30", "end_date": DAY,
                })
            elif scoped and scenario == "fundamentals_metadata" and not any(
                msg["role"] == "tool" and "## close_50_sma values" in (msg.get("content") or "")
                for msg in body["messages"]
            ):
                finish = tool("get_indicators", {
                    "symbol": "600895.SS", "indicator": "close_50_sma", "curr_date": DAY,
                    "look_back_days": 20,
                })
            elif not tool_result or scoped:
                finish = tool(
                    "get_verified_market_snapshot", {"symbol": "600895.SS", "curr_date": DAY}
                )
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
                    for name in sorted(STATEMENT_TOOLS):
                        finish = tool(
                            name, {"ticker": "600895.SS", "curr_date": DAY, "freq": "annual"}
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
                if analyst == "market" and scoped:
                    _, _, raw_schema = prompt.rpartition("\nJSON Schema:\n")
                    schema = json.loads(raw_schema) if raw_schema else {}
                    definitions = schema.get("$defs", {})
                    citation = definitions.get("_PersonalMarketCitation", {})
                    verified_market_prompt = {
                        "closed_citation": citation.get("additionalProperties") is False
                        and set(citation.get("properties", {})) == {"evidence_id"},
                        "market_rows": all(text in prompt for text in (
                            f"M01 | Close on {DAY}: 12.00 CNY (qfq)",
                            f"M07 | 50-day SMA on {DAY}: 12.00",
                            "Exact quote: | close_50_sma | 12.00 |",
                        )),
                        "no_unbound_bollinger": "Exact quote: | boll |" not in prompt,
                        "advisory_context": scenario != "fundamentals_metadata" or all(
                            text in prompt for text in (
                                "Collected market context for commentary and evidence notes only.",
                                "from 2023-11-30 to 2024-02-29",
                                "## close_50_sma values",
                            )
                        ),
                    }
                    content["material_claims"] = [
                        {"evidence_id": "M01"}, {"evidence_id": "M07"}
                    ]
                    if scenario in MARKET_EXTRA_FIELDS:
                        content["material_claims"][0].update(MARKET_EXTRA_FIELDS[scenario])
                    if scenario == "market_unbound_indicator":
                        content["material_claims"][0]["evidence_id"] = "M99"
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
                    wire_schema = _fundamental_wire_schema(prompt)
                    verified_statement_prompt = {
                        "wire_schema": wire_schema,
                        "fields": all(
                            field in prompt
                            for field in (
                                "report_markdown",
                                "material_claims",
                                "evidence_notes",
                                "evidence_id",
                            )
                        ),
                        "date_and_currency": all(
                            value in prompt for value in ("2022-12-31", "2023-04-20", "CNY")
                        ),
                        "quarterly": all(
                            f"{title} data for 600895.SS (quarterly)" in prompt
                            for title in ("Balance Sheet", "Income Statement")
                        ),
                        "cashflow": (
                            f"get_cashflow (quarterly) through {DAY}: empty_frame." in prompt
                            if scenario == "fundamentals_coverage_gap"
                            else "Cash Flow data for 600895.SS (quarterly)" in prompt
                        ),
                        "annual": all(
                            f"{title} data for 600895.SS (annual)" in prompt
                            for title in ("Balance Sheet", "Income Statement")
                        ),
                        "no_draft": "Unvalidated prose draft" not in prompt,
                    }
                    if wire_schema and scenario in {
                        "fundamentals_metadata",
                        "fundamentals_invalid_payload",
                        "fundamentals_coverage_gap",
                        "fundamentals_wrong_evidence",
                        "fundamentals_false_value",
                        "fundamentals_excess_claims",
                    }:
                        claims = list(_statement_claims(prompt))
                        if scenario == "fundamentals_invalid_payload":
                            del claims[0]["evidence_id"]
                            claims[0][PRIVATE_FIELD] = PARTIAL
                            completion_tokens = (
                                2710 if "Previous structured attempt" not in prompt else 2978
                            )
                        if scenario == "fundamentals_wrong_evidence":
                            claims[0]["evidence_id"] = "R999"
                        if scenario == "fundamentals_false_value":
                            claims[0]["statement"] = "Reported value was 12.35 亿元."
                        if scenario == "fundamentals_excess_claims":
                            claims.append(claims[0].copy())
                        if scenario == "fundamentals_coverage_gap":
                            content["report_markdown"] = FINANCIAL_LIMITATION
                        content["material_claims"] = claims
                        if scenario == "fundamentals_metadata":
                            exact_rows = dict(
                                re.findall(r"^(R[0-9]{3}) \| (.+)$", prompt, re.MULTILINE)
                            )
                            expanded = dict(
                                content,
                                material_claims=[
                                    {
                                        "statement": "Exact amount and period from the statement row.",
                                        "source_ref": "financial-request:v1:" + "0" * 64,
                                        "source_quote": exact_rows[claim["evidence_id"]],
                                    }
                                    for claim in claims
                                ],
                            )
                            statement_output_chars = [
                                len(json.dumps(item)) for item in (content, expanded)
                            ]
                    if scenario == "fundamentals_large_report":
                        content["report_markdown"] = "测" * 481
                message["content"] = json.dumps(content)
                if analyst == "fundamentals":
                    if scenario == "fundamentals_partial_json":
                        message["content"] = message["content"][:-1]
                    if scenario == "fundamentals_duplicate_json":
                        message["content"] = message["content"][:-1] + ', "evidence_notes": []}'
                    if scenario == "fundamentals_finalizer_length":
                        finish, completion_tokens = "length", 4096
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
            "verified_market_prompt": verified_market_prompt,
            "statement_output_chars": statement_output_chars,
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
    if not full:
        assert len(market_followups) == 1 and market_followups[0]["replayed_reasoning"]


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
    assert all(claim["analyst"] != "news" for claim in evidence["material_claims"])
    assert all(fact["source_id"] not in unavailable_ids for fact in evidence["source_facts"])
    assert audit["admission_gate"]["admitted"] is True
    assert audit["admission_gate"]["readiness"] == "degraded"
    assert "optional_evidence_unavailable" in audit["admission_gate"]["diagnostic_codes"]


def assert_personal_financial_outcome(receipt, run_dir, scenario):
    events = receipt["analyst_output_diagnostics"]["events"]
    finalizers = [e for e in events if e["analyst"] == "fundamentals" and e["stage"] == "finalizer"]
    if scenario == "fundamentals_invalid_payload":
        assert [
            (e["observation"], e["validation_kind"], e["validation_fields"], e["completion_tokens"])
            for e in finalizers
        ] == [
            ("schema_validation", "schema_validation", ["material_claims[].evidence_id"], n)
            for n in (2710, 2978)
        ]
        return
    if scenario == "fundamentals_wrong_evidence":
        assert [(e["observation"], e["validation_kind"]) for e in finalizers] == [
            ("validation_error", "unknown_evidence_id")
        ] * 2
        return
    if scenario == "fundamentals_false_value":
        assert [(e["validation_kind"], e["validation_fields"]) for e in finalizers] == [
            ("schema_validation", ["material_claims[].statement"])
        ] * 2
        assert "12.35" not in (run_dir / receipt["report_relative_path"]).read_text(encoding="utf8")
        audit = json.loads((run_dir / "report" / "decision-audit.json").read_text(encoding="utf8"))
        assert all(
            not row["claim_id"].startswith("fundamentals.")
            for row in audit["evidence_state"]["claim_validations"]
        )
        return
    if scenario in {"fundamentals_large_report", "fundamentals_excess_claims"}:
        field = "report_markdown" if scenario == "fundamentals_large_report" else "material_claims"
        assert [(e["validation_kind"], e["validation_fields"]) for e in finalizers] == [
            ("schema_validation", [field])
        ] * 2
        return
    if scenario in {"fundamentals_partial_json", "fundamentals_duplicate_json"}:
        assert [(e["observation"], e["validation_kind"]) for e in finalizers] == [
            ("json_syntax", "json_syntax")
        ] * 2
        assert all("validation_fields" not in e for e in finalizers)
        return
    if scenario == "fundamentals_finalizer_length":
        assert [e["observation"] for e in finalizers] == ["sdk_length_finish_reason_error"]
        assert finalizers[0]["completion_tokens"] == 4096 and "validation_kind" not in finalizers[0]
        return
    if scenario not in {
        "fundamentals_metadata",
        "fundamentals_coverage_gap",
    }:
        return
    assert [e["observation"] for e in finalizers] == ["success"]
    audit = json.loads((run_dir / "report" / "decision-audit.json").read_text(encoding="utf8"))
    evidence = audit["evidence_state"]
    financial_claims = [
        row for row in evidence["material_claims"] if row["analyst"] == "fundamentals"
    ]
    expected_count = 4 if scenario == "fundamentals_coverage_gap" else 6
    assert [row["claim_id"] for row in financial_claims] == [
        f"fundamentals.claim-{n}" for n in range(1, expected_count + 1)
    ]
    validations = {row["claim_id"]: row for row in evidence["claim_validations"]}
    for claim in financial_claims:
        row = validations[claim["claim_id"]]
        assert row["status"] == "supported" and len(row["fact_ids"]) == 1
        if scenario == "fundamentals_metadata":
            comparison = row["comparison_evidence"]
            assert comparison["status"] == "unavailable"
            assert comparison["unavailable_reason"] == "input_limit" and comparison["checks"] == []
    financial_facts = [row for row in evidence["source_facts"] if row.get("financial_context")]
    assert len(financial_facts) == expected_count
    amounts = {"total_assets": "1000", "operating_revenue": "900", "cash_from_operations": "160"}
    groups = set()
    for fact in financial_facts:
        context = fact["financial_context"]
        digest = fact["artifact_ref"].removeprefix("artifact=sha256:")
        path = run_dir / "report" / "evidence_artifacts" / "sha256" / digest[:2] / f"{digest}.utf8.gz"
        raw = gzip.decompress(path.read_bytes())
        assert sha256(raw).hexdigest() == digest
        source = raw.decode("utf8")
        if scenario == "fundamentals_metadata":
            assert len(source) > 16_384
        row = next(reader([source[fact["source_span_start"]:fact["source_span_end"]]], strict=True))
        assert row[8:11] == [context["field"], amounts[context["field"]], "CNY_BASE_UNIT"]
        assert context["unit"] == "CNY" and context["scale"] == "1"
        assert context["period_end"] == "2022-12-31"
        assert context["period_basis"] == (
            "period_end_balance" if context["field"] == "total_assets" else "cumulative_report_type_1"
        )
        groups.add((context["field"], source.splitlines()[0].rsplit("(", 1)[-1].rstrip(")")))
    covered = set(amounts) if expected_count == 6 else {"total_assets", "operating_revenue"}
    assert groups == {(field, frequency) for field in covered for frequency in ("annual", "quarterly")}
    assert all(row["financial_context"]["decision_qualified"] is False for row in financial_facts)
    directional_facts = {row["fact_id"] for row in audit["validated_decision_context"]["facts"]}
    assert directional_facts.isdisjoint(row["fact_id"] for row in financial_facts)
    assert audit["admission_gate"]["admitted"] is True
    report = (run_dir / receipt["report_relative_path"]).read_text(encoding="utf8")
    if scenario == "fundamentals_metadata":
        for marker in (
            "total_assets | 1000 CNY",
            "operating_revenue | 900 CNY",
            "cash_from_operations | 160 CNY",
            "period_end=2022-12-31",
            "ann_date=2023-04-20",
            "retrieved_at=2024-02-29",
            "observed_at=2024-02-29",
        ):
            assert marker in report, marker
    if scenario == "fundamentals_coverage_gap":
        assert FINANCIAL_LIMITATION in report
        cashflow = [
            row
            for row in audit["financial_dispatch"]["requests"]
            if row["tool_name"] == "get_cashflow"
        ]
        assert len(cashflow) == 2 and all(row["artifact_sha256"] is None for row in cashflow)


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
        ("fundamentals_invalid_payload", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_partial_json", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_duplicate_json", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_finalizer_length", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_metadata", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_coverage_gap", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_wrong_evidence", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_false_value", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_large_report", True, "deepseek-v4-flash", None, 4096),
        ("fundamentals_excess_claims", True, "deepseek-v4-flash", None, 4096),
        *(
            (scenario, True, "deepseek-v4-flash", None, 4096)
            for scenario in (*MARKET_EXTRA_FIELDS, "market_unbound_indicator", *MARKET_SOURCE_MISMATCHES)
        ),
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
    valid_submission = scenario in {
        "success",
        "unavailable_news",
        "news_false_claim",
        "fundamentals_metadata",
        "fundamentals_coverage_gap",
    }
    assert code == (0 if valid_submission else 3), receipt.get("error")
    assert receipt["status"] == "completed" and receipt["report_written"] and receipt["usage_final"]
    assert (
        receipt["model_attempts"] == len(requests) == trace.read_text().splitlines().count("model")
    )
    assert receipt["admitted_attempts"] == len(trace.read_text().splitlines()) <= 60
    assert receipt["financial_attempts"] == 3
    assert_wire_policy(requests, full=full, quick=quick, effort=effort, limit=limit)
    order = Counter((row["analyst"], row["phase"]) for row in requests)
    assert order["market", "tools"] == (3 if scenario == "fundamentals_metadata" else 2 if not full else 1)
    assert order["news", "finalizer"] == {
        "unavailable_news": 0,
        "incomplete": 2,
        "news_constraint": 2,
    }.get(scenario, 1)
    assert order["market", "finalizer"] == (
        0 if scenario in {"length_tools", *MARKET_SOURCE_MISMATCHES}
        else 2 if scenario in {*MARKET_EXTRA_FIELDS, "market_unbound_indicator"}
        else 1
    )
    assert order["fundamentals", "tools"] == 1
    assert order["fundamentals", "finalizer"] == {
        "fundamentals_length": 0,
        "fundamentals_missing": 0,
        "fundamentals_invalid_payload": 2,
        "fundamentals_partial_json": 2,
        "fundamentals_duplicate_json": 2,
        "fundamentals_wrong_evidence": 2,
        "fundamentals_false_value": 2,
        "fundamentals_large_report": 2,
        "fundamentals_excess_claims": 2,
    }.get(scenario, 1)
    assert all(
        row["has_report_tool"] is (not full)
        for row in requests
        if row["analyst"] in {"fundamentals", "market"} and row["phase"] == "tools"
    )
    for row in requests:
        if row["verified_statement_prompt"] is not None:
            assert all(row["verified_statement_prompt"].values()), row["verified_statement_prompt"]
        if row["verified_market_prompt"] is not None:
            assert all(row["verified_market_prompt"].values()), row["verified_market_prompt"]
        if row["statement_output_chars"] is not None:
            compact, expanded = row["statement_output_chars"]
            assert 0 < compact * 2 < expanded
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
    if scenario.startswith("fundamentals_"):
        assert_personal_financial_outcome(receipt, folder, scenario)
    if full and (valid_submission or scenario.startswith("market_")):
        assert_personal_market_outcome(receipt, folder, scenario)
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


def assert_personal_market_outcome(receipt, folder, scenario):
    report_path = folder / receipt["report_relative_path"]
    report = report_path.read_text(encoding="utf8")
    audit = json.loads((folder / "report" / "decision-audit.json").read_text(encoding="utf8"))
    market_claims = [
        claim for claim in audit["evidence_state"]["claim_validations"]
        if claim["claim_id"].startswith("market.")
    ]
    events = [
        event for event in receipt["analyst_output_diagnostics"]["events"]
        if event["analyst"] == "market" and event["stage"] == "finalizer"
    ]
    if scenario.startswith("market_"):
        assert not market_claims and audit["admission_gate"]["admitted"] is False
        if scenario in MARKET_SOURCE_MISMATCHES:
            assert not events
        else:
            observation = "validation_error" if scenario == "market_unbound_indicator" else "schema_validation"
            assert [event["observation"] for event in events] == [observation, observation]
        assert "98765" not in report
        return
    assert [event["observation"] for event in events] == ["success"]
    assert {claim["claim_id"]: claim["status"] for claim in market_claims} == {
        "market.M01": "supported", "market.M07": "supported",
    }
    for claim in market_claims:
        assert len(claim["fact_ids"]) == 1
    assert "Close on 2024-02-29: 12.00 CNY (qfq)" in report
    assert "50-day SMA on 2024-02-29: 12.00 CNY (qfq)" in report
    assert audit["admission_gate"]["admitted"] is True


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


def test_financial_collection_retains_frequencies_and_excludes_unbindable_text():
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
    prompt, catalog = _personal_statement_prompt(repeated, ticker, DAY)
    assert middle not in prompt and not catalog
    assert f"get_cashflow (annual) through {DAY}" in prompt and PARTIAL not in prompt


def test_bank_statement_excerpts_keep_core_data_and_expose_gaps_without_handles():
    from tradingagents.agents.analysts.statement_excerpts import statement_excerpts
    from tradingagents.financial_observations import (
        CURRENT_OBSERVATION_MARKER,
        CURRENT_STATEMENT_COLUMNS,
    )

    stamp = f"{DAY}T08:30:00+00:00"

    def row(period, announced, field, provider_field="", value=""):
        unit = "CNY_BASE_UNIT" if value else ""
        return ",".join(
            (
                period,
                announced,
                announced,
                stamp,
                stamp,
                provider_field,
                value,
                unit,
                field,
                value,
                unit,
                "financial-provider-artifact:v1:" + "0" * 64,
                "reported" if value else "missing",
            )
        )

    income = row("2023-06-30", "2023-08-20", "interest_income", "int_income", "7")
    expense = row("2023-06-30", "2023-08-20", "interest_expense", "int_exp", "5")
    latest_gap = row("2023-06-30", "2023-08-20", "net_income")
    earlier_profit = row("2022-12-31", "2023-04-20", "net_income", "n_income_attr_p", "2")
    future = row("2023-12-31", "2024-03-01", "interest_income", "int_income", "9")
    current = "# CURRENT-only; source=tushare; not strict PIT evidence."
    raw = "\n".join(
        (
            "# Income Statement data for 600895.SS (quarterly)",
            current,
            CURRENT_OBSERVATION_MARKER,
            ",".join(CURRENT_STATEMENT_COLUMNS),
            income,
            expense,
            latest_gap,
            earlier_profit,
            future,
        )
    )
    args = {
        "tool_name": "get_income_statement",
        "frequency": "quarterly",
        "ticker": "600895.SS",
        "trade_date": DAY,
        "source_ref": "fixture",
        "tool_call_id": "fixture",
    }
    rows, notes, gaps = statement_excerpts(
        raw, artifact_sha256=sha256(raw.encode()).hexdigest(), **args
    )
    assert [quote for quote, _ in rows] == [income, expense, earlier_profit]
    assert [(row.field, row.value, row.period_end) for _, row in rows] == [
        ("interest_income", "7", "2023-06-30"),
        ("interest_expense", "5", "2023-06-30"),
        ("net_income", "2", "2022-12-31"),
    ]
    assert all(row.unit == "CNY" and row.period_basis == "cumulative_report_type_1" for _, row in rows)
    assert notes == [current] and gaps == ["2023-06-30 net_income: missing"]
    assert statement_excerpts(raw, artifact_sha256="0" * 64, **args) == ([], [], [])


def test_personal_financial_unknown_validation_is_explicit_and_private(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.test_personal_analyst_output_receipt import _run_receipt
    from tradingagents.agents.utils.structured import invoke_required_structured

    def reject(_):
        raise ValueError(PARTIAL)

    def exercise():
        result = invoke_required_structured(
            SimpleNamespace(invoke=reject),
            "synthetic",
            "Fundamentals Analyst",
            max_attempts=1,
            personal_validation_details=True,
        )
        assert result.value is None and result.reason == "validation_error"

    observations = _run_receipt(tmp_path, monkeypatch, exercise)
    assert observations == {
        "events": [
            {
                "analyst": "fundamentals",
                "stage": "finalizer",
                "observation": "validation_error",
                "validation_kind": "validation_unspecified",
            }
        ],
        "dropped_events": 0,
    }
    receipt = next(tmp_path.glob("*/status.json")).read_text(encoding="utf8")
    assert PARTIAL not in receipt


def test_personal_financial_content_cannot_supply_assembly_metadata():
    from pydantic import ValidationError

    from tradingagents.agents.analysts.submission import (
        _normalize_personal_fundamentals,
        _StatementEvidence,
    )
    from tradingagents.evidence import EVIDENCE_CONTRACT_VERSION, unsupported_numeric_claim_tokens
    from tradingagents.financial_observations import FinancialObservation

    citation = {"evidence_id": "R001"}
    observation = FinancialObservation(
        observation_id="finobs:" + "0" * 64,
        symbol="600895.SS",
        tool_name="get_cashflow",
        frequency="quarterly",
        field="cash_from_operations",
        provider_field="n_cashflow_act",
        value="1234567890",
        original_value="1234567890",
        original_unit="CNY_BASE_UNIT",
        period_end="2023-06-30",
        ann_date="2023-08-20",
        f_ann_date="2023-08-20",
        period_basis="cumulative_report_type_1",
        provider_artifact_identity="fixture-artifact",
    )
    quote = "2023-06-30,cash_from_operations,1234567890,CNY_BASE_UNIT"
    catalog = {
        "R001": _StatementEvidence("fixture-ref", quote, observation),
        "N001": _StatementEvidence("fixture-ref", "Exact note."),
    }
    payload = {
        "report_markdown": "Synthetic report.",
        "material_claims": [citation],
        "evidence_notes": [
            {"statement": "Synthetic coverage limitation.", "kind": "coverage", "evidence_id": "N001"}
        ],
    }
    _, (claim,), (note,) = _normalize_personal_fundamentals(payload, catalog)
    assert (claim.claim_id, note.note_id) == ("fundamentals.claim-1", "fundamentals.note-1")
    assert claim.contract_version == note.contract_version == EVIDENCE_CONTRACT_VERSION
    assert note.source_ref == claim.source_refs[0] == "fixture-ref"
    assert note.source_quote == "Exact note." and claim.source_quote == quote
    assert claim.statement == (
        "cash_from_operations: 1234567890 CNY "
        "(cumulative from fiscal year start through 2023-06-30; quarterly source)."
    )
    assert note.binding_status == "unverified" and note.decision_qualified is False
    with pytest.raises(ValidationError):
        _normalize_personal_fundamentals(
            dict(payload, contract_version=EVIDENCE_CONTRACT_VERSION), catalog
        )
    with pytest.raises(ValidationError):
        _normalize_personal_fundamentals(
            dict(payload, material_claims=[dict(citation, claim_id="fundamentals.fake")]), catalog
        )
    with pytest.raises(ValidationError):
        _normalize_personal_fundamentals(
            {k: v for k, v in payload.items() if k != "evidence_notes"}, catalog
        )
    with pytest.raises(ValidationError):
        _normalize_personal_fundamentals(
            dict(payload, evidence_notes=payload["evidence_notes"] * 4), catalog
        )
    with pytest.raises(ValidationError):
        _normalize_personal_fundamentals(
            dict(payload, evidence_notes=[dict(payload["evidence_notes"][0], statement="x" * 97)]),
            catalog,
        )
    with pytest.raises(ValidationError):
        _normalize_personal_fundamentals(
            dict(payload, material_claims=[dict(citation, evidence_id="N001")]), catalog
        )

    assert unsupported_numeric_claim_tokens((claim,), (quote,)) == ()
    restated = claim.model_copy(update={"statement": "2023-06-30 cash_from_operations 12.35 亿元。"})
    assert unsupported_numeric_claim_tokens((restated,), (quote,)) == ("12.35",)
    for extra in ({"statement": restated.statement}, {"amount": "12.35"}, {"source_quote": "wrong"}):
        with pytest.raises(ValidationError):
            _normalize_personal_fundamentals(
                dict(payload, material_claims=[dict(citation, **extra)]), catalog
            )


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

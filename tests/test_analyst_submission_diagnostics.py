"""Replay retained run04 failure shapes; model replies are explicitly synthetic."""

import json
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion

from tests.test_structured_agents import _tool_exchange
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.analysts.submission import build_analyst_update, process_analyst_response
from tradingagents.evidence import EvidenceSource, EvidenceState
from tradingagents.evidence_diagnostics import source_diagnostic
from tradingagents.report_presentation import evidence_diagnostic_markdown


@pytest.mark.parametrize("analyst,label", [
    ("market", "Market analyst submission"),
    ("sentiment", "Sentiment analyst submission"),
    ("news", "News analyst submission"),
    ("fundamentals", "Fundamentals analyst submission"),
])
def test_retained_submission_failure_is_named_without_changing_admission(analyst, label):
    source = EvidenceSource(source_id=f"analyst.{analyst}.submission",
                            status="unavailable", required=True, detail="output_limit")
    evidence = EvidenceState(sources=(source,))
    before = evidence.model_dump(mode="json")
    public = source_diagnostic(source)
    text = evidence_diagnostic_markdown(evidence, "Chinese")
    assert public.display_name == label
    assert public.capability == "analyst_submission"
    assert public.reason.value == "analyst_output_limit"
    assert label in text and "analyst_output_limit" in text
    assert "必需来源" in text
    assert evidence.model_dump(mode="json") == before


@pytest.mark.parametrize("source_id,detail", [
    ("analyst.market.submission:PRIVATE", "output_limit"),
    ("analyst.unrecognized.submission", "output_limit"),
    ("analyst.market.submission", "output_limit:PRIVATE"),
])
def test_unrecognized_submission_metadata_remains_opaque(source_id, detail):
    source = EvidenceSource(source_id=source_id, status="unavailable", required=True,
                            detail=detail)
    public = source_diagnostic(source).model_dump_json()
    assert "PRIVATE" not in public
    assert "details_not_retained" in public


def test_actual_market_node_uses_bounded_presentation_without_losing_evidence_rules():
    prompts = []
    finalizer = MagicMock()
    llm = MagicMock()

    def respond(prompt):
        prompts.append(prompt.to_string())
        return AIMessage(content="synthetic truncated response",
                         response_metadata={"finish_reason": "length"})

    llm.bind_tools.return_value = RunnableLambda(respond)
    llm.with_structured_output.return_value = finalizer
    # 1,212 rows is the retained run04 snapshot size; values below are synthetic.
    table = "date,close\n" + "2026-10-16,1.0\n" * 1212
    result = create_market_analyst(llm)({
        "company_of_interest": "601328.SS", "trade_date": "2026-10-16",
        "asset_type": "stock", "messages": _tool_exchange(
            "get_stock_data", {"symbol": "601328.SS", "start_date": "2021-10-16",
                               "end_date": "2026-10-16"}, table, "synthetic-price"),
        "evidence_state": EvidenceState().model_dump(mode="json"),
    })
    prompt = prompts[0]
    assert "configured output budget" in prompt
    assert "every material data gap" in prompt
    assert "complete historical coverage" in prompt
    assert "very detailed and nuanced" not in prompt
    assert "get_verified_market_snapshot" in prompt
    assert "exact contiguous source_quote" in prompt
    assert table in prompt
    evidence = EvidenceState.model_validate(result["evidence_state"])
    submission = next(s for s in evidence.sources if s.source_id == "analyst.market.submission")
    assert submission.required and submission.status.value == "unavailable"
    assert submission.detail == "output_limit"
    finalizer.invoke.assert_not_called()


@pytest.mark.parametrize("phase", ["initial_response", "finalizer"])
def test_real_submission_to_cli_receipt_retains_safe_stage_and_provided_usage(tmp_path, phase):
    import cli.main as cli

    finalizer = MagicMock()
    if phase == "initial_response":
        response = AIMessage(content="PRIVATE_PARTIAL",
            response_metadata={"finish_reason": "length", "token_usage": {
                "completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 7600}},
                "private": "PRIVATE_METADATA"})
    else:
        response = AIMessage(content="Synthetic draft with an explicit data gap.")
        completion = ChatCompletion(id="synthetic", object="chat.completion", created=0,
            model="deepseek-flash", choices=[{"index": 0, "finish_reason": "length",
            "message": {"role": "assistant", "content": "PRIVATE_PARTIAL",
                        "reasoning_content": "PRIVATE_REASONING"}}],
            usage={"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292,
                   "completion_tokens_details": {"reasoning_tokens": 7600}})
        finalizer.invoke.side_effect = LengthFinishReasonError(completion=completion)
    state = {"company_of_interest": "601328.SS", "trade_date": "2026-10-16",
             "messages": [], "evidence_state": EvidenceState().model_dump(mode="json")}
    result = process_analyst_response(response, finalizer=finalizer, analyst="fundamentals",
        ticker=state["company_of_interest"], trade_date=state["trade_date"], messages=[])
    update = build_analyst_update(state, result, "fundamentals_report")
    metadata = result.message.response_metadata["analyst_submission_diagnostic"]
    assert metadata["stage"] == phase
    assert metadata["completion_tokens"] == 8192
    assert metadata["reasoning_tokens"] == 7600
    status = tmp_path / "run_status.json"
    status.write_text("{}", encoding="utf-8")
    cli._record_analyst_submission_observability(
        {"status_file": status}, update["evidence_state"], update["messages"])
    first = json.loads(status.read_text(encoding="utf-8"))["analyst_submissions"]["fundamentals"]
    assert first["reason_class"] == "output_limit"
    assert first["diagnostic"]["stage"] == phase
    assert first["diagnostic"]["completion_tokens"] == 8192
    assert first["diagnostic"]["reasoning_tokens"] == 7600
    # Later graph updates must not erase the already retained failure observation.
    cli._record_analyst_submission_observability({"status_file": status}, update["evidence_state"])
    assert json.loads(status.read_text(encoding="utf-8"))["analyst_submissions"]["fundamentals"] == first
    assert "PRIVATE" not in status.read_text(encoding="utf-8")
    assert "PRIVATE" not in result.message.model_dump_json()
    assert result.claims == () and result.message.tool_calls == []
    assert result.submission_source.required and result.submission_source.detail == "output_limit"
    assert finalizer.invoke.call_count == (1 if phase == "finalizer" else 0)


def test_absent_usage_stays_unknown_instead_of_inventing_the_configured_cap():
    result = process_analyst_response(AIMessage(content="synthetic",
        response_metadata={"finish_reason": "length"}), finalizer=MagicMock(),
        analyst="market", ticker="601328.SS", trade_date="2026-10-16", messages=[])
    diagnostic = result.message.response_metadata["analyst_submission_diagnostic"]
    assert diagnostic["stage"] == "initial_response"
    assert diagnostic["finish_reason"] == "length"
    assert "completion_tokens" not in diagnostic and "reasoning_tokens" not in diagnostic


@pytest.mark.parametrize("damage", ["private-stage", "wrong-analyst", "bool-attempts", "bad-counts"])
def test_cli_diagnostic_never_copies_unapproved_metadata(tmp_path, damage):
    import cli.main as cli

    item = {"version": 1, "analyst": "market", "reason_class": "output_limit",
            "stage": "initial_response", "attempts": 1, "finish_reason": "length",
            "completion_tokens": 8192, "reasoning_tokens": 7600, "private": "PRIVATE"}
    if damage == "private-stage":
        item["stage"] = "PRIVATE"
    elif damage == "wrong-analyst":
        item["analyst"] = "fundamentals"
    elif damage == "bool-attempts":
        item["attempts"] = True
    else:
        item.update(completion_tokens=True, reasoning_tokens=9000, finish_reason="PRIVATE")
    source = EvidenceSource(source_id="analyst.market.submission", status="unavailable",
                            required=True, detail="output_limit")
    path = tmp_path / "status.json"
    path.write_text("{}", encoding="utf-8")
    cli._record_analyst_submission_observability({"status_file": path},
        EvidenceState(sources=(source,)), [AIMessage(content="PRIVATE_BODY",
            response_metadata={"analyst_submission_diagnostic": item})])
    raw = path.read_text(encoding="utf-8")
    assert "PRIVATE" not in raw
    entry = json.loads(raw)["analyst_submissions"]["market"]
    if damage == "bad-counts":
        assert entry["diagnostic"] == {"stage": "initial_response", "attempts": 1}
    else:
        assert "diagnostic" not in entry


def _failed_submission_graph(tmp_path, monkeypatch):
    from langgraph.graph.state import CompiledStateGraph

    import cli.main as cli
    from tests.test_analysis_budget_e2e import FakeWire
    from tests.test_optional_source_e2e import (
        test_mainland_core_cli_graph_tools_checkpoint_and_report,
    )

    original = FakeWire.httpx
    original_stream = CompiledStateGraph.stream
    status = tmp_path / "run_status.json"
    status.write_text("{}", encoding="utf-8")
    observed_chunks = []

    def observe_stream(self, *args, **kwargs):
        for chunk in original_stream(self, *args, **kwargs):
            # Observe the actual graph updates before message-reset nodes erase
            # the prior analyst messages, exactly as the CLI stream loop does.
            payload = chunk[1] if isinstance(chunk, tuple) else chunk
            updates = [payload] if "evidence_state" in payload else payload.values()
            for update in updates:
                if isinstance(update, dict) and "evidence_state" in update:
                    cli._record_analyst_submission_observability({"status_file": status},
                        update["evidence_state"], update.get("messages", ()))
                    observed_chunks.append(update)
            yield chunk

    def model_wire(self, transport, request, module):
        response = original(self, transport, request, module)
        body = json.loads(request.content)
        prompt = "\n".join(str(m.get("content") or "") for m in body["messages"])
        if (body.get("response_format", {}).get("type") == "json_object"
                and any(f"Finalize the {a} analyst" in prompt for a in ("market", "fundamentals"))):
            payload = response.json()
            payload["choices"][0]["finish_reason"] = "length"
            payload["choices"][0]["message"]["content"] = '{"report_markdown":"PRIVATE_PARTIAL'
            payload["usage"].update(completion_tokens=8192, total_tokens=8202,
                completion_tokens_details={"reasoning_tokens": 7000})
            return module.Response(200, request=request, json=payload)
        return response

    monkeypatch.setattr(FakeWire, "httpx", model_wire)
    monkeypatch.setattr(CompiledStateGraph, "stream", observe_stream)
    # Real CLI configuration, Graph, tool dispatch, evidence gate, durable budget.
    # Only external data/model wire replies use the existing synthetic fixture.
    test_mainland_core_cli_graph_tools_checkpoint_and_report(tmp_path, monkeypatch, False)
    observed = json.loads((tmp_path / "pre-render-observation.json").read_text(encoding="utf-8"))
    state = observed["state"]
    assert len(observed_chunks) >= 2
    submissions = json.loads(status.read_text(encoding="utf-8"))["analyst_submissions"]
    for analyst in ("market", "fundamentals"):
        entry = submissions[analyst]
        assert entry["status"] == "unavailable" and entry["reason_class"] == "output_limit"
        assert entry["diagnostic"] == {"stage": "finalizer", "attempts": 1,
            "finish_reason": "length", "completion_tokens": 8192, "reasoning_tokens": 7000}
    assert state["terminal_outcome_kind"] == "analysis_outcome"
    assert observed["budget"]["stop_reason"] == "completed"
    assert "trading_decision" not in state or not state["trading_decision"]
    return state


def test_real_graph_to_receipt_and_report_projection_preserves_both_output_failures(tmp_path, monkeypatch):
    state = _failed_submission_graph(tmp_path, monkeypatch)
    # This is the exact diagnostic section appended by reporting._write_report_tree;
    # the existing fixture's outcome-body renderer does not include that section.
    text = evidence_diagnostic_markdown(EvidenceState.model_validate(state["evidence_state"]), "Chinese")
    assert "Market analyst submission" in text and "Fundamentals analyst submission" in text
    assert "analyst_output_limit" in text and "PRIVATE_PARTIAL" not in text


def test_report_publication_of_failed_graph(tmp_path, monkeypatch):
    """Separate filesystem publication check; never substitute rendering for this test."""
    from tradingagents.reporting import write_report_tree

    state = _failed_submission_graph(tmp_path, monkeypatch)
    complete = write_report_tree(state, "601328.SS", tmp_path / "export", output_language="Chinese")
    text = complete.read_text(encoding="utf-8")
    assert "Market analyst submission" in text and "Fundamentals analyst submission" in text
    assert "analyst_output_limit" in text and "PRIVATE_PARTIAL" not in text


@pytest.mark.parametrize("bad_stage", [False, True])
def test_old_diagnostic_is_resanitized_on_later_empty_message_update(tmp_path, bad_stage):
    import cli.main as cli

    old = {"stage": "PRIVATE_STAGE" if bad_stage else "finalizer", "attempts": 1,
           "completion_tokens": 8192, "reasoning_tokens": -1, "private": "PRIVATE"}
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"analyst_submissions": {"market": {
        "status": "unavailable", "reason_class": "output_limit", "diagnostic": old}}}),
        encoding="utf-8")
    source = EvidenceSource(source_id="analyst.market.submission", status="unavailable",
                            required=True, detail="output_limit")
    cli._record_analyst_submission_observability({"status_file": path}, EvidenceState(sources=(source,)))
    raw = path.read_text(encoding="utf-8")
    assert "PRIVATE" not in raw
    entry = json.loads(raw)["analyst_submissions"]["market"]
    if bad_stage:
        assert "diagnostic" not in entry
    else:
        assert entry["diagnostic"] == {"stage": "finalizer", "attempts": 1, "completion_tokens": 8192}

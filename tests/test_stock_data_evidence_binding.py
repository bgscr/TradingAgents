"""Stock history must carry provenance through the real Graph ToolNode.

Retained run05 proves the missing envelope; exact failed model statements were
not retained, so these are controlled claims, not a historical replay.
"""
import json
import re

import pytest
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from tests.test_market_tool_provenance import snapshot_provider as _snapshot_provider
from tradingagents.agents.analysts.submission import (
    AnalystSubmissionResult,
    build_analyst_update,
    source_ref_tool_call_ids,
)
from tradingagents.dataflows.market_snapshot import authoritative_snapshot_run
from tradingagents.evidence import EvidenceSource, EvidenceState, EvidenceStatus, MaterialClaim
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.fixture
def snapshot_provider(monkeypatch):
    return _snapshot_provider.__wrapped__(monkeypatch)


def stock_messages():
    call = AIMessage(content="", tool_calls=[{
        "name": "get_stock_data", "args": {"symbol": "601328.SS", "start_date": "2026-06-01",
                                               "end_date": "2026-10-16"},
        "id": "stock-call", "type": "tool_call",
    }])
    node = TradingAgentsGraph._create_tool_nodes(None)["market"]
    result = node.func({"messages": [call]}, config={}, runtime=Runtime())["messages"][0]
    return [call, result]


def evidence_for(messages, *, statement="The latest close is 11.0.", change=None):
    if change is not None:
        messages = [messages[0], change(messages[1])]
    refs = source_ref_tool_call_ids(messages, "601328.SS", "2026-10-16")
    quote = next(line for line in messages[1].content.splitlines() if line.startswith("2026-10-16,"))
    claim = MaterialClaim(claim_id="market.latest_close", analyst="market", statement=statement,
                          source_quote=quote, source_refs=(next(iter(refs)),))
    submission = AnalystSubmissionResult(
        message=AIMessage(content=statement), report=statement, claims=(claim,),
        submission_source=EvidenceSource(source_id="analyst.market.submission",
                                         status=EvidenceStatus.AVAILABLE, required=True, detail="direct_tool"),
    )
    return EvidenceState.model_validate(build_analyst_update({
        "company_of_interest": "601328.SS", "trade_date": "2026-10-16", "messages": messages,
        "evidence_state": {},
    }, submission, "market_report")["evidence_state"])


def test_stock_toolnode_reaches_actual_submission_validation(snapshot_provider):
    with authoritative_snapshot_run():
        messages = stock_messages()
        evidence = evidence_for(messages)
    validation = evidence.claim_validations[0]
    assert validation.status.value == "supported", validation.detail
    assert len(snapshot_provider) == 1
    assert evidence.source_facts[0].tool_name == "get_stock_data"
    artifact = next(a for a in evidence.source_artifacts if a.tool_call_id == "stock-call")
    assert artifact.raw_text == messages[1].content
    assert artifact.tool_name == "get_stock_data"
    assert all(f.fact_kind == "excerpt" for f in evidence.source_facts)


@pytest.mark.parametrize("damage", ["no_envelope", "wrong_call", "changed_content", "wrong_value"])
def test_bad_source_or_wrong_value_still_rejected(snapshot_provider, damage):
    with authoritative_snapshot_run():
        messages = stock_messages()
        if damage == "no_envelope":
            def change(m):
                return m.model_copy(update={"artifact": None})
        elif damage == "wrong_call":
            def change(m):
                return m.model_copy(update={"artifact": {**m.artifact, "tool_call_id": "other"}})
        elif damage == "changed_content":
            def change(m):
                return m.model_copy(update={"content": m.content + "\nchanged"})
        else:
            change = None
        # A bad/missing catalog can only fail earlier; never an available claim.
        try:
            evidence = evidence_for(messages, statement="The close is 999.0." if damage == "wrong_value"
                                    else "The latest close is 11.0.", change=change)
        except StopIteration:
            assert damage == "wrong_call"
            return
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("damage", ["missing_snapshot", "wrong_prefix", "wrong_snapshot", "wrong_frame"])
def test_stock_binding_requires_exact_owned_projection(snapshot_provider, monkeypatch, damage):
    from tradingagents.agents.utils import market_data_validation_tools as tools

    with authoritative_snapshot_run():
        messages = stock_messages()
        content = messages[1].content
        if damage == "missing_snapshot":
            monkeypatch.setattr(tools, "get_active_authoritative_market_snapshot", lambda *args: None)
        elif damage == "wrong_snapshot":
            content = re.sub(r"(?m)^# Snapshot ID: .*", "# Snapshot ID: snapshot:v2:" + "f" * 64, content)
        elif damage == "wrong_frame":
            content = re.sub(r"(?m)^# Frame SHA-256: .*", "# Frame SHA-256: " + "f" * 64, content)
        envelope = tools.market_projection_envelope(
            content, symbol="601328.SS", curr_date="2026-10-16", tool_name="get_stock_data",
            tool_call_id="stock-call", capability="get_stock_data",
            metadata_prefix="" if damage == "wrong_prefix" else "# ",
        )
    assert envelope["selected_artifact"] is None
    assert envelope["acquisition_outcomes"][0]["error_code"] == "snapshot_projection_binding_missing"


def test_legacy_string_api_and_nonmainland_behavior_are_preserved(snapshot_provider, monkeypatch):
    from tradingagents.agents.utils import core_stock_tools as tools

    with authoritative_snapshot_run():
        legacy = tools.get_stock_data.func("601328.SS", "2026-06-01", "2026-10-16")
        wrapped = stock_messages()[1]
    assert legacy == wrapped.content
    assert len(snapshot_provider) == 1
    calls = []
    def legacy_route(*args):
        calls.append(args)
        return "public nonmainland fixture"
    monkeypatch.setattr(tools, "route_to_vendor", legacy_route)
    text, artifact = tools.get_stock_data_with_evidence.func("AAPL", "2026-06-01", "2026-10-16", "us-call")
    assert text == "public nonmainland fixture" and artifact is None
    assert calls == [("get_stock_data", "AAPL", "2026-06-01", "2026-10-16")]


def test_cli_graph_finalizer_stock_quote_is_usable_without_promoting_prose(tmp_path, monkeypatch):
    from tests.test_analysis_budget_e2e import FakeWire
    from tests.test_optional_source_e2e import (
        test_mainland_core_cli_graph_tools_checkpoint_and_report,
    )

    original = FakeWire.httpx

    def model_wire(self, transport, request, module):
        response = original(self, transport, request, module)
        body = json.loads(request.content)
        text = "\n".join(str(m.get("content") or "") for m in body["messages"])
        if "Finalize the market analyst" in text and body.get("response_format", {}).get("type") == "json_object":
            quote = re.search(r"(?m)^2026-10-16,[^\r\n]+", text).group(0)
            refs = re.findall(r"(?m)^- ([^\s]+)", text)
            source_ref = next(ref for ref in refs if ref.startswith("snapshot:"))
            payload = response.json()
            payload["choices"][0]["message"]["content"] = json.dumps({
                "report_markdown": "Observed market row: " + quote,
                "material_claims": [{"claim_id": "market.stock_row", "statement": quote,
                                     "source_ref": source_ref, "source_quote": quote}],
            })
            return module.Response(200, request=request, json=payload)
        return response

    monkeypatch.setattr(FakeWire, "httpx", model_wire)
    test_mainland_core_cli_graph_tools_checkpoint_and_report(tmp_path, monkeypatch, False)
    observed = json.loads((tmp_path / "pre-render-observation.json").read_text(encoding="utf-8"))
    evidence = EvidenceState.model_validate(observed["state"]["evidence_state"])
    validation = next(v for v in evidence.claim_validations if v.claim_id == "market.stock_row")
    assert validation.status.value == "supported", validation.detail
    facts = [f for f in evidence.source_facts if f.fact_id in validation.fact_ids]
    assert {f.tool_name for f in facts} == {"get_stock_data"}
    assert all(f.fact_kind == "excerpt" for f in facts)
    # Qualification remains decided by the unchanged canonical policy, not this quote.
    assert not any(f.canonical_field for f in facts)

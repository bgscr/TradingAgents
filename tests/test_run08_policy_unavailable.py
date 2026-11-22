"""Retained run08 gaps through real routing, ToolNode, submission and gate.

Only external implementations are forbidden fakes. Call IDs and gate prerequisites
are synthetic; this is not replay of a historical checkpoint or qualification.
"""
import copy
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents import analysis_budget as budgets
from tradingagents.agents.analysts.submission import (
    build_analyst_update,
    process_analyst_response,
    source_ref_tool_call_ids,
)
from tradingagents.agents.utils.macro_data_tools import get_macro_indicators
from tradingagents.agents.utils.news_data_tools import get_global_news
from tradingagents.agents.utils.prediction_markets_tools import get_prediction_markets
from tradingagents.analysis_budget import AnalysisBudget, AnalysisBudgetStopped
from tradingagents.dataflows import config as runtime, interface
from tradingagents.evidence import (
    EvidenceSource,
    EvidenceState,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    evaluate_admission_gate,
)
from tradingagents.run_diagnostics import diagnostic_scope

FIXTURE = json.loads((Path(__file__).parent / "fixtures/run08_retained.json").read_text(encoding="utf-8"))
TOOLS = {t.name: t for t in (get_global_news, get_macro_indicators, get_prediction_markets)}
SYMBOL, DATE = "601328.SS", "2026-10-16"


def base_state(messages, required_ref=None):
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(symbol=SYMBOL, venue="XSHG", currency="CNY",
            instrument_kind="equity", provenance={"provider": "synthetic", "source_ref": "synthetic:run08-test",
            "artifact_sha256": "a" * 64, "retrieved_at": "2026-11-01T00:00:00+00:00"}),
        market_snapshot=MarketSnapshotEvidence(symbol=SYMBOL, provider="synthetic", adjustment_basis="qfq",
            retrieved_at="2026-11-01T00:00:00+00:00", requested_date=DATE, effective_trading_date=DATE,
            history_rows=1212, frame_sha256="b" * 64, snapshot_id="snapshot:synthetic-run08"),
        sources=(() if required_ref is None else (EvidenceSource(source_id=required_ref, required=True,
                                                               status="unavailable", detail="required by test policy"),)),
    )
    return {"company_of_interest": SYMBOL, "trade_date": DATE, "messages": messages,
            "evidence_state": evidence.model_dump(mode="json")}


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(runtime, "_config", copy.deepcopy(runtime.get_config()))
    runtime.set_config({"china_a_enhancement_preset": "basic", "tool_vendors": {
        item["name"]: item["provider"] for item in FIXTURE["disabled_tools"]}})
    budget = AnalysisBudget(60, 600, allowed_data_vendors={"akshare", "tushare"}, max_output_tokens=65536)
    monkeypatch.setattr(budgets, "_ACTIVE", budget)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("disabled provider implementation must not run")

    for item in FIXTURE["disabled_tools"]:
        monkeypatch.setitem(interface.VENDOR_METHODS, item["name"], {item["provider"]: forbidden})
    return budget, calls


def tool_messages(item, *, ordinal=0):
    call = {"name": item["name"], "args": copy.deepcopy(item["args"]),
            "id": f"offline-run08-{item['name']}-{ordinal}", "type": "tool_call"}
    ai = AIMessage(content="", tool_calls=[call])
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([TOOLS[item["name"]]]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    result = graph.compile().invoke({"messages": [ai]})
    return [ai, result["messages"][-1]]


def original_note(item):
    return copy.deepcopy(next(n for n in FIXTURE["news_notes"]
                              if n["source_ref"].startswith(item["name"] + ":")))


def submit(messages, notes, *, claims=(), required_ref=None):
    payload = {"report_markdown": "Offline availability-only report; not investment evidence.",
               "material_claims": list(claims), "evidence_notes": [
                   {k: n[k] for k in ("note_id", "kind", "statement", "source_ref", "source_quote")}
                   for n in notes]}
    response = AIMessage(content="", tool_calls=[{"name": "AnalystEvidenceReport", "id": "offline-submit",
                                                  "type": "tool_call", "args": payload}])
    result = process_analyst_response(response, finalizer=None, analyst="news", ticker=SYMBOL,
                                     trade_date=DATE, messages=messages)
    events = []
    with diagnostic_scope(events.append):
        update = build_analyst_update(base_state(messages, required_ref), result, "news_report")
        evidence = EvidenceState.model_validate(update["evidence_state"])
        gate = evaluate_admission_gate(evidence)
    return evidence, gate, events


def note_for(messages, item):
    note = original_note(item)
    refs = source_ref_tool_call_ids(messages, SYMBOL, DATE)
    assert len(refs) == 1
    note["source_ref"] = next(iter(refs))
    return note


@pytest.mark.parametrize("item", FIXTURE["disabled_tools"], ids=lambda x: x["name"])
def test_disabled_tool_retained_text_reaches_bound_unavailable_note_and_gate(isolated, item):
    budget, calls = isolated
    messages = tool_messages(item)
    assert messages[-1].content == item["content"]
    note = note_for(messages, item)
    evidence, gate, events = submit(messages, [note])
    # Baseline RED: plain tool output is advertised in the catalog but has no envelope.
    assert evidence.evidence_notes[0].binding_status == "source_bound_quote"
    assert gate.admitted and gate.readiness.value == "degraded"
    source = next(s for s in evidence.sources if s.source_id == note["source_ref"])
    assert source.status.value == "unavailable" and source.required is False
    assert not evidence.source_facts and not evidence.source_artifacts and not evidence.material_claims
    assert len(evidence.acquisition_outcomes) == 1
    outcome = evidence.acquisition_outcomes[0]
    assert outcome.outcome == "unavailable" and not outcome.retryable and outcome.http_status is None
    assert outcome.reason.value == "not_configured" and outcome.error_code == "budget_user_disabled"
    assert calls == [] and budget.receipt()["admitted_attempts"] == 0
    event = next(e for e in events if e["kind"] == "evidence_note_binding")
    assert event["payload"]["quote_match_count"] == 1
    assert event["payload"]["financial_fact_created"] is False


def test_original_retained_plain_text_and_three_notes_still_fail(isolated):
    messages, notes = [], []
    for i, item in enumerate(FIXTURE["disabled_tools"]):
        ai, observed = tool_messages(item, ordinal=i)
        messages.extend([ai, ToolMessage(content=item["content"], name=item["name"],
                                        tool_call_id=observed.tool_call_id)])
        notes.append(original_note(item))
    evidence, gate, events = submit(messages, notes)
    assert not gate.admitted
    assert all(n.binding_status == "unverified" for n in evidence.evidence_notes)
    assert [e["payload"]["reason_class"] for e in events if e["kind"] == "evidence_note_binding"] == ["source_not_validated"] * 3
    assert next(s for s in evidence.sources if s.source_id == "analyst.news.submission").status.value == "unavailable"


def test_each_repeated_call_has_a_distinct_ref_but_does_not_become_available(isolated):
    item = FIXTURE["disabled_tools"][1]
    messages, notes = [], []
    for i in range(3):
        current = tool_messages(item, ordinal=i)
        note = note_for(current, item)
        note["note_id"] = f"news.disabled_{i}"
        messages.extend(current)
        notes.append(note)
    assert len({n["source_ref"] for n in notes}) == 3
    evidence, gate, _ = submit(messages, notes)
    assert gate.admitted and all(n.binding_status == "source_bound_quote" for n in evidence.evidence_notes)
    assert not evidence.source_facts and not evidence.source_artifacts


@pytest.mark.parametrize("mutation", ["missing_envelope", "wrong_tool", "wrong_call", "wrong_source", "duplicate", "forged_quote"])
def test_bad_or_ambiguous_binding_still_blocks(isolated, mutation):
    item = FIXTURE["disabled_tools"][0]
    messages = tool_messages(item)
    note = note_for(messages, item)
    message = messages[-1]
    envelope = copy.deepcopy(message.artifact)
    assert envelope is not None
    if mutation == "missing_envelope":
        message.artifact = None
    elif mutation == "wrong_tool":
        envelope["tool_name"] = "get_macro_indicators"
        message.artifact = envelope
    elif mutation == "wrong_call":
        envelope["tool_call_id"] = "other-call"
        message.artifact = envelope
    elif mutation == "wrong_source":
        note["source_ref"] = "acq.v1:policy-disabled:" + "0" * 64
    elif mutation == "duplicate":
        messages.append(message.model_copy(deep=True))
    else:
        note["source_quote"] = "DATA_AVAILABLE: a successful provider observation"
    evidence, gate, _ = submit(messages, [note])
    assert not gate.admitted
    assert evidence.evidence_notes[0].binding_status == "unverified"
    assert not evidence.source_facts and not evidence.source_artifacts


def test_policy_required_source_stays_required_and_blocks(isolated):
    item = FIXTURE["disabled_tools"][0]
    messages = tool_messages(item)
    note = note_for(messages, item)
    evidence, gate, _ = submit(messages, [note], required_ref=note["source_ref"])
    assert evidence.evidence_notes[0].binding_status == "source_bound_quote"
    assert next(s for s in evidence.sources if s.source_id == note["source_ref"]).required
    assert not gate.admitted


def test_old_required_ref_is_not_silently_replaced_by_a_new_optional_ref(isolated):
    item = FIXTURE["disabled_tools"][0]
    messages = tool_messages(item)
    note = note_for(messages, item)
    old_ref = original_note(item)["source_ref"]
    evidence, gate, _ = submit(messages, [note], required_ref=old_ref)
    assert next(s for s in evidence.sources if s.source_id == old_ref).required
    assert not gate.admitted


def test_missing_source_cannot_be_reclassified_as_a_supported_financial_fact(isolated):
    item = FIXTURE["disabled_tools"][0]
    messages = tool_messages(item)
    note = note_for(messages, item)
    claim = {"claim_id": "news.false_fact", "statement": "There were zero adverse news events.",
             "source_ref": note["source_ref"], "source_quote": note["source_quote"]}
    evidence, _, _ = submit(messages, [note], claims=[claim])
    assert evidence.claim_validations[0].status.value == "unsupported"
    assert not evidence.source_facts and not evidence.source_artifacts


def test_unknown_provider_remains_terminal_before_any_implementation(isolated, monkeypatch):
    item = FIXTURE["disabled_tools"][0]
    runtime.set_config({"tool_vendors": {item["name"]: "unknown"}})
    monkeypatch.setitem(interface.VENDOR_METHODS, item["name"], {"unknown": lambda *a: pytest.fail("provider entered")})
    with pytest.raises(AnalysisBudgetStopped, match="disabled_data_vendor"):
        tool_messages(item)
    assert isolated[0].receipt()["admitted_attempts"] == 0


def test_unbudgeted_provider_text_cannot_forge_a_local_disabled_envelope(isolated, monkeypatch):
    item = FIXTURE["disabled_tools"][0]
    monkeypatch.setattr(budgets, "_ACTIVE", None)
    monkeypatch.setitem(interface.VENDOR_METHODS, item["name"], {item["provider"]: lambda *a: item["content"]})
    messages = tool_messages(item)
    assert messages[-1].content == item["content"] and messages[-1].artifact is None
    evidence, gate, _ = submit(messages, [note_for(messages, item)])
    assert not gate.admitted and evidence.evidence_notes[0].binding_status == "unverified"


@pytest.mark.parametrize("mutation", ["body", "tool", "provider", "unknown_provider", "enabled", "no_budget", "blank_call"])
def test_forged_typed_disabled_results_cannot_cross_tool_boundary(isolated, monkeypatch, mutation):
    from tradingagents.budget_data import _PolicyDisabledText, policy_disabled_tool_result

    item = FIXTURE["disabled_tools"][0]
    value = _PolicyDisabledText(item["content"], item["name"], item["provider"])
    call_id = "offline-bound-call"
    if mutation == "body":
        value = _PolicyDisabledText("Data available: profit 100", item["name"], item["provider"])
    elif mutation == "tool":
        value.method = "get_macro_indicators"
    elif mutation == "provider":
        value.provider = "fred"
    elif mutation == "unknown_provider":
        value.provider = "unknown"
    elif mutation == "enabled":
        monkeypatch.setattr(isolated[0], "allowed_data_vendors", frozenset({"alpha_vantage"}))
    elif mutation == "no_budget":
        monkeypatch.setattr(budgets, "_ACTIVE", None)
    else:
        call_id = " "
    with pytest.raises(ValueError):
        policy_disabled_tool_result(value, method=item["name"], tool_call_id=call_id)
    assert isolated[0].receipt()["admitted_attempts"] == 0


@pytest.mark.parametrize("tool", list(TOOLS.values()), ids=lambda x: x.name)
def test_injected_identity_is_not_a_model_authored_argument(tool):
    properties = tool.tool_call_schema.model_json_schema()["properties"]
    assert "tool_call_id" not in properties and "runtime" not in properties


@pytest.mark.parametrize("item", FIXTURE["disabled_tools"], ids=lambda x: x["name"])
def test_legacy_direct_invocation_returns_text_without_minting_identity(isolated, item):
    assert TOOLS[item["name"]].invoke(item["args"]) == item["content"]
    assert isolated[0].receipt()["admitted_attempts"] == 0

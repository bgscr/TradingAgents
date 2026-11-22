"""Synthetic auxiliary-note routing through the real analyst submission seam."""
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from tests.test_multiyear_trend import ASOF, SYMBOL, synthetic_evidence
from tradingagents.agents.analysts.submission import build_analyst_update, process_analyst_response
from tradingagents.evidence import AnalystEvidenceReport, EvidenceState, evaluate_admission_gate
from tradingagents.multiyear_trend import multiyear_trend_markdown


def submit(monkeypatch, *, language="Chinese", mutation=None, analyst="market", mode="direct"):
    ev = synthetic_evidence()
    text = multiyear_trend_markdown(ev, symbol=SYMBOL, as_of=ASOF, language=language)
    prefixes = ("- 价格口径:", "- 覆盖状态:", "- 时间证据:") if language == "Chinese" else (
        "- Price basis:", "- Coverage:", "- Temporal evidence:")
    quotes = [line for line in text.splitlines() if line.startswith(prefixes)]
    assert len(quotes) == 3
    notes = [{"note_id": f"{analyst}.limit_{i}", "kind": "availability" if i == 1 else "applicability",
                  "statement": "Model paraphrase is not verified; SYNTHETIC_UNTRUSTED_TEXT",
                  "source_ref": ev.market_snapshot.snapshot_id, "source_quote": quote}
             for i, quote in enumerate(quotes)]
    state = {"company_of_interest": SYMBOL, "trade_date": ASOF, "messages": [],
                 "evidence_state": ev.model_dump(mode="json")}
    if mutation == "wrong-stock":
        state["company_of_interest"] = "600000.SS"
    elif mutation == "wrong-asof":
        state["trade_date"] = "2026-11-12"
    elif mutation == "bad-hash":
        state["evidence_state"]["source_artifacts"][0]["raw_text"] += "tampered"
    elif mutation == "missing-source":
        state["evidence_state"]["acquisition_outcomes"] = []
    elif mutation == "wrong-ref":
        for note in notes:
            note["source_ref"] = "snapshot:v2:" + "a" * 64
    elif mutation == "unknown-quote":
        for note in notes:
            note["source_quote"] += " Future profit is guaranteed."
    elif mutation == "partial-quote":
        for note in notes:
            note["source_quote"] = note["source_quote"][4:]
    elif mutation == "numeric-row":
        notes[0]["source_quote"] = next(line for line in text.splitlines() if "100.00%" in line)
    elif mutation == "identity-conflict":
        state["evidence_state"]["instrument_identity"]["symbol"] = "600000.SS"
    elif mutation == "wrong-kind":
        for note in notes:
            note["kind"] = "provenance"
    elif mutation == "temporal-availability":
        notes[2]["kind"] = "availability"
    elif mutation == "price-availability":
        notes[0]["kind"] = "availability"
    elif mutation == "claim-id-conflict":
        state["evidence_state"]["material_claims"] = [{
            "claim_id": notes[0]["note_id"], "analyst": "market", "statement": "Prior synthetic claim",
            "source_refs": [ev.market_snapshot.snapshot_id], "source_quote": "old observation"}]
    payload = {"report_markdown": "Synthetic commentary / 合成正文", "material_claims": [], "evidence_notes": notes}
    if mutation == "material-claim":
        payload["material_claims"] = [{"claim_id": "market.claim", "statement": "Invented profit 999",
                                         "source_ref": notes[0]["source_ref"], "source_quote": notes[0]["source_quote"]}]
    response = AIMessage(content="", tool_calls=[{"name": "AnalystEvidenceReport", "args": payload,
                                                      "id": "synthetic-report", "type": "tool_call"}])
    finalizer = None
    if mode == "finalizer":
        response = AIMessage(content="Synthetic draft")
        finalizer = MagicMock()
        finalizer.invoke.return_value = AnalystEvidenceReport(**payload)
    result = process_analyst_response(response, finalizer=finalizer, analyst=analyst,
                                     ticker=state["company_of_interest"], trade_date=state["trade_date"], messages=[])
    active = None
    if mutation == "active-conflict":
        active = SimpleNamespace(snapshot_id="snapshot:v2:" + "f" * 64)
    monkeypatch.setattr("tradingagents.agents.analysts.submission.get_active_authoritative_market_snapshot",
                        lambda *args: active)
    from tradingagents.dataflows import config
    monkeypatch.setitem(config._config, "output_language", language)
    before = copy.deepcopy(state)
    update = build_analyst_update(state, result, f"{analyst}_report")
    assert state == before
    return ev, notes, update, EvidenceState.model_validate(update["evidence_state"])


@pytest.mark.parametrize("language", ["Chinese", "English"])
@pytest.mark.parametrize("mode", ["direct", "finalizer"])
def test_bound_deterministic_limitations_return_to_advisory_not_evidence(monkeypatch, language, mode):
    ev, notes, update, current = submit(monkeypatch, language=language, mode=mode)
    submission = next(s for s in current.sources if s.source_id == "analyst.market.submission")
    assert submission.status.value == "available"
    assert current.evidence_notes == current.material_claims == current.source_facts == ()
    assert current.market_snapshot == ev.market_snapshot
    assert current.source_artifacts == ev.source_artifacts
    assert current.acquisition_outcomes == ev.acquisition_outcomes
    report = update["market_report"]
    assert "SYNTHETIC_UNTRUSTED_TEXT" not in report
    assert ev.market_snapshot.snapshot_id in report and ev.market_snapshot.frame_sha256 in report
    assert ASOF in report and SYMBOL in report
    assert ("辅助说明" if language == "Chinese" else "advisory limitations") in report
    for note in notes:
        assert note["source_quote"] in report
    assert update["messages"][0].content == report


@pytest.mark.parametrize("mutation", ["wrong-stock", "wrong-asof", "bad-hash", "missing-source",
                                     "wrong-ref", "unknown-quote", "partial-quote", "numeric-row",
                                     "identity-conflict", "wrong-kind", "claim-id-conflict"])
def test_unverified_notes_keep_existing_gate(monkeypatch, mutation):
    if mutation == "bad-hash":
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="artifact_sha256"):
            submit(monkeypatch, mutation=mutation)
        return
    _, _, _, current = submit(monkeypatch, mutation=mutation)
    submission = next(s for s in current.sources if s.source_id == "analyst.market.submission")
    assert submission.status.value in {"unavailable", "conflicted"}
    assert not evaluate_admission_gate(current).admitted
    if mutation != "claim-id-conflict":
        assert any(note.binding_status == "unverified" for note in current.evidence_notes)
    assert current.source_facts == ()


def test_other_analyst_does_not_gain_market_auxiliary_exception(monkeypatch):
    _, _, _, current = submit(monkeypatch, analyst="fundamentals")
    assert next(s for s in current.sources if s.source_id == "analyst.fundamentals.submission").status.value == "unavailable"
    assert not evaluate_admission_gate(current).admitted


def test_auxiliary_quote_cannot_launder_material_claim(monkeypatch):
    _, _, _, current = submit(monkeypatch, mutation="material-claim")
    assert len(current.material_claims) == 1
    assert current.claim_validations[0].status.value == "unsupported"
    assert current.source_facts == ()


def test_active_snapshot_mismatch_does_not_demote_old_notes(monkeypatch):
    from tradingagents.agents.analysts.submission import (
        AnalystSubmissionResult,
        _route_multiyear_advisory_notes,
    )
    from tradingagents.evidence import EvidenceNote, EvidenceSource
    ev = synthetic_evidence()
    quote = next(line for line in multiyear_trend_markdown(ev, symbol=SYMBOL, as_of=ASOF).splitlines()
                 if line.startswith("- Coverage:"))
    result = AnalystSubmissionResult(message=AIMessage(content="Synthetic"), report="Synthetic",
        submission_source=EvidenceSource(source_id="analyst.market.submission", status="available", required=True),
        notes=(EvidenceNote(note_id="market.limit", analyst="market", kind="availability", statement="Synthetic",
                            source_ref=ev.market_snapshot.snapshot_id, source_quote=quote),))
    for active in [SimpleNamespace(snapshot_id="snapshot:v2:" + "f" * 64),
                   SimpleNamespace(snapshot_id=ev.market_snapshot.snapshot_id, frame_sha256="f" * 64),
                   SimpleNamespace(snapshot_id=ev.market_snapshot.snapshot_id,
                                   frame_sha256=ev.market_snapshot.frame_sha256, requested_date="2026-11-12")]:
        assert _route_multiyear_advisory_notes({"company_of_interest": SYMBOL, "trade_date": ASOF},
                                              result, ev, active, "market_report") is result


@pytest.mark.parametrize("language", ["Chinese", "English"])
@pytest.mark.parametrize("mode", ["direct", "finalizer"])
def test_exact_temporal_limitation_availability_label_is_advisory_only(monkeypatch, language, mode):
    ev, notes, update, current = submit(monkeypatch, language=language, mode=mode,
                                       mutation="temporal-availability")
    assert next(s for s in current.sources if s.source_id == "analyst.market.submission").status.value == "available"
    assert current.evidence_notes == current.material_claims == current.source_facts == ()
    assert current.market_snapshot == ev.market_snapshot
    assert current.source_artifacts == ev.source_artifacts
    assert notes[2]["source_quote"] in update["market_report"]
    assert "SYNTHETIC_UNTRUSTED_TEXT" not in update["market_report"]


def test_temporal_label_compatibility_does_not_apply_to_other_advisory_lines(monkeypatch):
    _, _, _, current = submit(monkeypatch, mutation="price-availability")
    assert next(s for s in current.sources if s.source_id == "analyst.market.submission").status.value == "unavailable"
    assert not evaluate_admission_gate(current).admitted

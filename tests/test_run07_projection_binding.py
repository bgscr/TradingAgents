"""Portable synthetic projections for bounded evidence checks."""

import copy
import json
import os
from hashlib import sha256
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.synthetic_projection_data import market_projection_case
from tradingagents.agents.analysts.submission import (
    ANALYST_OUTPUT_GUIDANCE,
    _finalization_prompt,
    build_analyst_update,
    process_analyst_response,
)
from tradingagents.evidence import (
    EvidenceState,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    SourceAcquisitionAvailable,
    SourceArtifact,
    ToolExecutionEvidenceEnvelope,
    evaluate_admission_gate,
)

FIXTURE = market_projection_case()
SYMBOL, DATE = "601328.SS", "2026-10-16"
REF = FIXTURE["claims"][0]["source_ref"]
OHLC = "market.601328.ohlcv.20261016"


def inputs(*, tools=None, identity=True):
    tools = copy.deepcopy(FIXTURE["tools"] if tools is None else tools)
    messages = []
    for index, tool in enumerate(tools):
        name, content = tool["name"], tool["content"]
        call = {
            "name": name,
            "id": f"offline-run07-{index}",
            "type": "tool_call",
            "args": tool["args"],
        }
        artifact = SourceArtifact(
            artifact_sha256=sha256(content.encode()).hexdigest(),
            source_ref=REF,
            tool_call_id=call["id"],
            tool_name=name,
            raw_text=content,
        )
        capability = "market_snapshot" if name == "get_verified_market_snapshot" else name
        acquired = SourceAcquisitionAvailable(
            provider="offline-projection-test",
            capability=capability,
            source_ref=REF,
            attempt=1,
            artifact=artifact,
            acquisition_id=f"offline-run07-acquisition-{index}",
            retrieved_at="2026-11-01T05:03:31.957953+00:00",
        )
        envelope = ToolExecutionEvidenceEnvelope(
            tool_call_id=call["id"],
            tool_name=name,
            source_ref=REF,
            capability=capability,
            acquisition_outcomes=(acquired,),
            selected_artifact=artifact,
        )
        messages.extend(
            [
                AIMessage(content="", tool_calls=[call]),
                ToolMessage(
                    name=name,
                    tool_call_id=call["id"],
                    content=content,
                    artifact=envelope.model_dump(mode="json"),
                ),
            ]
        )
    identity_value = (
        InstrumentIdentityEvidence(
            symbol=SYMBOL,
            venue="XSHG",
            instrument_kind="equity",
            currency="CNY",
            provenance={
                "provider": "synthetic",
                "source_ref": "synthetic:run07-offline-identity",
                "artifact_sha256": "a" * 64,
                "retrieved_at": "2026-11-01T00:00:00+00:00",
            },
        )
        if identity
        else None
    )
    evidence = EvidenceState(
        instrument_identity=identity_value,
        market_snapshot=MarketSnapshotEvidence(
            symbol=SYMBOL,
            provider="synthetic",
            retrieved_at="2026-11-01T00:00:00+00:00",
            adjustment_basis="qfq",
            requested_date=DATE,
            effective_trading_date=DATE,
            history_rows=1212,
            frame_sha256="b" * 64,
            snapshot_id=REF,
        ),
    )
    return {
        "company_of_interest": SYMBOL,
        "trade_date": DATE,
        "messages": messages,
        "evidence_state": evidence.model_dump(mode="json"),
    }


def submit(*, unique_notes=False, tools=None, identity=True, claims=None, notes=None):
    current = inputs(tools=tools, identity=identity)
    claims = copy.deepcopy(FIXTURE["claims"] if claims is None else claims)
    notes = copy.deepcopy(FIXTURE["notes"] if notes is None else notes)
    if unique_notes:
        for note in notes:
            if note["note_id"] in {"market.n2", "market.n3"}:
                note["source_quote"] = "- " + note["source_quote"]
    payload = {
        "report_markdown": "Offline test output; no decision qualification.",
        "material_claims": [
            {k: c[k] for k in ("claim_id", "statement", "source_ref", "source_quote")}
            for c in claims
        ],
        "evidence_notes": [
            {k: n[k] for k in ("note_id", "kind", "statement", "source_ref", "source_quote")}
            for n in notes
        ],
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "id": "offline-submission",
                "type": "tool_call",
                "args": payload,
            }
        ],
    )
    result = process_analyst_response(
        response,
        finalizer=None,
        analyst="market",
        ticker=SYMBOL,
        trade_date=DATE,
        messages=current["messages"],
    )
    update = build_analyst_update(current, result, "market_report")
    evidence = EvidenceState.model_validate(update["evidence_state"])
    return evidence, evaluate_admission_gate(evidence)


def status(evidence, claim=OHLC):
    return next(v.status.value for v in evidence.claim_validations if v.claim_id == claim)


def test_synthetic_quote_counts_require_full_line_not_arbitrary_projection_selection():
    for text in ("Provider: tushare", "Adjustment basis: qfq"):
        assert [t["content"].count(text) for t in FIXTURE["tools"]] == [1, 1, 11]
        assert [t["content"].count("- " + text) for t in FIXTURE["tools"]] == [0, 1, 0]


def test_original_short_notes_still_fail_closed_after_identity_repair():
    evidence, gate = submit()
    assert not gate.admitted
    assert [c.value for c in gate.diagnostic_codes] == ["required_evidence_unavailable"]
    assert [
        s.source_id for s in evidence.sources if s.required and s.status.value == "unavailable"
    ] == ["analyst.market.submission"]
    assert [n.note_id for n in evidence.evidence_notes if n.binding_status == "unverified"] == [
        "market.n2",
        "market.n3",
    ]


def test_unique_notes_close_only_the_note_blocker_and_keep_exact_binding():
    evidence, gate = submit(unique_notes=True)
    assert (
        gate.admitted
    )  # Narrow admission gate with synthetic identity/snapshot, not a Trading Decision.
    assert all(n.binding_status == "source_bound_quote" for n in evidence.evidence_notes)
    snapshot = FIXTURE["tools"][1]["content"]
    for note in evidence.evidence_notes:
        assert note.decision_qualified is False
        if note.note_id in {"market.n2", "market.n3"}:
            assert note.tool_call_id == "offline-run07-1"
            assert note.source_content_sha256 == sha256(snapshot.encode()).hexdigest()
            assert snapshot[note.quote_start : note.quote_end] == note.source_quote
    assert all(
        f.fact_kind == "excerpt" and f.financial_observation is None for f in evidence.source_facts
    )


def test_unique_ohlcv_quote_binds_identity_with_three_companion_projections():
    evidence, _ = submit(unique_notes=True)
    assert status(evidence) == "supported"
    assert len(evidence.claim_validations) == 8
    assert all(v.status.value == "supported" for v in evidence.claim_validations)


@pytest.mark.parametrize("indices", [(0,), (0, 1), (0, 2), (2, 1, 0)])
def test_identifier_binding_does_not_depend_on_companion_count_or_order(indices):
    tools = [FIXTURE["tools"][i] for i in indices]
    claims = [c for c in FIXTURE["claims"] if c["claim_id"] == OHLC]
    evidence, _ = submit(tools=tools, claims=claims, notes=[])
    assert status(evidence) == "supported"


@pytest.mark.parametrize("tool_name", ["get_verified_market_snapshot", "get_indicators"])
def test_excluded_wrong_identity_companion_cannot_turn_into_single_source_success(tool_name):
    tools = copy.deepcopy(
        [t for t in FIXTURE["tools"] if t["name"] in {"get_stock_data", tool_name}]
    )
    tools[1]["args"]["symbol"] = "600000.SS"
    claims = [c for c in FIXTURE["claims"] if c["claim_id"] == OHLC]
    evidence, _ = submit(tools=tools, claims=claims, notes=[])
    assert status(evidence) == "unsupported"


@pytest.mark.parametrize(
    "index,old,new",
    [
        (1, "| Close | 8.09 |", "| Close | 9.99 |"),
        (1, "- Latest trading row used: 2026-10-16", "- Latest trading row used: 2026-10-15"),
        (1, "snapshot for 601328.SS", "snapshot for 600000.SS"),
        (1, "- Requested analysis date: 2026-10-16", "- Requested analysis date: 2026-10-15"),
        (2, "Effective trading date: 2026-10-16", "Effective trading date: 2026-10-15"),
        (2, "Adjustment basis: qfq", "Adjustment basis: raw"),
        (2, "Provider: tushare", "Provider: other"),
    ],
)
def test_distinct_same_snapshot_projections_cannot_disagree(index, old, new):
    tools = copy.deepcopy(FIXTURE["tools"])
    assert old in tools[index]["content"]
    tools[index]["content"] = tools[index]["content"].replace(old, new, 1)
    claims = [c for c in FIXTURE["claims"] if c["claim_id"] == OHLC]
    evidence, _ = submit(tools=tools, claims=claims, notes=[])
    assert status(evidence) == "unsupported"


def test_malformed_scientific_value_fails_closed_without_decimal_exception():
    tools = copy.deepcopy(FIXTURE["tools"])
    claims = [copy.deepcopy(c) for c in FIXTURE["claims"] if c["claim_id"] == OHLC]
    old = claims[0]["source_quote"]
    new = old.replace("8.09", "1e999999999999999999999999999999")
    tools[0]["content"] = tools[0]["content"].replace(old, new)
    claims[0]["source_quote"] = new
    evidence, _ = submit(tools=tools, claims=claims, notes=[])
    assert status(evidence) == "unsupported"


def test_guidance_explains_source_wide_unique_quote_without_rewriting_or_omitting_notes():
    current = inputs()
    prompt = _finalization_prompt(
        analyst="market",
        ticker=SYMBOL,
        trade_date=DATE,
        draft="retained public draft",
        messages=current["messages"],
    )
    assert "exactly once across all tool outputs sharing that source_ref" in ANALYST_OUTPUT_GUIDANCE
    assert "preserve leading punctuation" in ANALYST_OUTPUT_GUIDANCE
    assert "do not invent a locator or discard a material limitation" in ANALYST_OUTPUT_GUIDANCE
    assert ANALYST_OUTPUT_GUIDANCE in prompt


@pytest.mark.parametrize(
    "change",
    [
        "wrong_symbol",
        "wrong_venue",
        "missing_identity",
        "wrong_call",
        "companion_wrong_call",
        "duplicate_projection",
        "conflicting_projection",
        "duplicate_quote_other_tool",
        "wrong_text_symbol",
        "duplicate_row_date",
        "wrong_date_header",
        "wrong_source_header",
        "missing_csv_header",
        "partial_quote",
        "cross_tool_quote",
        "wrong_date",
        "reversed_date",
        "wrong_price",
        "identifier_as_amount",
        "identifier_as_percent",
    ],
)
def test_identity_is_bound_to_one_exact_stock_row_and_compatible_projections(change):
    tools = copy.deepcopy(FIXTURE["tools"])
    claims = [copy.deepcopy(next(c for c in FIXTURE["claims"] if c["claim_id"] == OHLC))]
    c = claims[0]
    identity = change != "missing_identity"
    if change == "wrong_symbol":
        c["statement"] = c["statement"].replace(SYMBOL, "600000.SS")
    if change == "wrong_venue":
        c["statement"] = c["statement"].replace(SYMBOL, "601328.SZ")
    if change == "wrong_call":
        tools[0]["args"]["symbol"] = "600000.SS"
    if change == "companion_wrong_call":
        tools[1]["args"]["symbol"] = "600000.SS"
    if change in {"duplicate_projection", "conflicting_projection"}:
        duplicate = copy.deepcopy(tools[0])
        if change == "conflicting_projection":
            duplicate["content"] = duplicate["content"].replace(
                c["source_quote"], "2026-10-16,8.1,8.2,8.03,9.99,289163320.0"
            )
        tools.append(duplicate)
    if change == "duplicate_quote_other_tool":
        tools[2]["content"] += "\n" + c["source_quote"]
    if change == "wrong_text_symbol":
        tools[0]["content"] = tools[0]["content"].replace(SYMBOL, "600000.SS")
    if change == "duplicate_row_date":
        tools[0]["content"] += "\n2026-10-16,8.1,8.2,8.03,9.99,289163320.0\n"
    if change == "wrong_date_header":
        tools[0]["content"] = tools[0]["content"].replace(
            "# Requested date: " + DATE, "# Requested date: 2026-10-15"
        )
    if change == "wrong_source_header":
        tools[0]["content"] = tools[0]["content"].replace(REF, "snapshot:other")
    if change == "missing_csv_header":
        tools[0]["content"] = tools[0]["content"].replace("Date,Open,High,Low,Close,Volume", "")
    if change == "partial_quote":
        c["source_quote"] = "8.1,8.2,8.03,8.09,289163320.0"
    if change == "cross_tool_quote":
        c["source_quote"] += "\n| close_10_ema | 7.90 |"
    if change == "wrong_date":
        c["statement"] = c["statement"].replace(DATE, "2026-10-15")
    if change == "reversed_date":
        c["statement"] = c["statement"].replace(DATE, "2026-16-10")
    if change == "wrong_price":
        c["statement"] = c["statement"].replace("closed at 8.09", "closed at 8.30")
    if change == "identifier_as_amount":
        c["statement"] += " The amount was 601328."
    if change == "identifier_as_percent":
        c["statement"] += " The return was 601328%."
    evidence, _ = submit(tools=tools, claims=claims, notes=[], identity=identity)
    assert status(evidence) == "unsupported"


@pytest.mark.parametrize("change", ["wrong_ref", "wrong_quote", "duplicate", "cross_tool"])
def test_note_guidance_does_not_change_unique_binding_rejection(change):
    tools = copy.deepcopy(FIXTURE["tools"])
    notes = [copy.deepcopy(FIXTURE["notes"][1])]
    notes[0]["source_quote"] = "- Provider: tushare"
    if change == "wrong_ref":
        notes[0]["source_ref"] = "snapshot:other"
    if change == "wrong_quote":
        notes[0]["source_quote"] = "- Provider: other"
    if change == "duplicate":
        tools[2]["content"] += "\n- Provider: tushare"
    if change == "cross_tool":
        stock, snapshot, _ = tools
        (count_line,) = [line for line in stock["content"].splitlines() if line.startswith("# Total records:")]
        assert count_line not in snapshot["content"]
        assert notes[0]["source_quote"] in snapshot["content"]
        assert notes[0]["source_quote"] not in stock["content"]
        notes[0]["source_quote"] += "\n" + count_line
    evidence, gate = submit(tools=tools, notes=notes)
    assert evidence.evidence_notes[0].binding_status == "unverified"
    assert not gate.admitted


def test_bound_quote_never_qualifies_note_paraphrase_or_rsi_rule():
    evidence, _ = submit(unique_notes=True)
    assert all(n.semantic_status == "not_established" for n in evidence.evidence_notes)
    claims = [copy.deepcopy(next(c for c in FIXTURE["claims"] if "rsi" in c["claim_id"].lower()))]
    claims[0]["statement"] = "RSI 74.58 is above the rule threshold 70."
    negative, _ = submit(claims=claims, notes=[])
    assert negative.claim_validations[0].status.value == "unsupported"


def test_unbound_identity_cannot_be_supplied_by_tool_text():
    evidence, gate = submit(unique_notes=True, identity=False)
    assert status(evidence) == "unsupported"
    assert not gate.admitted and gate.diagnostic_codes[0].value == "identity_unavailable"


def test_record_controlled_comparison_matrix(tmp_path):
    observations = []
    for unique_notes in (False, True):
        evidence, gate = submit(unique_notes=unique_notes)
        assert gate.admitted is unique_notes
        observations.append(
            {
                "unique_note_quotes": unique_notes,
                "ohlcv_validation": status(evidence),
                "supported_claims": sum(
                    v.status.value == "supported" for v in evidence.claim_validations
                ),
                "note_bindings": {n.note_id: n.binding_status for n in evidence.evidence_notes},
                "gate_admitted": gate.admitted,
                "decision_qualified": False,
            }
        )
    path = Path(os.environ.get("BOUNDARY_TEST_OUTPUT", str(tmp_path))) / "controlled_comparison.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(
            {
                "historical_replay": False,
                "synthetic_identity_and_envelopes": True,
                "observations": observations,
            },
            handle,
            indent=2,
        )



def test_matrix_receipt_does_not_require_local_driver_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("BOUNDARY_TEST_OUTPUT", raising=False)
    test_record_controlled_comparison_matrix(tmp_path)
    saved = json.loads((tmp_path / "controlled_comparison.json").read_text())
    assert len(saved["observations"]) == 2
    assert saved["observations"][1]["ohlcv_validation"] == "supported"

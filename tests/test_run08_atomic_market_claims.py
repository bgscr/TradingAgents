"""Synthetic bundled statements versus explicitly edited atomic controls; no model."""
import copy
import json
import os
from hashlib import sha256
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.synthetic_projection_data import market_projection_case
from tests.test_run08_policy_unavailable import base_state
from tradingagents.agents.analysts.submission import (
    ANALYST_OUTPUT_GUIDANCE,
    build_analyst_update,
    process_analyst_response,
)
from tradingagents.evidence import (
    EvidenceState,
    SourceAcquisitionAvailable,
    SourceArtifact,
    ToolExecutionEvidenceEnvelope,
    evaluate_admission_gate,
)

_market = market_projection_case()
REF = _market["claims"][0]["source_ref"]
_snapshot = _market["tools"][1]
_snapshot["content"] += "\n| vwma | 7.85 |"
FIXTURE = {
    "market_tools": _market["tools"],
    "market_notes": [dict(_market["notes"][0], source_quote="| vwma | 7.85 |")],
    "market_claims": [
        {"claim_id": "market.bundle", "source_ref": REF,
         "statement": "The 10-day EMA is 7.90 and 50-day SMA is 7.36.",
         "source_quote": "| close_10_ema | 7.90 |\n| close_50_sma | 7.36 |"},
        {"claim_id": "market.vwma", "source_ref": REF,
         "statement": "VWMA is 7.85.", "source_quote": "| vwma | 7.85 |"},
    ],
}


def market_state():
    messages = []
    for i, tool in enumerate(FIXTURE["market_tools"]):
        name, content = tool["name"], tool["content"]
        call = {"name": name, "args": tool["args"], "id": f"offline-market-{i}", "type": "tool_call"}
        artifact = SourceArtifact(artifact_sha256=sha256(content.encode()).hexdigest(), raw_text=content,
            source_ref=REF, tool_call_id=call["id"], tool_name=name)
        capability = "market_snapshot" if name == "get_verified_market_snapshot" else name
        acquired = SourceAcquisitionAvailable(provider="offline-synthetic-run08", capability=capability,
            source_ref=REF, attempt=1, acquisition_id=f"offline-market-{i}",
            retrieved_at="2026-11-01T06:08:04.172023+00:00", artifact=artifact)
        envelope = ToolExecutionEvidenceEnvelope(tool_call_id=call["id"], tool_name=name,
            source_ref=REF, capability=capability, acquisition_outcomes=(acquired,), selected_artifact=artifact)
        messages.extend([AIMessage(content="", tool_calls=[call]), ToolMessage(name=name, tool_call_id=call["id"],
            content=content, artifact=envelope.model_dump(mode="json"))])
    state = base_state(messages)
    state["evidence_state"]["market_snapshot"]["snapshot_id"] = REF
    return state


def assess(claims):
    state = market_state()
    payload = {"report_markdown": "Offline synthetic/edited control; not qualified analysis.",
        "material_claims": [{k: c[k] for k in ("claim_id", "statement", "source_ref", "source_quote")} for c in claims],
        "evidence_notes": [{k: n[k] for k in ("note_id", "kind", "statement", "source_ref", "source_quote")}
                           for n in FIXTURE["market_notes"]]}
    response = AIMessage(content="", tool_calls=[{"name": "AnalystEvidenceReport", "args": payload,
                                                 "id": "offline-market-submit", "type": "tool_call"}])
    result = process_analyst_response(response, finalizer=None, analyst="market", ticker="601328.SS",
                                     trade_date="2026-10-16", messages=state["messages"])
    update = build_analyst_update(state, result, "market_report")
    evidence = EvidenceState.model_validate(update["evidence_state"])
    return evidence, evaluate_admission_gate(evidence)


def atomic():
    return [{"claim_id": "market.atomic." + key, "statement": f"The {label} is {value}.",
             "source_ref": REF, "source_quote": f"| {key} | {value} |"}
            for key, label, value in [("close_10_ema", "10-day EMA", "7.90"),
                                      ("close_50_sma", "50-day SMA", "7.36"),
                                      ("close_200_sma", "200-day SMA", "6.85")]]


def test_original_bundle_stays_rejected_and_vwma_stays_supported():
    evidence, _ = assess(FIXTURE["market_claims"])
    assert [v.status.value for v in evidence.claim_validations] == ["unsupported", "supported"]
    assert evidence.claim_validations[0].detail == "indicator name metadata is not bound to one unambiguous observation"
    assert all(n.binding_status == "source_bound_quote" for n in evidence.evidence_notes)


def test_explicitly_edited_atomic_controls_are_bound_without_validator_change():
    evidence, gate = assess(atomic())
    assert [v.status.value for v in evidence.claim_validations] == ["supported"] * 3
    assert gate.admitted


@pytest.mark.parametrize("statement,quote", [
    ("The 20-day EMA is 7.90.", "| close_10_ema | 7.90 |"),
    ("The 10-day SMA is 7.90.", "| close_10_ema | 7.90 |"),
    ("The 10-day EMA is 8.90.", "| close_10_ema | 7.90 |"),
    ("The 10-day EMA is 7.90 on 2026-10-15.", "| close_10_ema | 7.90 |"),
    ("The 10-day EMA is USD 7.90.", "| close_10_ema | 7.90 |"),
    ("The 10-day EMA is 7.90.", "| close_50_sma | 7.36 |"),
    ("RSI is 74.58 above the overbought threshold of 70.", "| rsi | 74.58 |"),
    ("The 10-day EMA is 7.90 and 50-day SMA is 7.36.", "| close_10_ema | 7.90 |\n| close_50_sma | 7.36 |"),
])
def test_wrong_identity_value_date_unit_and_unregistered_threshold_stay_rejected(statement, quote):
    claim = atomic()[0] | {"statement": statement, "source_quote": quote}
    evidence, _ = assess([claim])
    assert evidence.claim_validations[0].status.value == "unsupported"


def test_prompt_asks_atomic_rows_without_new_exemptions():
    assert "keep each material observation atomic" in ANALYST_OUTPUT_GUIDANCE
    assert "does not exempt" in ANALYST_OUTPUT_GUIDANCE


def test_retained_vs_control_matrix_is_separate_from_original_result(tmp_path):
    original, original_gate = assess(copy.deepcopy(FIXTURE["market_claims"]))
    edited, edited_gate = assess(atomic())
    matrix = {"historical_replay": False, "decision_qualified": False, "model_calls": 0,
        "synthetic_bundled_statements": {"validations": [v.status.value for v in original.claim_validations],
                                        "local_gate_admitted": original_gate.admitted},
        "explicit_atomic_control": {"validations": [v.status.value for v in edited.claim_validations],
                                    "local_gate_admitted": edited_gate.admitted},
        "limit": "Edited control, not a new model response or proof of future compliance/decision qualification."}
    out = Path(os.environ.get("BOUNDARY_TEST_OUTPUT", str(tmp_path))) / "market-comparison.json"
    with out.open("x", encoding="utf-8") as stream:
        json.dump(matrix, stream, indent=2)

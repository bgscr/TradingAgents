"""Synthetic indicator and cash projections for bounded excerpt checks."""

import copy
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.synthetic_projection_data import indicator_and_cash_case
from tradingagents.agents.analysts.submission import build_analyst_update, process_analyst_response
from tradingagents.evidence import (
    EvidenceState,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    SourceAcquisitionAvailable,
    SourceArtifact,
    ToolExecutionEvidenceEnvelope,
    evaluate_admission_gate,
)

FIXTURE = indicator_and_cash_case()
SYMBOL, DATE = "601328.SS", "2026-10-16"


def submit(index=0, *, tools=None, statement=None, quote=None, identity=True, claim_ref=None):
    claim = copy.deepcopy(FIXTURE["claims"][index])
    if claim_ref is not None:
        claim["source_ref"] = claim_ref
    if statement is not None:
        claim["statement"] = statement
    if quote is not None:
        claim["source_quote"] = quote
    tools = copy.deepcopy(
        tools if tools is not None else FIXTURE["tools"][:3] if index == 0 else FIXTURE["tools"][3:]
    )
    messages = []
    for i, tool in enumerate(tools):
        call = {
            "name": tool["name"],
            "args": tool["args"],
            "id": f"offline-run09-{i}",
            "type": "tool_call",
        }
        if call["name"] == "get_fundamentals":
            source_ref = f"{call['name']}:{call['args']['ticker']}:{call['args']['curr_date']}"
        else:
            source_ref = FIXTURE["claims"][0]["source_ref"]
        artifact = SourceArtifact(
            artifact_sha256=sha256(tool["content"].encode()).hexdigest(),
            source_ref=source_ref,
            tool_call_id=call["id"],
            tool_name=call["name"],
            raw_text=tool["content"],
        )
        capability = (
            "market_snapshot" if call["name"] == "get_verified_market_snapshot" else call["name"]
        )
        acquired = SourceAcquisitionAvailable(
            provider="synthetic-run09",
            capability=capability,
            source_ref=source_ref,
            attempt=1,
            artifact=artifact,
            acquisition_id=f"offline-run09-acquisition-{i}",
            retrieved_at="2026-11-01T07:56:20+00:00",
        )
        envelope = ToolExecutionEvidenceEnvelope(
            tool_call_id=call["id"],
            tool_name=call["name"],
            source_ref=source_ref,
            capability=capability,
            acquisition_outcomes=(acquired,),
            selected_artifact=artifact,
        )
        messages += [
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(
                content=tool["content"],
                name=call["name"],
                tool_call_id=call["id"],
                artifact=envelope.model_dump(mode="json"),
            ),
        ]
    instrument = (
        InstrumentIdentityEvidence(
            symbol=SYMBOL,
            venue="XSHG",
            instrument_kind="equity",
            currency="CNY",
            provenance={
                "provider": "synthetic",
                "source_ref": "synthetic:offline-run09",
                "artifact_sha256": "a" * 64,
                "retrieved_at": "2026-11-01T00:00:00+00:00",
            },
        )
        if identity
        else None
    )
    evidence = EvidenceState(
        instrument_identity=instrument,
        market_snapshot=MarketSnapshotEvidence(
            symbol=SYMBOL,
            provider="synthetic",
            retrieved_at="2026-11-01T00:00:00+00:00",
            adjustment_basis="qfq",
            requested_date=DATE,
            effective_trading_date=DATE,
            history_rows=1212,
            frame_sha256="b" * 64,
            snapshot_id=FIXTURE["claims"][0]["source_ref"],
        ),
    )
    state = {
        "company_of_interest": SYMBOL,
        "trade_date": DATE,
        "messages": messages,
        "evidence_state": evidence.model_dump(mode="json"),
    }
    payload = {
        "report_markdown": "Synthetic projection, not a trading decision.",
        "material_claims": [
            {k: claim[k] for k in ("claim_id", "statement", "source_ref", "source_quote")}
        ],
        "evidence_notes": [],
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "AnalystEvidenceReport",
                "args": payload,
                "id": "offline-submit",
                "type": "tool_call",
            }
        ],
    )
    result = process_analyst_response(
        response,
        finalizer=None,
        analyst=claim["analyst"],
        ticker=SYMBOL,
        trade_date=DATE,
        messages=messages,
    )
    update = build_analyst_update(
        state, result, "market_report" if index == 0 else "fundamentals_report"
    )
    output = EvidenceState.model_validate(update["evidence_state"])
    gate = evaluate_admission_gate(output)
    assert all(f.fact_kind == "excerpt" for f in output.source_facts)
    return output.claim_validations[0], output, gate


def test_synthetic_shared_projection_english_indicator_is_bound():
    assert [
        t["content"].count(FIXTURE["claims"][0]["source_quote"]) for t in FIXTURE["tools"][:3]
    ] == [0, 1, 0]
    validation, _, gate = submit()
    assert validation.status.value == "supported", validation.detail
    assert gate.admitted  # Synthetic narrow gate only, never policy/decision qualification.


def test_synthetic_negative_cny_sign_is_preserved():
    validation, _, _ = submit(1)
    assert validation.status.value == "supported", validation.detail


@pytest.mark.parametrize("ref", [
    "synthetic:cash",
    "get_fundamentals:600000.SS:2026-10-16",
    "get_fundamentals:601328.SS:2026-10-15",
])
def test_cash_quote_requires_matching_tool_reference(ref):
    validation, _, _ = submit(1, claim_ref=ref)
    assert validation.status.value == "unsupported"


@pytest.mark.parametrize("order", [(1,), (0, 1), (1, 2), (2, 1, 0)])
def test_indicator_projection_order_does_not_select_a_convenient_value(order):
    validation, _, _ = submit(tools=[FIXTURE["tools"][i] for i in order])
    assert validation.status.value == "supported", validation.detail


@pytest.mark.parametrize(
    "change",
    [
        "period",
        "kind",
        "date",
        "reversed_date",
        "amount",
        "sign",
        "partial",
        "duplicate_quote",
        "duplicate_projection",
        "conflicting_frame",
        "conflicting_symbol",
        "excluded_call",
        "wrong_request_date",
        "wrong_heading_date",
        "unknown_indicator",
        "no_identity",
        "multi_indicator",
        "rsi_rule",
    ],
)
def test_indicator_identity_rejects_wrong_or_ambiguous_evidence(change):
    tools = copy.deepcopy(FIXTURE["tools"][:3])
    statement = FIXTURE["claims"][0]["statement"]
    quote = FIXTURE["claims"][0]["source_quote"]
    if change == "period":
        statement = statement.replace("50-day", "200-day")
    if change == "kind":
        statement = statement.replace("SMA", "EMA")
    if change == "date":
        statement = statement.replace(DATE, "2026-10-15")
    if change == "reversed_date":
        statement = statement.replace(DATE, "2026-16-10")
    if change == "amount":
        statement = statement.replace("7.3602", "7.3800")
    if change == "sign":
        statement = statement.replace("7.3602", "-7.3602")
    if change == "partial":
        quote = "7.3602"
    if change == "duplicate_quote":
        tools[2]["content"] += "\n" + quote
    if change == "duplicate_projection":
        tools.append(copy.deepcopy(tools[1]))
    if change == "conflicting_frame":
        tools[2]["content"] = tools[2]["content"].replace(
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "c" * 64
        )
    if change in ("conflicting_symbol", "excluded_call"):
        tools[0]["args"]["symbol"] = "600000.SS"
    if change == "wrong_request_date":
        tools[1]["args"]["curr_date"] = "2026-10-15"
    if change == "wrong_heading_date":
        tools[1]["content"] = tools[1]["content"].replace("to 2026-10-16:", "to 2026-10-15:", 1)
    if change == "unknown_indicator":
        tools[1]["args"]["indicator"] = "unknown"
    if change == "multi_indicator":
        statement += " The 200-day SMA was 7.3602."
    if change == "rsi_rule":
        statement += " RSI is over threshold 70."
    v, _, _ = submit(
        tools=tools, statement=statement, quote=quote, identity=change != "no_identity"
    )
    assert v.status.value == "unsupported", (change, v.detail)


@pytest.mark.parametrize(
    "replacement",
    ["CNY 71,235,000,000", "+CNY 71,235,000,000", "-CNY 71,236,000,000", "-CNY 71,235,000,000%"],
)
def test_currency_wrong_sign_magnitude_or_percent_is_not_refunded(replacement):
    statement = FIXTURE["claims"][1]["statement"].replace("-CNY 71,235,000,000", replacement)
    validation, _, _ = submit(1, statement=statement)
    assert validation.status.value == "unsupported"


@pytest.mark.parametrize("value", ["2026", "10", "16"])
def test_indicator_value_cannot_borrow_a_date_component(value):
    statement = FIXTURE["claims"][0]["statement"].replace("7.3602", value)
    validation, _, _ = submit(statement=statement)
    assert validation.status.value == "unsupported"


def test_english_indicator_without_an_observed_value_is_not_a_numeric_fact():
    validation, _, _ = submit(statement="The 50-day SMA on 2026-10-16.")
    assert validation.status.value == "unsupported"


@pytest.mark.parametrize("index", [0, 2])
def test_companion_identity_title_must_match_request(index):
    tools = copy.deepcopy(FIXTURE["tools"][:3])
    tools[index]["content"] = tools[index]["content"].replace("for 601328.SS", "for 600000.SS", 1)
    validation, _, _ = submit(tools=tools)
    assert validation.status.value == "unsupported"

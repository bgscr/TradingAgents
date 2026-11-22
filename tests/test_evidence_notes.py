"""Synthetic source-bound limitations and identity metadata; no external data."""
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from tradingagents.agents.analysts.submission import (
    build_analyst_update,
    process_analyst_response,
)
from tradingagents.evidence import (
    AnalystEvidenceReport,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    ToolExecutionEvidenceEnvelope,
    build_tool_evidence_state,
    decision_ready_material_claims,
    evaluate_admission_gate,
)

SYMBOL = "601328.SS"
DATE = "2026-10-16"
REF = f"get_income_statement:synthetic:{SYMBOL}:{DATE}:annual"
LIMIT = "bank_unavailable_metrics=net_interest_margin,nonperforming_loan_ratio"


def identity(**updates):
    data = {"symbol": SYMBOL, "venue": "XSHG", "instrument_kind": "equity", "currency": "CNY",
        "provenance": {"provider": "synthetic", "source_ref": "synthetic:identity",
            "retrieved_at": f"{DATE}T00:00:00+00:00", "artifact_sha256": "a" * 64}}
    data.update(updates)
    return InstrumentIdentityEvidence(**data)


def state(*, unavailable=False, content=None, with_identity=True, required=False):
    content = content or ("DATA_GAP: bank metrics unavailable" if unavailable else
        "SYNTHETIC DIAGNOSTIC DATA — NOT OBSERVED COMPANY DATA\n" + LIMIT +
        "\nperiod_end,field,value,unit\n2025-12-31,net_profit,123.45,CNY\n")
    call = {"name": "get_income_statement", "id": "synthetic-income", "type": "tool_call",
        "args": {"ticker": SYMBOL, "curr_date": DATE, "frequency": "annual"}}
    common = {"provider": "synthetic", "capability": "get_income_statement", "source_ref": REF,
        "attempt": 1, "retrieved_at": f"{DATE}T00:00:00+00:00"}
    artifact = None if unavailable else SourceArtifact(
        artifact_sha256=sha256(content.encode()).hexdigest(), source_ref=REF,
        tool_call_id=call["id"], tool_name=call["name"], raw_text=content)
    outcome = (SourceAcquisitionUnavailable(**common, retryable=False, reason="provider_error")
        if unavailable else SourceAcquisitionAvailable(**common, artifact=artifact))
    envelope = ToolExecutionEvidenceEnvelope(tool_call_id=call["id"], tool_name=call["name"],
        source_ref=REF, capability="get_income_statement", acquisition_outcomes=(outcome,),
        selected_artifact=artifact)
    evidence = EvidenceState(instrument_identity=identity() if with_identity else None,
        market_snapshot=MarketSnapshotEvidence(symbol=SYMBOL, provider="synthetic",
            retrieved_at=f"{DATE}T00:00:00+00:00", adjustment_basis="qfq",
            requested_date=DATE, effective_trading_date=DATE, history_rows=1212,
            frame_sha256="b" * 64, snapshot_id="snapshot:synthetic"),
        sources=(EvidenceSource(source_id=REF, status=EvidenceStatus.AVAILABLE,
            required=True),) if required else ())
    return {"company_of_interest": SYMBOL, "trade_date": DATE,
        "evidence_state": evidence.model_dump(mode="json"), "messages": [
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content=content, name=call["name"], tool_call_id=call["id"],
                artifact=envelope.model_dump(mode="json")),
        ]}


def claim(statement, quote="2025-12-31,net_profit,123.45,CNY"):
    return {"claim_id": "fundamentals.profit", "statement": statement,
        "source_ref": REF, "source_quote": quote}


def note(**updates):
    data = {"note_id": "fundamentals.bank_gap", "kind": "availability",
        "statement": "Bank metrics missing; their investment implications are unassessable.",
        "source_ref": REF, "source_quote": LIMIT}
    data.update(updates)
    return data


def submit(current, *, claims=(), notes=(), finalizer=None):
    payload = {"report_markdown": "Synthetic limitations; no investment decision.",
        "material_claims": list(claims), "evidence_notes": list(notes)}
    response = AIMessage(content="draft" if finalizer else "", tool_calls=[] if finalizer else [
        {"name": "AnalystEvidenceReport", "args": payload, "id": "report", "type": "tool_call"}])
    result = process_analyst_response(response, finalizer=finalizer, analyst="fundamentals",
        ticker=SYMBOL, trade_date=DATE, messages=current["messages"])
    update = build_analyst_update(current, result, "fundamentals_report")
    return EvidenceState.model_validate(update["evidence_state"]), result


@pytest.mark.parametrize("statement", [
    "601328.SS net profit was 123.45 CNY.", "证券：601328.SS；净利润123.45 CNY。",
    "证券601328.SS的净利润为123.45 CNY。",
])
def test_exact_identity_span_is_not_an_amount(statement):
    evidence, _ = submit(state(), claims=[claim(statement)])
    assert evidence.claim_validations[0].status.value == "supported"
    assert evidence.material_claims[0].statement == statement


@pytest.mark.parametrize("statement", [
    "601328.SS profit was 601328 CNY.", "601328.SS return was 601328%.",
    "601328 profit was 123.45 CNY.", "601329.SS profit was 123.45 CNY.",
    "601328.SS.extra profit was 123.45 CNY.",
    "601328.SZ profit was 123.45 CNY.",
])
def test_other_numeric_spans_are_not_exempt(statement):
    evidence, _ = submit(state(), claims=[claim(statement)])
    assert evidence.claim_validations[0].status.value == "unsupported"


def test_ascii_word_prefix_does_not_enter_new_ticker_exemption():
    from tradingagents.evidence import _instrument_numeric_statement

    text = "x601328.SS profit was 123.45 CNY."
    assert _instrument_numeric_statement(text, SYMBOL) == text
    # Existing numeric parsing already excludes ASCII word identifiers. Do not
    # invent a stronger preexisting semantic check to claim this patch fixed it.
    bound, _ = submit(state(), claims=[claim(text)])
    unbound, _ = submit(state(with_identity=False), claims=[claim(text)])
    assert bound.claim_validations == unbound.claim_validations


@pytest.mark.parametrize("case_name", ["none", "no_provenance", "wrong_symbol", "wrong_venue"])
def test_ticker_exemption_requires_authoritative_matching_identity(case_name):
    current = state()
    identities = {"none": None, "no_provenance": identity(provenance=None),
        "wrong_symbol": identity(symbol="601329.SS"), "wrong_venue": identity(venue="XSHE")}
    current["evidence_state"]["instrument_identity"] = (
        identities[case_name].model_dump(mode="json") if identities[case_name] else None)
    evidence, _ = submit(current, claims=[claim("601328.SS profit 123.45 CNY")])
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("change", ["missing", "name", "id", "ticker", "contradictory_symbol"])
def test_direct_builder_requires_exact_paired_request_for_identifier(change):
    current = state()
    call = dict(current["messages"][0].tool_calls[0])
    call["args"] = dict(call["args"])
    if change in {"name", "id"}:
        call[change] = "wrong"
    if change == "ticker":
        call["args"]["ticker"] = "601329.SS"
    if change == "contradictory_symbol":
        call["args"]["symbol"] = "601329.SS"
    args = claim("601328.SS profit 123.45 CNY")
    material = MaterialClaim(analyst="fundamentals", source_refs=(args.pop("source_ref"),), **args)
    evidence = build_tool_evidence_state(current["messages"], [material],
        tool_call_ids_by_source={REF: ["synthetic-income"]},
        tool_calls_by_id={} if change == "missing" else {"synthetic-income": call},
        instrument_identity=identity())
    assert evidence.claim_validations[0].status.value == "unsupported"


def test_notes_bind_trace_without_becoming_financial_evidence():
    current = state()
    evidence, _ = submit(current, notes=[note()])
    record = evidence.evidence_notes[0]
    text = current["messages"][1].content
    assert record.binding_status == "source_bound_quote"
    assert text[record.quote_start:record.quote_end] == LIMIT
    assert record.source_content_sha256 == sha256(text.encode()).hexdigest()
    assert record.source_status is EvidenceStatus.AVAILABLE
    assert record.source_required is False
    assert record.metric_requirement == "not_assessed"
    assert record.semantic_status == "not_established"
    assert record.decision_qualified is False
    assert evidence.material_claims == evidence.source_facts == evidence.claim_validations == ()
    assert decision_ready_material_claims(evidence) == ()
    assert EvidenceState.model_validate_json(evidence.model_dump_json()) == evidence


@pytest.mark.parametrize("content", ["different source text", LIMIT + "\n" + LIMIT])
def test_rebinding_typed_note_cannot_reuse_prior_success(content):
    original, _ = submit(state(), notes=[note()])
    changed = state(content=content)
    evidence = build_tool_evidence_state(changed["messages"], (),
        notes=original.evidence_notes, tool_call_ids_by_source={REF: ["synthetic-income"]})
    record = evidence.evidence_notes[0]
    assert record.binding_status == "unverified"
    assert record.tool_call_id is record.source_content_sha256 is record.quote_start is record.quote_end is None
    assert EvidenceState.model_validate_json(evidence.model_dump_json()) == evidence


@pytest.mark.parametrize("required", [False, True])
def test_unavailable_note_retains_source_policy_and_gate(required):
    current = state(unavailable=True, required=required)
    evidence, _ = submit(current, notes=[note(source_quote=current["messages"][1].content)])
    record = evidence.evidence_notes[0]
    assert record.binding_status == "source_bound_quote"
    assert record.source_status is EvidenceStatus.UNAVAILABLE
    assert record.source_required is required
    assert evidence.source_artifacts == evidence.source_facts == ()
    assert len(evidence.acquisition_outcomes) == 1
    gate = evaluate_admission_gate(evidence)
    assert gate.admitted is (not required)
    assert gate.readiness.value == ("insufficient" if required else "degraded")
    assert any(REF in reason for reason in gate.diagnostics)


def test_no_claim_or_note_cannot_hide_required_unavailable_source():
    evidence, _ = submit(state(unavailable=True, required=True))
    assert len(evidence.acquisition_outcomes) == 1
    assert evidence.sources[0].required is True
    assert not evaluate_admission_gate(evidence).admitted


@pytest.mark.parametrize("kind", ["missing_quote", "unknown_ref", "ambiguous", "bad_envelope"])
def test_unbound_notes_stop_submission_instead_of_becoming_facts(kind):
    current = state(content=(LIMIT + "\n" + LIMIT) if kind == "ambiguous" else None)
    limitation = note()
    if kind == "missing_quote":
        limitation["source_quote"] = "not present"
    if kind == "unknown_ref":
        limitation["source_ref"] = "invented:ref"
    if kind == "bad_envelope":
        current["messages"][1].artifact["selected_artifact"]["raw_text"] = "forged"
    evidence, _ = submit(current, notes=[limitation])
    assert evidence.evidence_notes[0].binding_status == "unverified"
    source = next(s for s in evidence.sources if s.source_id == "analyst.fundamentals.submission")
    assert source.status is EvidenceStatus.UNAVAILABLE and source.required
    assert not evaluate_admission_gate(evidence).admitted
    assert evidence.source_facts == ()


@pytest.mark.parametrize("field,value", [
    ("decision_qualified", True), ("source_required", False), ("semantic_status", "verified"),
    ("kind", "financial_fact"), ("fact_ids", ["injected"]),
])
def test_model_cannot_author_qualification_or_policy_fields(field, value):
    with pytest.raises(ValidationError):
        AnalystEvidenceReport(report_markdown="x", evidence_notes=[note(**{field: value})])


def test_old_checkpoint_and_report_shape_remain_compatible():
    evidence = EvidenceState.model_validate({"contract_version": "1.0"})
    assert evidence.evidence_notes == ()
    assert "evidence_notes" not in evidence.model_dump(mode="json")
    assert AnalystEvidenceReport(report_markdown="legacy").evidence_notes == ()


def test_note_id_cannot_reclassify_existing_financial_claim():
    current = state()
    evidence, _ = submit(current, claims=[claim("profit was 123.45 CNY")])
    current["evidence_state"] = evidence.model_dump(mode="json")
    merged, _ = submit(current, notes=[note(note_id="fundamentals.profit")])
    assert merged.evidence_notes == ()
    assert merged.material_claims == evidence.material_claims
    assert not evaluate_admission_gate(merged).admitted


def test_note_only_report_cannot_pass_actual_graph_policy_admission():
    from tradingagents.decision_policy import (
        DecisionHorizon,
        DecisionPolicyEngine,
        StrategyRuleDefinition,
    )
    from tradingagents.graph.evidence_gate import create_admission_gate_node, route_after_admission

    horizon = DecisionHorizon(count=20, unit="trading_days")
    # A synthetic registered bank rule, not a newly introduced production policy.
    rule = StrategyRuleDefinition(rule_id="synthetic.bank", version="1", applicable_instrument_kinds=("equity",),
        required_canonical_fields=("net_interest_margin", "nonperforming_loan_ratio"),
        target_rating="Buy", polarity="supports", horizon=horizon, predicate_id="synthetic.bank_predicate",
        comparator="greater_than", threshold=0, implementation_version="synthetic-1")
    policy = DecisionPolicyEngine(rules=[rule])
    evidence, _ = submit(state(), notes=[note()])
    assert evaluate_admission_gate(evidence).admitted  # Reach the intended policy gate.
    assert policy.candidate_applications(evidence, horizon=horizon) == ()
    result = create_admission_gate_node(decision_policy=policy, decision_horizon=horizon)({
        "evidence_state": evidence.model_dump(mode="json"), "trade_date": DATE, "run_id": "run:" + "c" * 64,
    })
    assert result["admission_gate"]["admitted"] is False
    assert "no_applicable_registered_strategy_rule" in result["admission_gate"]["diagnostics"]
    assert "validated_decision_context" not in result
    assert route_after_admission(result) == "blocked"
    assert "No Trading Decision was issued." in result["analysis_outcome"]


def test_finalizer_keeps_limitations_separate_using_production_submission_path():
    class SyntheticTransport:
        calls = 0

        def invoke(self, prompt):
            self.calls += 1
            assert "evidence_notes" in prompt
            assert "not financial evidence" in prompt
            return AnalystEvidenceReport(report_markdown="No bank investment conclusion.", evidence_notes=[note()])

    transport = SyntheticTransport()
    evidence, result = submit(state(), finalizer=transport)
    assert transport.calls == 1
    assert result.submission_source.detail == "finalized_structured"
    assert len(evidence.evidence_notes) == 1
    assert evidence.material_claims == evidence.source_facts == ()

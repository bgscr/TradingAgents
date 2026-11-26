"""Production submission/evidence/report boundary with synthetic statement rows."""

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from tests.test_claim_semantic_boundaries import CASES, ROWS, TABLE as LEGACY_TABLE
from tests.test_evidence_notes import DATE, SYMBOL, claim, identity, state
from tradingagents.agents.analysts.submission import build_analyst_update, process_analyst_response
from tradingagents.evidence import (
    DraftThesis,
    EvidenceState,
    decision_ready_material_claims,
    evaluate_decision_gate,
    verified_financial_observations,
)

MARKER = "# financial_observations_contract=current-statement-observation-v1"
TABLE = LEGACY_TABLE.replace(",CNY,", ",CNY_BASE_UNIT,")
ROW = TABLE.splitlines()[1]
CONTENT = (
    f"# Income Statement data for {SYMBOL} (annual)\n"
    "# CURRENT-only; source=tushare; not strict PIT evidence.\n" + MARKER + "\n" + TABLE + "\n"
)


def submit_row(*, content=CONTENT, quote=ROW, statement=None, current=None):
    current = current or state(content=content)
    statement = statement or f"{SYMBOL} net income was 123.45 CNY for 2024-12-31."
    payload = {
        "report_markdown": statement,
        "material_claims": [claim(statement, quote)],
        "evidence_notes": [],
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "AnalystEvidenceReport", "args": payload, "id": "report", "type": "tool_call"}
        ],
    )
    result = process_analyst_response(
        response,
        finalizer=None,
        analyst="fundamentals",
        ticker=SYMBOL,
        trade_date=DATE,
        messages=current["messages"],
    )
    update = build_analyst_update(current, result, "fundamentals_report")
    return EvidenceState.model_validate(update["evidence_state"]), update


def test_unique_complete_source_row_has_typed_observation_without_decision_upgrade():
    evidence, update = submit_row()
    fact = evidence.source_facts[0]
    observation = fact.model_dump(mode="json").get("financial_observation")
    assert observation is not None
    assert observation["field"] == "net_income"
    assert observation["value"] == "123.45"
    assert observation["unit"] == "CNY" and observation["scale"] == "1"
    assert observation["period_end"] == ROWS[0]["period_end"]
    assert observation["ann_date"] == ROWS[0]["ann_date"]
    assert observation["frequency"] == "annual"
    assert observation["period_basis"] == "cumulative_report_type_1"
    assert fact.fact_kind == "excerpt" and fact.canonical_field == ""
    assert fact.calculation_lineage is None
    assert decision_ready_material_claims(evidence) == ()
    assert "Advisory commentary" in update["fundamentals_report"]
    assert (
        "net_income | 123.45 CNY | scale=1 | period_end=2024-12-31" in update["fundamentals_report"]
    )


@pytest.mark.parametrize("statement", list(CASES.values()))
def test_source_row_does_not_certify_wrong_prose_or_bypass_final_gate(statement):
    # Wrong-period prose can contain a number in another row; that remains only
    # lexical support. Neither whole-table nor single-row citations certify it.
    evidence, update = submit_row(statement=statement)
    assert evidence.material_claims[0].statement == statement
    assert decision_ready_material_claims(evidence) == ()
    gate = evaluate_decision_gate(
        DraftThesis(
            rating="BUY",
            narrative="test",
            material_claim_ids=(evidence.material_claims[0].claim_id,),
        ),
        evidence,
    )
    assert not gate.permitted
    assert "Advisory commentary" in update["fundamentals_report"]
    assert all(
        not x.statement_verified and not x.decision_qualified
        for x in verified_financial_observations(evidence)
    )


@pytest.mark.parametrize(
    "quote",
    [
        TABLE,
        TABLE.splitlines()[0],
        "123.45",
        "net_income,123.45,CNY",
        ROW + "\n" + TABLE.splitlines()[2],
    ],
)
def test_partial_header_multiline_quotes_never_get_typed_meaning(quote):
    evidence, _ = submit_row(quote=quote)
    assert all(f.financial_observation is None for f in evidence.source_facts)
    assert verified_financial_observations(evidence) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "USD"},
        {"unit": "CNY"},
        {"original_unit": "USD"},
        {"original_unit": "CNY_MILLION"},
        {"original_unit": "万元"},
        {"original_value": "123450000"},
        {"value": "NaN"},
        {"value": "Infinity"},
        {"value": "1_23.45"},
        {"value": " 123.45"},
        {"field": ""},
        {"provider_field": ""},
        {"period_end": "2024-09-30"},
        {"period_end": "2024-12-30"},
        {"ann_date": "2024-12-30"},
        {"f_ann_date": "2024-12-30"},
        {"retrieved_at": "2026-10-16T00:00:00"},
        {"observed_at": "not-a-date"},
        {"artifact_identity": ""},
        {"value_status": "unknown"},
    ],
)
def test_malformed_source_contract_does_not_assign_typed_semantics(change):
    values = dict(ROWS[0], original_unit="CNY_BASE_UNIT", unit="CNY_BASE_UNIT")
    values.update(change)
    row = ",".join(values[key] for key in ROWS[0])
    evidence, _ = submit_row(content=CONTENT.replace(ROW, row), quote=row)
    assert all(f.financial_observation is None for f in evidence.source_facts)
    assert verified_financial_observations(evidence) == ()


@pytest.mark.parametrize(
    "case",
    [
        "no_marker",
        "duplicate_marker",
        "duplicate_header",
        "duplicate_row",
        "wrong_tool_title",
        "wrong_symbol",
        "ragged",
        "broken_csv",
    ],
)
def test_unknown_or_ambiguous_table_is_not_inferred(case):
    content = CONTENT
    quote = ROW
    if case == "no_marker":
        content = content.replace(MARKER + "\n", "")
    if case == "duplicate_marker":
        content = content.replace(MARKER, MARKER + "\n" + MARKER)
    if case == "duplicate_header":
        content += TABLE.splitlines()[0] + "\n"
    if case == "duplicate_row":
        content += ROW + "\n"
    if case == "wrong_tool_title":
        content = content.replace("Income Statement", "Balance Sheet")
    if case == "wrong_symbol":
        content = content.replace(SYMBOL, "601329.SS")
    if case == "ragged":
        content = content.replace(ROW, ROW + ",extra")
        quote += ",extra"
    if case == "broken_csv":
        content = content.replace(ROW, '"' + ROW)
        quote = '"' + ROW
    evidence, _ = submit_row(content=content, quote=quote)
    assert all(f.financial_observation is None for f in evidence.source_facts)
    assert verified_financial_observations(evidence) == ()


@pytest.mark.parametrize(
    "change",
    [{"venue": "XSHE"}, {"currency": "USD"}, {"symbol": "601329.SS"}, {"provenance": None}],
)
def test_authoritative_matching_security_is_required(change):
    current = state(content=CONTENT)
    current["evidence_state"]["instrument_identity"] = identity(**change).model_dump(mode="json")
    evidence, _ = submit_row(current=current)
    assert all(f.financial_observation is None for f in evidence.source_facts)
    assert verified_financial_observations(evidence) == ()


def test_roundtrip_rederives_observation_and_keeps_legacy_serialization():
    evidence, _ = submit_row()
    restored = EvidenceState.model_validate_json(evidence.model_dump_json())
    assert verified_financial_observations(restored) == verified_financial_observations(evidence)
    assert len(verified_financial_observations(restored)) == 1
    legacy, _ = submit_row(content=CONTENT.replace(MARKER + "\n", ""))
    assert "financial_observation" not in legacy.source_facts[0].model_dump(mode="json")
    assert decision_ready_material_claims(legacy) == ()


@pytest.mark.parametrize(
    "case",
    [
        "field",
        "value",
        "period",
        "quote",
        "source",
        "fact_id",
        "call_id",
        "artifact_hash",
        "unavailable",
        "identity_venue",
        "identity_currency",
        "identity_symbol",
        "invalid_status",
        "canonical_upgrade",
        "changed_source_text",
    ],
)
def test_reloaded_bindings_are_rechecked_before_observation_display(case):
    evidence, _ = submit_row()
    data = evidence.model_dump(mode="json")
    fact = data["source_facts"][0]
    if case == "field":
        fact["financial_observation"]["field"] = "revenue"
    if case == "value":
        fact["financial_observation"]["value"] = "999.99"
    if case == "period":
        fact["financial_observation"]["period_end"] = "2025-12-31"
    if case == "quote":
        data["material_claims"][0]["source_quote"] = TABLE
    if case == "source":
        data["material_claims"][0]["source_refs"] = ["synthetic:unbound"]
    if case == "fact_id":
        fact["fact_id"] = "fact:" + "f" * 64
    if case == "call_id":
        fact["tool_call_id"] = "other-call"
    if case == "artifact_hash":
        fact["artifact_sha256"] = "f" * 64
    if case == "unavailable":
        for source in data["sources"]:
            source["status"] = "unavailable"
    if case == "identity_venue":
        data["instrument_identity"]["venue"] = "XSHE"
    if case == "identity_currency":
        data["instrument_identity"]["currency"] = "USD"
    if case == "identity_symbol":
        data["instrument_identity"]["symbol"] = "601329.SS"
    if case == "invalid_status":
        data["claim_validations"][0]["status"] = "unsupported"
    if case == "canonical_upgrade":
        fact["fact_kind"] = "canonical"
    if case == "changed_source_text":
        data["source_artifacts"][0]["raw_text"] += "# tampered"
    try:
        restored = EvidenceState.model_validate(data)
    except ValidationError:
        return  # Closed contract rejection is also fail-closed.
    assert verified_financial_observations(restored) == ()


def test_quarterly_income_is_cumulative_not_a_single_quarter_or_calculation():
    content = CONTENT.replace("(annual)", "(quarterly)")
    current = state(content=content)
    current["messages"][0].tool_calls[0]["args"]["frequency"] = "quarterly"
    evidence, _ = submit_row(content=content, current=current)
    observation = evidence.source_facts[0].financial_observation
    assert observation.frequency == "quarterly"
    assert observation.period_basis == "cumulative_report_type_1"
    assert observation.scale == "1"
    assert evidence.source_facts[0].calculation_lineage is None
    assert not observation.decision_qualified


@pytest.mark.parametrize("tool", ["get_balance_sheet", "get_income_statement", "get_cashflow"])
@pytest.mark.parametrize("source_unit", [None, "CNY_YUAN", "yuan", "元"])
def test_production_normalizer_renderer_envelope_and_submission_bind_units(
    tmp_path, monkeypatch, tool, source_unit
):
    from tests.test_current_tushare_statements import (
        Clock,
        complete_frame,
        current_statements,
        dispatcher,
        request,
        runtime_config,
        statement_rows,
    )
    from tradingagents.asset_configuration import resolve_run_asset_configuration
    from tradingagents.dataflows.financial_dispatch import FinancialReportingFrequency

    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class SyntheticTransport:
        def query(self, endpoint, **params):
            calls.append(endpoint)
            frame = statement_rows(endpoint)
            if source_unit is not None:
                frame["unit"] = source_unit
            return complete_frame(frame)

    monkeypatch.setattr(
        current_statements, "create_financial_statement_client", lambda _: SyntheticTransport()
    )
    clock = Clock()
    req = request(tool, frequency=FinancialReportingFrequency.ANNUAL)
    result = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(req)
    assert len(calls) == 1 and result.artifact is not None
    quote = next(line for line in result.value.splitlines() if line.endswith(",reported"))
    asset = resolve_run_asset_configuration("600895.SS", config=config)
    call = {
        "name": tool,
        "id": req.tool_call_id,
        "args": {"ticker": "600895.SS", "curr_date": "2026-10-15", "frequency": "annual"},
        "type": "tool_call",
    }
    current = {
        "company_of_interest": "600895.SS",
        "trade_date": "2026-10-15",
        "evidence_state": EvidenceState(instrument_identity=asset.instrument_identity).model_dump(
            mode="json"
        ),
        "messages": [AIMessage(content="", tool_calls=[call]), result.to_tool_message()],
    }
    payload = {
        "report_markdown": "Synthetic source row only.",
        "material_claims": [
            {
                "claim_id": "fundamentals.source_row",
                "statement": "Source row quoted for inspection.",
                "source_ref": result.artifact.source_ref,
                "source_quote": quote,
            }
        ],
        "evidence_notes": [],
    }
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "AnalystEvidenceReport", "args": payload, "id": "report", "type": "tool_call"}
        ],
    )
    submitted = process_analyst_response(
        response,
        finalizer=None,
        analyst="fundamentals",
        ticker="600895.SS",
        trade_date="2026-10-15",
        messages=current["messages"],
    )
    update = build_analyst_update(current, submitted, "fundamentals_report")
    evidence = EvidenceState.model_validate(update["evidence_state"])
    observations = verified_financial_observations(evidence)
    assert len(observations) == 1
    assert observations[0].unit == "CNY" and observations[0].scale == "1"
    assert observations[0].tool_name == tool
    assert observations[0].statement_verified is False
    assert decision_ready_material_claims(evidence) == ()


def test_absent_financial_quote_still_marks_prose_advisory():
    evidence, update = submit_row(quote="absent source row")
    assert evidence.source_facts == ()
    assert evidence.claim_validations[0].status.value == "unsupported"
    assert "Advisory commentary" in update["fundamentals_report"]

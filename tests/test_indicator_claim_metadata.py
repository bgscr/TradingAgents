"""Indicator-name metadata must not become an exempt numerical fact."""
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tradingagents.evidence import (
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceArtifact,
    ToolExecutionEvidenceEnvelope,
    build_tool_evidence_state,
)


def evaluate(statement, quote, *, content=None, indicator="close_50_sma", tool="get_indicators", with_call=True,
             additional_results=(), catalog_name=None, return_inputs=False):
    content = content if content is not None else "date,indicator,value\n2026-10-16,close_50_sma,4.2900\n"
    ref = f"{tool}:601328.SS:2026-10-16"
    messages, calls = [], {}
    for index, text in enumerate((content, *additional_results)):
        call = {"name": tool, "args": {"symbol": "601328.SS", "curr_date": "2026-10-16",
            "indicator": indicator}, "id": f"synthetic-indicator-{index}", "type": "tool_call"}
        artifact = SourceArtifact(artifact_sha256=sha256(text.encode()).hexdigest(),
            source_ref=ref, tool_call_id=call["id"], tool_name=tool, raw_text=text)
        available = SourceAcquisitionAvailable(provider="synthetic", capability=tool, attempt=1,
            retrieved_at="2026-10-16T00:00:00+00:00", source_ref=ref,
            acquisition_id=f"synthetic-indicator-outcome-{index}", artifact=artifact)
        envelope = ToolExecutionEvidenceEnvelope(tool_call_id=call["id"], tool_name=tool,
            source_ref=ref, capability=tool, acquisition_outcomes=(available,), selected_artifact=artifact)
        messages.extend([AIMessage(content="", tool_calls=[call]),
            ToolMessage(content=text, name=tool, tool_call_id=call["id"], artifact=envelope.model_dump(mode="json"))])
        calls[call["id"]] = {**call, "name": catalog_name or tool}
    claim = MaterialClaim(claim_id="market.observation", analyst="market", statement=statement,
        source_refs=(ref,), source_quote=quote)
    evidence = build_tool_evidence_state(messages, (claim,), tool_call_ids_by_source={ref: tuple(calls)},
        tool_calls_by_id=calls if with_call else {})
    return (evidence, messages, claim) if return_inputs else evidence


def test_exact_bound_period_name_is_metadata_not_a_new_price():
    evidence = evaluate("50日均线为4.29。", "2026-10-16,close_50_sma,4.2900")
    assert evidence.claim_validations[0].status.value == "supported"
    assert evidence.material_claims[0].statement == "50日均线为4.29。"
    assert "50" not in evidence.source_facts[0].normalized_numeric_tokens
    assert evidence.source_facts[0].calculation_ids == ("close_50_sma",)


def test_conflicting_rows_for_same_indicator_date_do_not_select_a_convenient_value():
    evidence = evaluate("50日均线为4.29。", "2026-10-16,close_50_sma,4.2900",
        content="date,indicator,value\n2026-10-16,close_50_sma,4.2900\n2026-10-16,close_50_sma,9.9900\n")
    assert evidence.claim_validations[0].status.value == "unsupported"


def test_conflicting_artifacts_with_same_source_ref_do_not_select_a_convenient_quote():
    evidence = evaluate("50日均线为4.29。", "2026-10-16,close_50_sma,4.2900",
        additional_results=("date,indicator,value\n2026-10-16,close_50_sma,9.9900\n",))
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("content", [
    "## close_50_sma values from 2026-10-01 to 2026-10-16:\n\n2026-10-16: 4.2900\n2026-10-16: N/A: insufficient history\n",
    "## close_50_sma values from 2026-10-01 to 2026-10-16:\n\n2026-10-15: 9.9900\n\nSource: synthetic\n2026-10-16: 4.2900\n",
])
def test_unavailable_conflict_and_footer_prose_are_not_indicator_observations(content):
    evidence = evaluate("50日均线为4.29。", "2026-10-16: 4.2900", content=content)
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("statement", [
    "50日均线为10。", "50日均线为16。", "50日均线为2026。", "50日均线为50。",
    "50日均线为4.30。", "价格为50。", "50日收益为50%。",
    "10日均线为4.29。", "200日均线为4.29。", "0日均线为4.29。", "50日指数均线为4.29。",
    "50日均线为4.29%。", "50日均线为4.29万元。", "50日均线为4.29美元。",
    "50日均线为4.29股。", "50日均线为4.29手。", "50日均线为50元。",
    "2026年10月15日50日均线为4.29。", "2026-16-10的50日均线为4.29。",
    "50日均线为4.29且价格为50。", "50日均线与200日均线均为4.29。",
])
def test_real_values_units_dates_and_wrong_names_are_not_exempt(statement):
    evidence = evaluate(statement, "2026-10-16,close_50_sma,4.2900")
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("kwargs", [
    {"with_call": False}, {"indicator": "close_200_sma"}, {"indicator": "xclose_50_sma"},
    {"indicator": ["close_50_sma"]}, {"catalog_name": "get_stock_data"}, {"tool": "get_stock_data"},
    {"content": "date,indicator,value,unit\n2026-10-16,close_50_sma,4.2900\n"},
    {"additional_results": ("date,indicator,value\n2026-10-16,close_50_sma,4.2900\n",)},
])
def test_missing_mismatched_schema_or_ambiguous_binding_cannot_exempt_period(kwargs):
    evidence = evaluate("50日均线为4.29。", "2026-10-16,close_50_sma,4.2900", **kwargs)
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("indicator,statement,period", [
    ("close_50_sma", "50日均线为4.29。", 50),
    ("close_200_sma", "200日简单移动平均线为4.29。", 200),
    ("close_10_ema", "10日指数均线为4.29。", 10),
])
@pytest.mark.parametrize("layout", ["csv", "production_text"])
def test_bound_schema_survives_real_submission_and_keeps_history_requirements(indicator, statement, period, layout):
    from tradingagents.agents.analysts.submission import (
        AnalystSubmissionResult,
        build_analyst_update,
    )
    from tradingagents.evidence import EvidenceSource, EvidenceState, EvidenceStatus

    if layout == "csv":
        quote = f"2026-10-16,{indicator},4.2900"
        content = "date,indicator,value\n" + quote + "\n"
    else:
        quote = "2026-10-16: 4.2900"
        content = f"## {indicator} values from 2026-10-01 to 2026-10-16:\n\n{quote}\n\nSource: synthetic\n"
    evidence, messages, claim = evaluate(statement, quote, content=content, indicator=indicator, return_inputs=True)
    assert evidence.claim_validations[0].status.value == "supported"
    result = AnalystSubmissionResult(message=AIMessage(content="合成报告"), report=statement, claims=(claim,),
        submission_source=EvidenceSource(source_id="analyst.market.submission", status=EvidenceStatus.AVAILABLE, required=True))
    update = build_analyst_update({"messages": messages, "company_of_interest": "601328.SS", "trade_date": "2026-10-16",
        "evidence_state": EvidenceState().model_dump(mode="json")}, result, "market_report")
    state = EvidenceState.model_validate(update["evidence_state"])
    assert state.material_claims[0].minimum_history_rows == period
    fact = state.source_facts[0]
    assert fact.fact_kind == "excerpt" and fact.calculation_ids == (indicator,)
    assert fact.canonical_field == "" and fact.normalized_value is None and fact.calculation_lineage is None
    assert state.claim_validations[0].status.value == "supported"


@pytest.mark.parametrize("indicator,statement,period", [
    ("close_50_sma", "50日均线为4.29。", 50),
    ("close_200_sma", "200日均线为4.29。", 200),
    ("close_10_ema", "10日指数均线为4.29。", 10),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_quote_fact_cannot_lose_history_binding_due_to_claim_order(indicator, statement, period, reverse):
    from tradingagents.agents.analysts.submission import (
        AnalystSubmissionResult,
        build_analyst_update,
    )
    from tradingagents.evidence import EvidenceSource, EvidenceState, EvidenceStatus

    quote = f"2026-10-16,{indicator},4.2900"
    _, messages, claim = evaluate(statement, quote, content="date,indicator,value\n" + quote + "\n",
        indicator=indicator, return_inputs=True)
    plain_claim = claim.model_copy(update={"claim_id": "market.plain", "statement": "数值为4.29。"})
    claims = (plain_claim, claim) if reverse else (claim, plain_claim)
    result = AnalystSubmissionResult(message=AIMessage(content="合成报告"), report=statement, claims=claims,
        submission_source=EvidenceSource(source_id="analyst.market.submission", status=EvidenceStatus.AVAILABLE, required=True))
    update = build_analyst_update({"messages": messages, "company_of_interest": "601328.SS", "trade_date": "2026-10-16",
        "evidence_state": EvidenceState().model_dump(mode="json")}, result, "market_report")
    state = EvidenceState.model_validate(update["evidence_state"])
    assert len(state.source_facts) == 1
    assert state.source_facts[0].calculation_ids == (indicator,)
    assert all(c.minimum_history_rows == period for c in state.material_claims)
    assert all(v.status.value == "supported" for v in state.claim_validations)


@pytest.mark.parametrize("content", [
    "## close_50_sma values from 2026-10-01 to 2026-10-16:\n\n2026-10-16: 4.2900\n2026-10-16: 9.9900\n",
    "## close_50_sma values from 2026-10-01 to 2026-10-16:\n\n2026-10-16: 4.2900\n## close_50_sma values from 2026-10-01 to 2026-10-16:\n2026-10-16: 9.9900\n",
    "## close_50_sma values from 2026-10-01 to 2026-10-16:\n2026-10-15: 9.9900\n## rsi values from 2026-10-01 to 2026-10-16:\n2026-10-16: 4.2900\n",
    "## close_50_sma values from 2026-10-01 to 2026-10-16:\n2026-10-15: 9.9900\n## Notes\n2026-10-16: 4.2900\n",
    "## close_50_sma values from 2026-10-17 to 2026-10-18:\n2026-10-16: 4.2900\n",
])
def test_production_text_sections_cannot_launder_conflicts_or_other_indicators(content):
    evidence = evaluate("50日均线为4.29。", "2026-10-16: 4.2900", content=content)
    assert evidence.claim_validations[0].status.value == "unsupported"


def test_citation_must_select_one_complete_row_and_one_indicator():
    content = "date,indicator,value\n2026-10-16,close_50_sma,4.2900\n2026-10-16,close_200_sma,9.9900\n"
    for quote in ["4.2900", content.removeprefix("date,indicator,value\n").strip()]:
        assert evaluate("50日均线为4.29。", quote, content=content,
            indicator="close_50_sma,close_200_sma").claim_validations[0].status.value == "unsupported"
    assert evaluate("2026年10月16日50日均线为4.29。", "2026-10-16,close_50_sma,4.2900", content=content,
        indicator="close_50_sma,close_200_sma").claim_validations[0].status.value == "supported"


def test_non_indicator_claim_keeps_original_precision_rule():
    assert evaluate("数值为4.29。", "2026-10-16,close_50_sma,4.2900").claim_validations[0].status.value == "supported"
    assert evaluate("数值为4.30。", "2026-10-16,close_50_sma,4.2900").claim_validations[0].status.value == "unsupported"

"""A permitted thesis must not borrow a price from calendar components."""
import pytest

from tests.test_indicator_claim_metadata import evaluate
from tradingagents.evidence import DraftThesis, build_inline_evidence_state, evaluate_decision_gate


def evidence_for(quote, statement, path):
    tool, _, claim = evaluate(statement, quote, content=quote,
                              tool="synthetic_numeric", return_inputs=True)
    if path == "tool":
        return tool
    artifact = tool.source_artifacts[0]
    return build_inline_evidence_state({artifact.source_ref: quote}, (claim,),
                                      source_artifact_by_ref={artifact.source_ref: artifact})


@pytest.mark.parametrize("path", ("tool", "inline"))
def test_decision_narrative_cannot_borrow_price_from_date(path):
    evidence = evidence_for("2026-10-16，价格4.29元。", "价格4.29元。", path)
    assert evidence.claim_validations[0].status.value == "supported"
    draft = DraftThesis(rating="Hold", narrative="价格10元。",
                        material_claim_ids=(evidence.material_claims[0].claim_id,))
    assert evaluate_decision_gate(draft, evidence).permitted is False


@pytest.mark.parametrize("path", ("tool", "inline"))
@pytest.mark.parametrize("narrative,extra,permitted", [
    ("价格4.29元。", "", True),
    ("价格4.3元。", "", True),
    ("价格4.30元。", "", False),
    ("价格16元。", "", False),
    ("价格2026元。", "", False),
    ("收益率5%。", " 收益率5%。", True),
    ("收益率5%。", " 数值5。", False),
    ("报告期2026-10-16，价格4.29元。", "", True),
    ("报告期2026年10月16日，价格4.29元。", "", True),
    ("日期2026-10-15。", " 另一个日期2025-09-15。", False),
    ("日期2025-02-29。", " 日期2025-02-29。", False),
    ("报告期2024年2月29日。", " 日期2024-02-29。", True),
    ("价格10元。", " 日期2026年10月16日。", False),
    ("价格999元。", "", False),
])
def test_narrative_keeps_numeric_and_calendar_contracts(path, narrative, extra, permitted):
    quote = "Date 2026-10-16. 价格4.29元。" + extra
    evidence = evidence_for(quote, "价格4.29元。", path)
    assert evidence.claim_validations[0].status.value == "supported"
    before = evidence.model_dump_json()
    draft = DraftThesis(rating="Hold", narrative=narrative,
                        material_claim_ids=(evidence.material_claims[0].claim_id,))
    result = evaluate_decision_gate(draft, evidence)
    assert result.permitted is permitted
    assert draft.rating == "Hold" and draft.narrative == narrative
    assert evidence.model_dump_json() == before


def test_narrative_does_not_borrow_uncited_claim_and_keeps_annotation_rule():
    from tradingagents.evidence import MaterialClaim

    quotes = {"first": "2026-10-16，价格4.29元。", "second": "价格999元。"}
    claims = tuple(MaterialClaim(claim_id=f"market.{i}", analyst="market", statement=quote,
                                source_refs=(ref,), source_quote=quote)
                   for i, (ref, quote) in enumerate(quotes.items(), 1))
    evidence = build_inline_evidence_state(quotes, claims)
    assert all(v.status.value == "supported" for v in evidence.claim_validations)
    for narrative, permitted in (("[market.1] 价格4.29元。", True), ("价格999元。", False)):
        result = evaluate_decision_gate(DraftThesis(rating="Hold", narrative=narrative,
                                material_claim_ids=("market.1",)), evidence)
        assert result.permitted is permitted


def test_non_date_duplicate_diagnostics_and_old_mapping_are_preserved():
    from tradingagents.evidence import EvidenceState

    evidence = evidence_for("2026-10-16，价格4.29元。", "价格4.29元。", "tool")
    legacy = evidence.model_dump(mode="json")
    for validation in legacy["claim_validations"]:
        validation.pop("comparison_evidence", None)
    evidence = EvidenceState.model_validate(legacy)
    draft = DraftThesis(rating="Hold", narrative="数值999和999。",
                        material_claim_ids=(evidence.material_claims[0].claim_id,))
    result = evaluate_decision_gate(draft, evidence)
    assert result.permitted is False
    assert result.diagnostics == ("Draft narrative contains unsupported numeric claim: 999.",) * 2
    assert evidence.claim_validations[0].comparison_evidence is None


def test_binding_failure_precedes_narrative_matching():
    evidence = evidence_for("2026-10-16，价格4.29元。", "价格4.29元。", "tool")
    broken = evidence.model_copy(update={"source_artifacts": ()})
    draft = DraftThesis(rating="Hold", narrative="价格4.29元。",
                        material_claim_ids=(evidence.material_claims[0].claim_id,))
    result = evaluate_decision_gate(draft, broken)
    assert result.permitted is False
    assert any("Source Artifact" in detail for detail in result.diagnostics)

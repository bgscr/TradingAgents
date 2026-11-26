"""Calendar components cannot substantiate an unrelated financial value."""
import pytest

from tests.test_indicator_claim_metadata import evaluate
from tradingagents.evidence import build_inline_evidence_state


@pytest.mark.parametrize("path", ("tool", "inline"))
def test_date_component_cannot_support_a_price(path):
    quote = "2026-10-16，价格为4.29元。"
    tool, _, claim = evaluate("价格为10元。", quote, content=quote,
                              tool="synthetic_numeric", return_inputs=True)
    artifact = tool.source_artifacts[0]
    evidence = tool if path == "tool" else build_inline_evidence_state(
        {artifact.source_ref: quote}, (claim,), source_artifact_by_ref={artifact.source_ref: artifact})
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("path", ("tool", "inline"))
@pytest.mark.parametrize("statement,quote,status", [
    ("价格为10元。", "2026年10月16日，价格4.29元。", "unsupported"),
    ("价格为2026元。", "2026-10-16，价格4.29元。", "unsupported"),
    ("价格为16元。", "2026-10-16，价格4.29元。", "unsupported"),
    ("价格为10元。", "2026-10-16，价格10元。", "supported"),
    ("价格为4.29元。", "2026-10-16，价格4.294元。", "supported"),
    ("价格为4.29元。", "2026-10-16，价格4.296元。", "unsupported"),
    ("收益率10%。", "2026-10-16，收益率4%。", "unsupported"),
    ("收益率10%。", "2026-10-16，收益率10%。", "supported"),
    ("日期为2026-10-16。", "日期2026-10-16。", "supported"),
    ("日期为2026年10月16日。", "日期2026-10-16。", "supported"),
    ("日期为2026-10-16。", "日期2026年10月16日。", "supported"),
    ("日期为2026-10-16。", "年份2026，价格10，数量16。", "unsupported"),
    ("日期为2026-10-16。", "日期2026-10-15及2025-09-16。", "unsupported"),
    ("日期为2026-02-30。", "日期2026-02-30。", "unsupported"),
    ("日期为2026-10-16。", "序列2026-10-16.5。", "unsupported"),
    ("数值为16.5。", "序列2026-10-16.5。", "supported"),
    ("日期为2024年2月29日。", "日期2024-02-29。", "supported"),
    ("日期为2025-02-29。", "日期2025-02-29。", "unsupported"),
    ("价格为30元。", "日期2026-02-30，价格4元。", "unsupported"),
    ("2026-10-16价格为10元。", "2026-10-16价格4.29元。", "unsupported"),
    ("2026-10-16价格为4.29元。", "2026-10-16价格4.29元。", "supported"),
    ("价格为10元。", "无可核对数字。", "unsupported"),
    ("价格为111元。", "价格1，2026-10-16，数量11。", "unsupported"),
])
def test_date_and_value_roles_keep_independent_contracts(path, statement, quote, status):
    tool, _, claim = evaluate(statement, quote, content=quote,
                              tool="synthetic_numeric", return_inputs=True)
    artifact = tool.source_artifacts[0]
    evidence = tool if path == "tool" else build_inline_evidence_state(
        {artifact.source_ref: quote}, (claim,), source_artifact_by_ref={artifact.source_ref: artifact})
    assert evidence.claim_validations[0].status.value == status
    assert evidence.material_claims[0].statement == statement


def test_indicator_mismatch_stays_unsupported_without_date_token_match():
    evidence = evaluate("50日均线为10。", "2026-10-16,close_50_sma,4.2900")
    validation = evidence.claim_validations[0]
    assert validation.status.value == "unsupported"
    checks = validation.comparison_evidence.checks
    assert any(c.scope == "indicator_observation" and not c.matched for c in checks)
    assert not any(c.claim_token == "10" and c.reference_token == "10" for c in checks)


def test_inline_full_source_keeps_legitimate_amount_but_not_date_only():
    quote = "价格4元。"
    for other, status in (("2026-10-16", "unsupported"), ("另一价格10元。", "supported")):
        tool, _, claim = evaluate("价格10元。", quote, content=quote + other,
                                  tool="synthetic_numeric", return_inputs=True)
        artifact = tool.source_artifacts[0]
        assert tool.claim_validations[0].status.value == "unsupported"
        inline = build_inline_evidence_state({artifact.source_ref: artifact.raw_text}, (claim,),
                    source_artifact_by_ref={artifact.source_ref: artifact})
        assert inline.claim_validations[0].status.value == status


def test_separate_source_claims_do_not_borrow_a_value():
    from tradingagents.evidence import MaterialClaim

    claims = tuple(MaterialClaim(claim_id=f"synthetic.{ref}", analyst="market", statement="价格10元。",
                                 source_refs=(ref,), source_quote="行情记录")
                   for ref in ("first", "second"))
    evidence = build_inline_evidence_state(
        {"first": "行情记录 2026-10-16 价格4元。", "second": "行情记录 价格10元。"}, claims)
    # Preserve existing per-source validation; one convenient source does not
    # erase another cited source's mismatch.
    assert [v.status.value for v in evidence.claim_validations] == ["unsupported", "supported"]


def test_missing_reference_and_legacy_claim_keep_closed_behavior():
    from tradingagents.evidence import MaterialClaim

    claim = MaterialClaim.model_validate_json(
        '{"claim_id":"synthetic.old","analyst":"market","statement":"价格10元。",'
        '"source_refs":["missing"],"source_quote":"2026-10-16"}')
    evidence = build_inline_evidence_state({}, (claim,))
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("path", ("tool", "inline"))
def test_date_before_sentence_period_cannot_support_price(path):
    quote = "Date 2026-10-16. Price 4.29."
    tool, _, claim = evaluate("Price 10.", quote, content=quote,
                              tool="synthetic_numeric", return_inputs=True)
    artifact = tool.source_artifacts[0]
    evidence = tool if path == "tool" else build_inline_evidence_state(
        {artifact.source_ref: quote}, (claim,), source_artifact_by_ref={artifact.source_ref: artifact})
    assert evidence.claim_validations[0].status.value == "unsupported"

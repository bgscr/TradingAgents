"""Synthetic acquisition times must not substantiate unrelated financial values."""
import pytest

from tests.test_financial_observation_binding import CONTENT, ROW, submit_row
from tests.test_narrative_date_roles import evidence_for
from tradingagents.evidence import DraftThesis, evaluate_decision_gate


@pytest.mark.parametrize("amount", ("36", "5.908460", "0"))
def test_financial_submission_does_not_borrow_timestamp_components(amount):
    row = ROW.replace("T00:00:00+00:00", "T17:36:05.908460+00:00")
    evidence, _ = submit_row(
        content=CONTENT.replace(ROW, row), quote=row,
        statement=f"Net income was {amount} CNY for 2024-12-31.",
    )
    # Exercise actual analyst submission and source-row binding, not a regex helper.
    observation = evidence.source_facts[0].financial_observation
    assert observation is not None and observation.value == "123.45"
    assert evidence.claim_validations[0].status.value == "unsupported"


@pytest.mark.parametrize("path", ("tool", "inline"))
@pytest.mark.parametrize("statement,quote,status", [
    ("价格36元。", "2026-10-16T17:36:05.908460+09:45，价格4.29元。", "unsupported"),
    ("价格5.908460元。", "2026-10-16T17:36:05.908460+09:45，价格4.29元。", "unsupported"),
    ("价格45元。", "2026-10-16T17:36:05.908460+09:45，价格4.29元。", "unsupported"),
    ("价格36元。", "采集时刻17:36:05，价格4.29元。", "unsupported"),
    ("价格36元。", "2026-10-16 17:36:05Z，价格36元。", "supported"),
    ("价格4.29元。", "2026-10-16T17:36:05+00:00，价格4.294元。", "supported"),
    ("价格4.29元。", "2026-10-16T17:36:05+00:00，价格4.296元。", "unsupported"),
    ("收益率36%。", "时刻17:36:05，收益率4%。", "unsupported"),
    ("收益率36%。", "时刻17:36:05，收益率36%。", "supported"),
    ("报告期2026-10-16。", "2026-10-16T17:36:05Z，价格4.29元。", "supported"),
    ("时刻17:36:05Z。", "2026-10-16T17:36:05+00:00，价格4.29元。", "supported"),
    ("时刻17:36:05Z。", "2026-10-16T17:36:04Z，价格4.29元。", "unsupported"),
    ("时间2026-10-16T17:36:05Z。", "2026-10-16 17:36:05+00:00，价格4.29元。", "supported"),
    ("时间2026-10-16T17:36:05Z。", "2026-10-16T17:40:08Z，2026-10-15T17:36:05Z。", "unsupported"),
    ("时刻17:36:05。", "数值17、36、5。", "unsupported"),
    ("时间2026-02-30T17:36:05Z。", "2026-02-30T17:36:05Z。", "unsupported"),
    ("时刻25:36:05。", "时刻25:36:05。", "unsupported"),
    ("价格36元。", "时刻25:36:05，价格4.29元。", "unsupported"),
    ("时刻17:36:05.1234567Z。", "时刻17:36:05.1234568Z。", "unsupported"),
    ("时刻17:36:05.90Z。", "时刻17:36:05.900+00:00。", "supported"),
    ("价格111元。", "1 17:36:05 11。", "unsupported"),
    ("时刻17:36:05+09:99。", "时刻17:36:05+09:99。", "unsupported"),
    ("时刻17:36:05+24:00。", "时刻17:36:05+24:00。", "unsupported"),
    ("时间2026-10-16T17:36:05+00:00。", "时间2026-10-16T17:36:05+01:00。", "unsupported"),
])
def test_timestamp_role_and_ordinary_value_contracts(path, statement, quote, status):
    evidence = evidence_for(quote, statement, path)
    assert evidence.claim_validations[0].status.value == status
    assert evidence.material_claims[0].statement == statement


@pytest.mark.parametrize("path", ("tool", "inline"))
@pytest.mark.parametrize("narrative,permitted", [
    ("价格36元。", False), ("价格45元。", False),
    ("价格4.29元。", True), ("价格4.3元。", True), ("价格4.30元。", False),
    ("报告期2026-10-16。", True),
    ("时刻17:36:05+09:45。", True), ("时刻17:36:06+09:45。", False),
])
def test_independent_narrative_gate_keeps_time_and_amount_roles(path, narrative, permitted):
    evidence = evidence_for("2026-10-16T17:36:05+09:45，价格4.29元。", "价格4.29元。", path)
    before = evidence.model_dump_json()
    draft = DraftThesis(rating="Hold", narrative=narrative,
                        material_claim_ids=(evidence.material_claims[0].claim_id,))
    assert evaluate_decision_gate(draft, evidence).permitted is permitted
    assert draft.rating == "Hold" and evidence.model_dump_json() == before


def test_financial_valid_value_keeps_observation_and_comparison_scope():
    row = ROW.replace("T00:00:00+00:00", "T17:36:05.908460+00:00")
    evidence, _ = submit_row(content=CONTENT.replace(ROW, row), quote=row)
    validation = evidence.claim_validations[0]
    assert validation.status.value == "supported"
    observation = evidence.source_facts[0].financial_observation
    assert observation.value == "123.45" and not observation.decision_qualified
    checks = validation.comparison_evidence.checks
    assert checks and all(check.scope == "tool_excerpt" for check in checks)
    assert any(check.matched and check.reference_token == "123.45" for check in checks)
    assert not any(check.reference_token in {"36", "05.908460", "00"} for check in checks)


def test_inline_full_source_and_tool_excerpt_keep_separate_pools():
    from tests.test_indicator_claim_metadata import evaluate
    from tradingagents.evidence import build_inline_evidence_state

    quote = "价格4.29元。"
    for extra, expected in ((" 时刻17:36:05。", "unsupported"), (" 价格36元。", "supported")):
        tool, _, claim = evaluate("价格36元。", quote, content=quote + extra,
                                  tool="synthetic_numeric", return_inputs=True)
        artifact = tool.source_artifacts[0]
        assert tool.claim_validations[0].status.value == "unsupported"
        inline = build_inline_evidence_state({artifact.source_ref: artifact.raw_text}, (claim,),
                         source_artifact_by_ref={artifact.source_ref: artifact})
        assert inline.claim_validations[0].status.value == expected
        assert all(c.scope == "inline_source"
                   for c in inline.claim_validations[0].comparison_evidence.checks)

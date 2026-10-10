"""Mock final states reproduce the missing stages without calling any model."""

import copy
import json

import pytest

from tests.report_export_helpers import export_root
from tests.test_reporting import _decision_state
from tradingagents.evidence import ClaimValidation, EvidenceSource
from tradingagents.evidence_artifacts import AuditClaimValidation, AuditEvidenceSource
from tradingagents.evidence_diagnostics import (
    PublicSourceDiagnostic,
    claim_diagnostic,
    source_diagnostic,
)
from tradingagents.report_presentation import advisory_sections
from tradingagents.reporting import write_report_tree


def nine_role_state():
    state = _decision_state()
    state["investment_debate_state"].update(bull_history="多头原文", bear_history="空头原文")
    state["risk_debate_state"].update(
        aggressive_history="激进原文", conservative_history="保守原文", neutral_history="中立原文"
    )
    return state


def test_chinese_market_only_export_preserves_all_nine_advisory_roles(tmp_path):
    state = nine_role_state()
    decision = copy.deepcopy(state["trading_decision"])
    legacy = tmp_path / "market_report.md"
    legacy.write_text("already captured stage", encoding="utf-8")
    complete = write_report_tree(state, "AAPL", tmp_path, output_language="中文").read_text(
        encoding="utf-8"
    )
    version_root = export_root(tmp_path)
    authoritative = (version_root / "5_portfolio/decision.md").read_text(encoding="utf-8")
    assert len(advisory_sections(state)) == 9
    for relative, _, _, content in advisory_sections(state):
        saved = (version_root / relative).read_text(encoding="utf-8")
        assert "辅助分析（模型生成，未经事实验证）" in saved
        assert content in saved and content in complete
        assert content not in authoritative
    assert "# 交易分析报告" in complete
    assert "# 经验证的交易决定" in authoritative
    assert "**评级:** 买入" in authoritative
    assert "# Validated Trading Decision" not in authoritative
    assert state["trading_decision"] == decision
    assert legacy.read_text(encoding="utf-8") == "already captured stage"


def test_presentation_language_does_not_change_decision_or_audit(tmp_path):
    english, chinese = _decision_state(), _decision_state()
    for state in (english, chinese):
        state["decision_audit_created_at"] = "2026-07-20T12:30:00+00:00"
    write_report_tree(english, "AAPL", tmp_path / "en")
    write_report_tree(chinese, "AAPL", tmp_path / "zh", output_language="Chinese")
    assert english["trading_decision"] == chinese["trading_decision"]
    assert (tmp_path / "en/decision-audit.json").read_bytes() == (
        tmp_path / "zh/decision-audit.json"
    ).read_bytes()


def test_typed_diagnostics_publish_safe_reason_without_raw_detail(tmp_path):
    state = _decision_state()
    evidence = state["evidence_state"]
    evidence["sources"].append(
        EvidenceSource(
            source_id="get_indicators:private?token=do-not-publish",
            status="unavailable",
            required=False,
            detail="no matching tool result",
        ).model_dump(mode="json")
    )
    evidence["claim_validations"] = [
        ClaimValidation(
            claim_id="market.rsi",
            status="unsupported",
            fact_ids=(),
            detail="source quote is absent from the cited tool result",
        ).model_dump(mode="json")
    ]
    # Audit cannot contain a validation whose associated claim is absent.
    from tradingagents.evidence import MaterialClaim

    evidence["material_claims"] = [
        MaterialClaim(
            claim_id="market.rsi",
            analyst="market",
            statement="Unverified RSI",
            source_refs=("get_indicators:private?token=do-not-publish",),
            source_quote="missing",
        ).model_dump(mode="json")
    ]
    complete = write_report_tree(state, "AAPL", tmp_path, output_language="Chinese").read_text(
        encoding="utf-8"
    )
    audit = json.loads((tmp_path / "decision-audit.json").read_text(encoding="utf-8"))
    diagnostic = audit["evidence_state"]["sources"]
    assert any(
        row.get("diagnostic", {}).get("display_name") == "Technical indicators"
        for row in diagnostic
    )
    assert "tool_result_missing" in complete and "source_quote_missing" in complete
    assert "未支持主张：1 条" in complete
    assert "do-not-publish" not in complete + json.dumps(audit)


def test_unknown_error_text_cannot_become_a_public_diagnostic():
    source = EvidenceSource(
        source_id="https://private/?token=secret",
        status="unavailable",
        required=False,
        detail="secret raw exception",
    )
    public = source_diagnostic(source).model_dump_json()
    assert "secret" not in public and "https" not in public
    assert "details_not_retained" in public
    with pytest.raises(ValueError):
        PublicSourceDiagnostic(
            display_name="secret", capability="unknown", reason="details_not_retained"
        )


@pytest.mark.parametrize("location", ("tool result", "source block"))
@pytest.mark.parametrize(
    "condition,code",
    (("absent from", "source_quote_missing"), ("ambiguous within", "source_quote_ambiguous")),
)
def test_both_evidence_binding_paths_keep_specific_quote_failure_reason(location, condition, code):
    validation = ClaimValidation(
        claim_id="claim",
        status="unsupported",
        fact_ids=(),
        detail=f"source quote is {condition} the cited {location}",
    )
    assert claim_diagnostic(validation).value == code


def test_old_audit_source_and_validation_remain_readable_without_new_fields():
    source = {
        "contract_version": "1.0",
        "source_id": "source=sha256:" + "a" * 64,
        "status": "unavailable",
        "required": False,
    }
    claim = {"contract_version": "1.0", "claim_id": "old", "status": "unsupported", "fact_ids": []}
    assert (
        AuditEvidenceSource.model_validate_json(json.dumps(source)).model_dump(mode="json")
        == source
    )
    assert (
        AuditClaimValidation.model_validate_json(json.dumps(claim)).model_dump(mode="json") == claim
    )

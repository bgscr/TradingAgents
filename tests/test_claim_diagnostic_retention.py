"""Public diagnostics preserve validator reasons, never arbitrary detail text."""
import json

import pytest

from tests.test_indicator_claim_metadata import evaluate, synthetic_snapshot_evaluate
from tradingagents.evidence import ClaimValidation, EvidenceState, evaluate_admission_gate
from tradingagents.evidence_artifacts import (
    AuditClaimValidation,
    AuditEvidenceProjection,
    SourceArtifactManifest,
    SourceArtifactManifestEntry,
    project_evidence_for_audit,
)
from tradingagents.evidence_diagnostics import claim_diagnostic
from tradingagents.report_presentation import evidence_diagnostic_markdown


def test_bound_indicator_conflict_survives_audit_and_report_without_changing_claim():
    evidence = evaluate("200日均线为4.29。", "2026-10-16,close_50_sma,4.2900")
    validation = evidence.claim_validations[0]
    assert validation.status.value == "unsupported"
    assert validation.detail == "indicator name metadata conflicts with the cited indicator"
    before = evidence.model_dump(mode="json")
    manifest = SourceArtifactManifest(artifacts=tuple(
        SourceArtifactManifestEntry(artifact_ref="artifact=sha256:" + a.artifact_sha256,
                                    byte_length=len(a.raw_text.encode("utf-8")))
        for a in evidence.source_artifacts
    ))
    audit = project_evidence_for_audit(evidence, manifest)
    assert audit.claim_validations[0].diagnostic_code.value == "indicator_identity_conflict"
    report = evidence_diagnostic_markdown(evidence, "中文")
    assert "指标名称或周期与引用指标不一致" in report
    assert "未支持主张：1 条" in report
    assert "details_not_retained" not in report
    assert evidence.model_dump(mode="json") == before
    assert evidence.material_claims[0].statement == "200日均线为4.29。"
    assert "indicator_identity_conflict" in json.dumps(audit.model_dump(mode="json"))


@pytest.mark.parametrize("detail,code", [
    ("indicator name metadata is not bound to one unambiguous observation", "indicator_binding_unverified"),
    ("indicator name metadata is not bound to one unambiguous unit-free observation", "indicator_binding_unverified"),
    ("indicator observation date conflicts with the cited row", "indicator_date_conflict"),
    ("indicator observation units are not established by the cited row", "indicator_units_unverified"),
    ("indicator observation value is absent from the dated claim", "indicator_value_missing"),
    ("latest indicator observation requires a bound latest snapshot row", "indicator_latest_binding_missing"),
    ("indicator observation date is invalid", "indicator_date_invalid"),
    ("snapshot observation date conflicts with the cited row", "snapshot_date_conflict"),
    ("snapshot date is not an explicit observation-date span", "snapshot_date_unbound"),
    ("snapshot observation value is absent from the dated claim", "snapshot_value_missing"),
])
@pytest.mark.parametrize("language", ["中文", "English"])
def test_fixed_validator_reasons_survive_public_projection(detail, code, language):
    evidence = EvidenceState(claim_validations=(ClaimValidation(
        claim_id="synthetic.diagnostic", status="unsupported", detail=detail,
    ),))
    before = evidence.model_dump(mode="json")
    gate = evaluate_admission_gate(evidence).model_dump(mode="json")
    projected = project_evidence_for_audit(evidence, SourceArtifactManifest())
    assert projected.claim_validations[0].diagnostic_code.value == code
    assert AuditEvidenceProjection.model_validate_json(projected.model_dump_json()) == projected
    rendered = evidence_diagnostic_markdown(evidence, language)
    assert code in rendered and "details_not_retained" not in rendered
    assert evidence.model_dump(mode="json") == before
    assert evaluate_admission_gate(evidence).model_dump(mode="json") == gate


@pytest.mark.parametrize("detail,expected", [
    ("", "details_not_retained"),
    ("unrecognized reason SECRET_SENTINEL", "details_not_retained"),
    ("indicator observation date conflicts with the cited row SECRET_SENTINEL", "details_not_retained"),
    ("indicator name metadata conflicts with the cited indicator\nSECRET_SENTINEL", "details_not_retained"),
    ("source does not contain numeric claim(s): SECRET_SENTINEL", "numeric_claim_unsupported"),
    ("source quote is absent from the cited tool result", "source_quote_missing"),
    ("source quote is ambiguous within the cited source block", "source_quote_ambiguous"),
])
def test_legacy_and_unknown_reasons_do_not_publish_free_text(detail, expected):
    validation = ClaimValidation(claim_id="synthetic.safe", status="unsupported",
                                 detail=detail, fact_ids=("fact:synthetic",))
    assert claim_diagnostic(validation).value == expected
    evidence = EvidenceState(claim_validations=(validation,))
    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())
    public = audit.model_dump_json() + evidence_diagnostic_markdown(evidence, "中文")
    assert "SECRET_SENTINEL" not in public
    assert audit.claim_validations[0].status.value == "unsupported"
    assert audit.claim_validations[0].fact_ids == validation.fact_ids


def test_old_audit_missing_reason_and_old_unknown_code_are_not_reconstructed():
    for extra in ({}, {"diagnostic_code": "details_not_retained"}):
        old = {"contract_version": "1.0", "claim_id": "old.sma", "status": "unsupported",
               "fact_ids": [], **extra}
        assert AuditClaimValidation.model_validate_json(json.dumps(old)).model_dump(mode="json") == old


@pytest.mark.parametrize("statement,code", [
    ("2026年10月15日50日均线为4.29。", "indicator_date_conflict"),
    ("50-day SMA is 4.29 CNY", "indicator_units_unverified"),
    ("最新50日均线为4.29。", "indicator_latest_binding_missing"),
    ("50日均线为4.30。", "numeric_claim_unsupported"),
])
def test_real_validator_path_preserves_reason_and_failed_admission(statement, code):
    # English unit validation requires a bound snapshot/indicator context before
    # reaching its unit check; a bare CSV legitimately fails binding first.
    evidence = (synthetic_snapshot_evaluate(statement, "close_50_sma")
                if code == "indicator_units_unverified" else
                evaluate(statement, "2026-10-16,close_50_sma,4.2900"))
    validation = evidence.claim_validations[0]
    assert validation.status.value == "unsupported"
    assert claim_diagnostic(validation).value == code
    assert code in evidence_diagnostic_markdown(evidence, "中文")
    assert not evaluate_admission_gate(evidence).admitted
    assert evidence.material_claims[0].statement == statement


def test_diagnostic_rendering_keeps_rule_rating_and_opposing_advisory_text():
    from tests.test_reporting import _validated_components
    from tradingagents.report_presentation import advisory_markdown

    _, decision, _ = _validated_components()
    before = decision.model_dump(mode="json")
    evidence = evaluate("200日均线为4.29。", "2026-10-16,close_50_sma,4.2900")
    commentary = "合成辅助建议：卖出；仅供讨论。"
    report = advisory_markdown("合成研究", commentary, "中文", decision=decision, evidence=evidence)
    assert commentary in report
    assert "买入 (Buy)" in report
    assert "未支持主张：1" in report
    assert decision.model_dump(mode="json") == before

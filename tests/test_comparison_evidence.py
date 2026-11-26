"""Comparison diagnostics describe execution; they never supply decision facts."""
import json

import pytest
from pydantic import ValidationError

from tests.test_indicator_claim_metadata import evaluate
from tradingagents.comparison_evidence import NumericComparisonEvidence, comparison_markdown
from tradingagents.evidence import (
    ClaimValidation,
    EvidenceState,
    build_inline_evidence_state,
    evaluate_admission_gate,
)
from tradingagents.evidence_artifacts import AuditClaimValidation
from tradingagents.report_presentation import evidence_diagnostic_markdown


def test_tool_comparison_records_executed_pairs():
    evidence = evaluate("数值为5。", "数值4。", content="数值4。\n其它5。",
                        tool="synthetic_numeric")
    validation = evidence.claim_validations[0]
    assert validation.status.value == "unsupported"
    trace = validation.comparison_evidence
    assert trace.status == "recorded"
    assert trace.financial_unit_status == "unverified"
    assert len(trace.checks) == 1
    check = trace.checks[0]
    assert (check.scope, check.claim_token, check.reference_token, check.method,
            check.absolute_tolerance, check.matched) == (
                "tool_excerpt", "5", "4", "exact", None, False)
    assert check.artifact_sha256 == evidence.source_artifacts[0].artifact_sha256


def test_inline_records_full_source_and_stops_after_actual_match():
    tool, _, claim = evaluate("数值为5。", "数值4。", content="数值4。\n其它5。\n其它9。",
                              tool="synthetic_numeric", return_inputs=True)
    artifact = tool.source_artifacts[0]
    inline = build_inline_evidence_state({artifact.source_ref: artifact.raw_text}, (claim,),
                                        source_artifact_by_ref={artifact.source_ref: artifact})
    assert inline.claim_validations[0].status.value == "supported"
    checks = inline.claim_validations[0].comparison_evidence.checks
    assert [(x.scope, x.reference_token, x.matched) for x in checks] == [
        ("inline_source", "4", False), ("inline_source", "5", True)]
    assert all(x.artifact_sha256 == artifact.artifact_sha256 for x in checks)


def test_observation_failure_does_not_compare_unrelated_date_components():
    evidence = evaluate("50日均线为10。", "2026-10-16,close_50_sma,4.2900")
    validation = evidence.claim_validations[0]
    assert validation.status.value == "unsupported"
    checks = validation.comparison_evidence.checks
    assert any(x.scope == "indicator_observation" and x.claim_token == "10"
               and x.reference_token == "4.2900" and not x.matched for x in checks)
    assert not any(x.scope == "tool_excerpt" and x.reference_token in {"2026", "10", "16"}
                   for x in checks)


def test_audit_and_appendix_preserve_optional_execution_diagnostics():
    from tradingagents.evidence_artifacts import (
        SourceArtifactManifest,
        SourceArtifactManifestEntry,
        project_evidence_for_audit,
    )

    evidence = evaluate("数值为5。", "数值4。", content="数值4。", tool="synthetic_numeric")
    manifest = SourceArtifactManifest(artifacts=tuple(SourceArtifactManifestEntry(
        artifact_ref="artifact=sha256:" + a.artifact_sha256,
        byte_length=len(a.raw_text.encode("utf-8"))) for a in evidence.source_artifacts))
    projected = project_evidence_for_audit(evidence, manifest)
    validation = projected.claim_validations[0]
    assert validation.comparison_evidence == evidence.claim_validations[0].comparison_evidence
    assert AuditClaimValidation.model_validate_json(validation.model_dump_json()) == validation
    appendix = evidence_diagnostic_markdown(evidence, "中文")
    assert "金融单位未核实" in appendix
    assert "5" in appendix and "4" in appendix
    assert "数值为5。" not in appendix


@pytest.mark.parametrize("statement,quote,method,tolerance,matched,status", [
    ("值5.00", "值5.00", "exact", None, True, "supported"),
    ("值5.00", "值5.004", "absolute_tolerance", "0.005", True, "supported"),
    ("值5.00", "值5.006", "absolute_tolerance", "0.005", False, "unsupported"),
    ("值5%", "值5", "percent_mismatch", None, False, "unsupported"),
    ("值5", "无数字", "no_reference", None, False, "unsupported"),
    ("金额5亿元", "金额5万元", "exact", None, True, "supported"),
])
def test_actual_method_tolerance_and_unknown_financial_unit(statement, quote, method, tolerance, matched, status):
    evidence = evaluate(statement, quote, content=quote, tool="synthetic_numeric")
    validation = evidence.claim_validations[0]
    assert validation.status.value == status
    trace = validation.comparison_evidence
    assert trace.financial_unit_status == "unverified"
    assert (trace.checks[0].method, trace.checks[0].absolute_tolerance, trace.checks[0].matched) == (
        method, tolerance, matched)


@pytest.mark.parametrize("extra,reason", [
    (" token=SECRET_SENTINEL_123456", "sensitive_input"),
    (" Cookie: PRIVATE_SENTINEL_123456", "sensitive_input"),
    (" https://synthetic.invalid/SECRET_SENTINEL", "sensitive_input"),
    ("x" * 17000, "input_limit"),
    (" 12345678901234567", "numeric_limit"),
])
def test_sensitive_oversized_or_numeric_limit_retains_no_values(extra, reason):
    quote = "值4" + extra
    evidence = evaluate("值5", quote, content=quote, tool="synthetic_numeric")
    trace = evidence.claim_validations[0].comparison_evidence
    assert trace.status == "unavailable" and trace.unavailable_reason == reason
    assert not trace.checks
    public = trace.model_dump_json() + "".join(comparison_markdown(trace, chinese=True))
    assert "SENTINEL" not in public and "123456" not in public


def test_bounded_check_limit_does_not_publish_a_partial_trace():
    quote = " ".join(str(i) for i in range(1, 33))
    evidence = evaluate("值101 102 103 104 105", quote, content=quote, tool="synthetic_numeric")
    validation = evidence.claim_validations[0]
    assert validation.status.value == "unsupported"
    assert validation.comparison_evidence.unavailable_reason == "check_limit"
    assert not validation.comparison_evidence.checks


def test_unknown_binding_does_not_fabricate_reference_identity():
    evidence = evaluate("值5", "不存在的引用4", content="值4", tool="synthetic_numeric")
    assert evidence.claim_validations[0].status.value == "unsupported"
    assert evidence.claim_validations[0].comparison_evidence.status == "unavailable"


def test_legacy_claim_and_audit_missing_diagnostics_stay_missing():
    old = {"claim_id": "synthetic.old", "status": "unsupported", "fact_ids": []}
    for model in (ClaimValidation, AuditClaimValidation):
        parsed = model.model_validate_json(json.dumps(old))
        assert parsed.comparison_evidence is None
        assert "comparison_evidence" not in parsed.model_dump(mode="json")


@pytest.mark.parametrize("payload", [
    {"status": "recorded", "checks": [], "raw_body": "SECRET_SENTINEL"},
    {"status": "unavailable", "unavailable_reason": "SECRET_SENTINEL"},
    {"status": "unavailable", "unavailable_reason": "input_limit", "financial_unit_status": "CNY"},
    {"status": "recorded", "checks": [{"scope": "inline_source", "artifact_sha256": "0" * 64,
      "claim_token": "<script>SECRET_SENTINEL</script>", "reference_token": "4",
      "method": "exact", "matched": False}]},
])
def test_untrusted_comparison_shape_is_closed(payload):
    with pytest.raises(ValidationError):
        NumericComparisonEvidence.model_validate_json(json.dumps(payload))


def test_diagnostics_roundtrip_and_removal_do_not_change_admission_or_claims():
    evidence = evaluate("数值为5。", "数值4。", content="数值4。", tool="synthetic_numeric")
    restored = EvidenceState.model_validate_json(evidence.model_dump_json())
    assert restored == evidence
    assert EvidenceState.model_validate(evidence.model_dump(mode="json")) == evidence
    without = evidence.model_copy(update={"claim_validations": tuple(
        v.model_copy(update={"comparison_evidence": None}) for v in evidence.claim_validations)})
    assert evaluate_admission_gate(evidence) == evaluate_admission_gate(without)
    assert evidence.material_claims == without.material_claims
    assert evidence.source_facts == without.source_facts


def test_current_cache_source_change_still_denies_old_proof(tmp_path, monkeypatch):
    from tradingagents.market_history import current_cache as cache

    manifest = json.loads(cache.MANIFEST_PATH.read_text(encoding="utf-8"))
    local = tmp_path / "synthetic-manifest.json"
    monkeypatch.setattr(cache, "MANIFEST_PATH", local)
    for name in cache.SOURCE_FILES:
        manifest["sources"].setdefault(name, "f" * 64)
    local.write_text(json.dumps(manifest), encoding="utf-8")
    fingerprints = {name: manifest["sources"][name] for name in cache.SOURCE_FILES}
    monkeypatch.setattr(cache, "source_fingerprints", lambda **kwargs: fingerprints)
    assert cache.compatibility_diagnostic() is None
    fingerprints["tradingagents/evidence.py"] = "0" * 64
    assert cache.compatibility_diagnostic().startswith("current_cache_gate_denied:")


def test_sensitive_marker_outside_excerpt_suppresses_entire_trace():
    evidence = evaluate("值123456", "值4", content="值4\nToken=123456", tool="synthetic_numeric")
    trace = evidence.claim_validations[0].comparison_evidence
    assert trace.status == "unavailable" and trace.unavailable_reason == "sensitive_input"
    assert "123456" not in trace.model_dump_json()


def test_surrogate_in_claim_does_not_break_existing_numeric_verdict():
    evidence = evaluate("值5\ud800", "值4", content="值4", tool="synthetic_numeric")
    assert evidence.claim_validations[0].status.value == "unsupported"
    assert evidence.claim_validations[0].comparison_evidence.status == "unavailable"


def test_unbound_trace_digest_is_not_published_as_execution_evidence():
    from tradingagents.evidence_artifacts import (
        SourceArtifactManifest,
        SourceArtifactManifestEntry,
        project_evidence_for_audit,
    )

    evidence = evaluate("值5", "值4", content="值4", tool="synthetic_numeric")
    original = evidence.claim_validations[0]
    check = original.comparison_evidence.checks[0].model_copy(update={"artifact_sha256": "0" * 64})
    forged = original.comparison_evidence.model_copy(update={"checks": (check,)})
    evidence = evidence.model_copy(update={"claim_validations": (
        original.model_copy(update={"comparison_evidence": forged}),)})
    manifest = SourceArtifactManifest(artifacts=tuple(SourceArtifactManifestEntry(
        artifact_ref="artifact=sha256:" + a.artifact_sha256,
        byte_length=len(a.raw_text.encode("utf-8"))) for a in evidence.source_artifacts))
    projected = project_evidence_for_audit(evidence, manifest)
    assert projected.claim_validations[0].comparison_evidence.status == "unavailable"
    assert "0" * 64 not in evidence_diagnostic_markdown(evidence, "中文")


@pytest.mark.parametrize("overrides", [
    {"claim_token": "4", "reference_token": "4", "method": "exact", "matched": False},
    {"claim_token": "4.00", "reference_token": "4.1", "method": "absolute_tolerance",
     "absolute_tolerance": "0.5", "matched": True},
    {"claim_token": "4", "reference_token": "4", "method": "percent_mismatch", "matched": False},
])
def test_internally_contradictory_pair_is_rejected(overrides):
    check = {"scope": "tool_excerpt", "artifact_sha256": "0" * 64, **overrides}
    with pytest.raises(ValidationError):
        NumericComparisonEvidence.model_validate_json(json.dumps({"status": "recorded", "checks": [check]}))


@pytest.mark.parametrize("changes", [
    {"tool_call_id": "different-call"}, {"source_ref": "different-source"},
    {"tool_name": "different-tool"}, {"source_span_start": 0, "source_span_end": 2},
])
def test_same_digest_with_wrong_call_source_or_span_is_not_a_bound_trace(changes):
    from tradingagents.evidence_artifacts import (
        SourceArtifactManifest,
        SourceArtifactManifestEntry,
        project_evidence_for_audit,
    )

    evidence = evaluate("值5", "值4", content="prefix 值4 suffix", tool="synthetic_numeric")
    fact = evidence.source_facts[0].model_copy(update=changes)
    evidence = evidence.model_copy(update={"source_facts": (fact,)})
    manifest = SourceArtifactManifest(artifacts=tuple(SourceArtifactManifestEntry(
        artifact_ref="artifact=sha256:" + a.artifact_sha256,
        byte_length=len(a.raw_text.encode("utf-8"))) for a in evidence.source_artifacts))
    projected = project_evidence_for_audit(evidence, manifest)
    assert projected.claim_validations[0].comparison_evidence.status == "unavailable"
    assert "数字校验执行记录" not in evidence_diagnostic_markdown(evidence, "中文")

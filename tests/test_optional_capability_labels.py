"""Synthetic operational labels must not invent acquired evidence or provenance."""

import json

import pytest

from tradingagents.evidence import (
    EvidenceSource,
    EvidenceState,
    SourceAcquisitionUnavailable,
    evaluate_admission_gate,
)
from tradingagents.evidence_artifacts import (
    AuditEvidenceProjection,
    SourceArtifactManifest,
    project_evidence_for_audit,
)
from tradingagents.evidence_diagnostics import source_diagnostic
from tradingagents.report_presentation import evidence_diagnostic_markdown


CAPABILITIES = (
    ("get_macro_indicators", "macro_indicators", "宏观指标", "Macroeconomic indicators"),
    ("get_prediction_markets", "prediction_markets", "预测市场", "Prediction markets"),
)


def source(ref="synthetic:optional", *, required=False, detail="tool returned unavailable"):
    return EvidenceSource(
        source_id=ref, status="unavailable", required=required, detail=detail,
    )


def outcome(capability, ref="synthetic:optional", *, reason="not_configured", **changes):
    return SourceAcquisitionUnavailable(
        provider="synthetic", capability=capability, source_ref=ref, attempt=1,
        retrieved_at="2026-10-16T00:00:00+00:00", retryable=False,
        reason=reason, **changes,
    )


@pytest.mark.parametrize("capability,public_capability,zh,en", CAPABILITIES)
@pytest.mark.parametrize("language", ("中文", "English"))
@pytest.mark.parametrize("required", (False, True))
def test_exact_bound_outcome_labels_operational_gap_without_creating_evidence(
    capability, public_capability, zh, en, language, required,
):
    evidence = EvidenceState(
        sources=(source(required=required),), acquisition_outcomes=(outcome(capability),),
    )
    before = evidence.model_dump(mode="json")
    admission = evaluate_admission_gate(evidence).model_dump(mode="json")
    rendered = evidence_diagnostic_markdown(evidence, language)
    diagnostic = source_diagnostic(evidence.sources[0], evidence.acquisition_outcomes)
    assert diagnostic.display_name == en
    assert diagnostic.capability == public_capability
    assert diagnostic.reason.value == "source_unavailable"
    assert diagnostic.acquisition_reason.value == "not_configured"
    assert (zh if language == "中文" else en) in rendered
    assert ("操作状态" if language == "中文" else "operational status") in rendered
    if language == "中文":
        assert ("必需来源" if required else "可选来源") in rendered
        assert "此采集记录显示" in rendered
    assert "Unidentified source" not in rendered
    assert evidence.model_dump(mode="json") == before
    assert evaluate_admission_gate(evidence).model_dump(mode="json") == admission
    assert evidence.source_facts == evidence.current_analysis_source_facts == ()
    assert evidence.source_artifacts == evidence.material_claims == ()
    assert evidence.physical_attempt_count == 0


@pytest.mark.parametrize("capability,public_capability,zh,en", CAPABILITIES)
@pytest.mark.parametrize("case", ("prefix_only", "wrong_ref", "unknown_capability", "unregistered", "ambiguous"))
def test_name_or_mismatched_metadata_cannot_supply_new_capability_label(
    capability, public_capability, zh, en, case,
):
    ref = capability + ":synthetic"
    item = source(ref)
    outcomes = ()
    if case == "wrong_ref":
        outcomes = (outcome(capability, "synthetic:other"),)
    elif case == "unknown_capability":
        outcomes = (outcome("unrecognized", ref),)
    elif case == "unregistered":
        item = source(ref, detail="source ref is not in allowed catalog")
        outcomes = (outcome(capability, ref),)
    elif case == "ambiguous":
        outcomes = tuple(outcome(row[0], ref) for row in CAPABILITIES)
    diagnostic = source_diagnostic(item, outcomes)
    assert diagnostic.capability == "unknown"
    assert diagnostic.display_name not in {row[3] for row in CAPABILITIES}


@pytest.mark.parametrize("capability,public_capability,zh,en", CAPABILITIES + (
    ("company_financials", "company_financials", "公司财务", "Company financials"),
))
def test_audit_and_rendering_remain_closed_and_old_unknown_audit_readable(
    capability, public_capability, zh, en,
):
    sentinel = "SYNTHETIC_PRIVATE_SENTINEL"
    evidence = EvidenceState(
        sources=(source("synthetic:" + sentinel, detail=sentinel),),
        acquisition_outcomes=(outcome(capability, "synthetic:" + sentinel, error_code=sentinel),),
    )
    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())
    wire = audit.model_dump(mode="json")
    rendered = evidence_diagnostic_markdown(evidence, "中文")
    assert sentinel not in json.dumps(wire) + rendered
    assert len(audit.sources) == len(audit.acquisition_outcomes) == 1
    assert audit.sources[0].source_id == audit.acquisition_outcomes[0].source_id
    assert audit.sources[0].diagnostic.capability == public_capability
    assert audit.sources[0].status.value == "unavailable"
    assert audit.source_facts == ()
    assert AuditEvidenceProjection.model_validate_json(json.dumps(wire)) == audit
    # Historical diagnostic fields remain parseable; no migration or rewritten history.
    wire["sources"][0]["diagnostic"]["display_name"] = "Unidentified source"
    wire["sources"][0]["diagnostic"]["capability"] = "unknown"
    assert AuditEvidenceProjection.model_validate_json(json.dumps(wire)).model_dump(mode="json") == wire


def test_non_configuration_failure_keeps_its_distinct_reason():
    evidence = EvidenceState(
        sources=(source(),),
        acquisition_outcomes=(outcome("get_macro_indicators", reason="no_data"),),
    )
    rendered = evidence_diagnostic_markdown(evidence, "中文")
    assert "宏观指标" in rendered and "`no_data`" in rendered
    assert "未配置" not in rendered


def test_existing_bound_market_indicator_label_is_not_upgraded_to_provider_fact():
    item = source("get_indicators:synthetic", detail="trusted acquisition metadata not exposed")
    diagnostic = source_diagnostic(item)
    assert diagnostic.model_dump(mode="json") == {
        "display_name": "Technical indicators", "capability": "technical_indicators",
        "reason": "trusted_metadata_missing", "acquisition_reason": None,
    }


def test_news_coverage_unknown_remains_a_separate_diagnostic():
    from tradingagents.evidence_diagnostics import NEWS_DIAGNOSTIC_LABELS

    note = NEWS_DIAGNOSTIC_LABELS["news_window_coverage_unknown"][1]
    assert "完整历史覆盖未经证实" in note
    assert "hitsTotal 不能证明期间覆盖完整" in note


@pytest.mark.parametrize("language", ("中文", "English"))
def test_bound_financial_dispatch_disconnect_is_named_without_changing_evidence(language):
    """The canonical dispatch capability is not a tool-name prefix."""
    evidence = EvidenceState(
        sources=(source("synthetic:financial-request"),),
        acquisition_outcomes=(SourceAcquisitionUnavailable(
            provider="akshare", capability="company_financials",
            source_ref="synthetic:financial-request", attempt=1,
            retrieved_at="2026-10-16T00:00:00+00:00", retryable=True,
            reason="disconnect",
        ),),
    )
    before = evidence.model_dump(mode="json")
    admission = evaluate_admission_gate(evidence).model_dump(mode="json")
    rendered = evidence_diagnostic_markdown(evidence, language)
    audit = project_evidence_for_audit(evidence, SourceArtifactManifest())
    diagnostic = audit.sources[0].diagnostic
    assert diagnostic.display_name == "Company financials"
    assert diagnostic.capability == "company_financials"
    assert ("公司财务" if language == "中文" else "Company financials") in rendered
    assert "Unidentified source" not in rendered
    assert "`disconnect`" in rendered and "未配置" not in rendered
    assert diagnostic.reason.value == "source_unavailable"
    assert diagnostic.acquisition_reason.value == "disconnect"
    assert audit.sources[0].source_id == audit.acquisition_outcomes[0].source_id
    assert audit.sources[0].status.value == "unavailable"
    assert evidence.model_dump(mode="json") == before
    assert evaluate_admission_gate(evidence).model_dump(mode="json") == admission
    assert evidence.source_facts == evidence.current_analysis_source_facts == ()
    assert evidence.physical_attempt_count == 0


@pytest.mark.parametrize("case", (
    "prefix_only", "wrong_ref", "unknown_capability", "unregistered",
    "conflicting_capability", "conflicting_legacy_tool",
))
def test_financial_label_requires_unambiguous_exact_acquisition_binding(case):
    ref = "company_financials:synthetic"
    item = source(ref)
    outcomes = ()
    if case == "wrong_ref":
        outcomes = (outcome("company_financials", "synthetic:other", reason="disconnect"),)
    elif case == "unknown_capability":
        outcomes = (outcome("unrecognized", ref, reason="disconnect"),)
    elif case == "unregistered":
        item = source(ref, detail="source ref is not in allowed catalog")
        outcomes = (outcome("company_financials", ref, reason="disconnect"),)
    elif case.startswith("conflicting_"):
        other = "get_fundamentals" if case == "conflicting_legacy_tool" else "get_macro_indicators"
        outcomes = (outcome("company_financials", ref, reason="disconnect"), outcome(other, ref))
    diagnostic = source_diagnostic(item, outcomes)
    assert diagnostic.capability == "unknown"
    assert diagnostic.display_name in {"Unidentified source", "Unregistered source reference"}
    assert item.status.value == "unavailable"

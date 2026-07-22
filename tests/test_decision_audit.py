from __future__ import annotations

import gzip
import json
from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest

import tradingagents.decision_audit as decision_audit
from tradingagents.agents.managers.direction_selector import render_trading_decision
from tradingagents.agents.schemas import PortfolioRating
from tradingagents.decision_policy import (
    DecisionAssertion,
    DecisionFact,
    DecisionGateResultV2,
    DecisionHorizon,
    DecisionInstrument,
    DirectionSelection,
    EvidenceIntegrityStatus,
    HorizonUnit,
    RulePolarity,
    TradingDecisionContract,
    ValidatedDecisionContext,
)
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    EvidenceState,
    SourceAcquisitionAvailable,
    SourceArtifact,
    SourceFact,
    render_analysis_outcome,
    stable_source_fact_id,
)


def _artifact_backed_outcome_state(raw_text: str) -> tuple[dict, str]:
    digest = sha256(raw_text.encode("utf-8")).hexdigest()
    source_ref = "acq.v1:test:" + "a" * 64
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref=source_ref,
        tool_call_id="runtime-call-private",
        tool_name="test_provider",
        raw_text=raw_text,
    )
    evidence = EvidenceState(
        source_artifacts=(artifact,),
        acquisition_outcomes=(
            SourceAcquisitionAvailable(
                provider="test-provider",
                capability="news",
                source_ref=source_ref,
                attempt=1,
                retrieved_at="2026-07-20T12:00:00+00:00",
                artifact=artifact,
            ),
        ),
    )
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
        diagnostic_codes=(AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE,),
    )
    return (
        {
            "company_of_interest": "AAPL",
            "trade_date": "2026-07-18",
            "asset_type": "stock",
            "graph_signature": "analysts=market|evidence_schema=4",
            "evidence_gate_mode": "enforce",
            "evidence_state": evidence.model_dump(mode="json"),
            "evidence_preflight": {
                "passed": False,
                "readiness": "insufficient",
                "blockers": ["authoritative instrument identity is missing"],
            },
            "analysis_outcome_contract": outcome.model_dump(mode="json"),
            "analysis_outcome": render_analysis_outcome(outcome),
            "decision_audit_created_at": "2026-07-20T12:30:00+00:00",
        },
        digest,
    )


def _artifact_backed_context_state() -> tuple[dict, str, str]:
    raw_text = "P/E was 9.5."
    source_ref = "Authorization: Bearer VALIDATED_CONTEXT_SOURCE_REF_SECRET"
    state, digest = _artifact_backed_outcome_state(raw_text)
    evidence = EvidenceState.model_validate(state["evidence_state"])
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref=source_ref,
        tool_call_id="context-runtime-call-private",
        tool_name="test_provider",
        raw_text=raw_text,
    )
    fact_id = stable_source_fact_id(
        source_ref=source_ref,
        artifact_sha256=digest,
        source_span_start=0,
        source_span_end=len(raw_text),
        canonical_field="pe_ratio",
        instrument_symbol="AAPL",
        effective_date="2026-07-18",
    )
    source_fact = SourceFact(
        fact_kind="canonical",
        fact_id=fact_id,
        source_ref=source_ref,
        tool_call_id="context-runtime-call-private",
        tool_name="test_provider",
        artifact_sha256=digest,
        raw_text=raw_text,
        source_span_start=0,
        source_span_end=len(raw_text),
        canonical_field="pe_ratio",
        normalized_value=Decimal("9.5"),
        unit="ratio",
        instrument_symbol="AAPL",
        effective_date="2026-07-18",
    )
    state["evidence_state"] = evidence.model_copy(
        update={
            "source_artifacts": (*evidence.source_artifacts, artifact),
            "source_facts": (source_fact,),
        }
    ).model_dump(mode="json")
    horizon = DecisionHorizon(count=20, unit=HorizonUnit.TRADING_DAYS)
    decision_fact = DecisionFact(
        fact_id=fact_id,
        canonical_field="pe_ratio",
        normalized_value=Decimal("9.5"),
        unit="ratio",
        instrument_symbol="AAPL",
        effective_date="2026-07-18",
        source_ref=source_ref,
        artifact_sha256=digest,
        source_span_start=0,
        source_span_end=len(raw_text),
    )
    assertion = DecisionAssertion(
        assertion_id="assertion:pe-ratio-low",
        rule_id="pe.low",
        rule_version="1",
        fact_ids=(fact_id,),
        target_rating=PortfolioRating.BUY,
        polarity=RulePolarity.SUPPORTS,
        horizon=horizon,
        predicate_id="decimal.less_than",
        comparator="lt",
        threshold=Decimal("12"),
        evaluation_digest="evaluation:pe-ratio-low",
    )
    state["validated_decision_context"] = ValidatedDecisionContext(
        context_id="context:pe-ratio-test",
        evidence_contract_version="1.0",
        registry_digest="registry:decision-policy-v1",
        calculation_registry_digest="registry:calculations-v1",
        instrument=DecisionInstrument(
            symbol="AAPL",
            venue="NASDAQ",
            instrument_kind="equity",
            currency="USD",
        ),
        capability_profile_id="equity.v1",
        as_of_date=date(2026, 7, 18),
        horizon=horizon,
        facts=(decision_fact,),
        assertions=(assertion,),
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
    ).model_dump(mode="json")
    return state, digest, source_ref


def _artifact_backed_trading_decision_state() -> tuple[dict, str, str]:
    state, digest, source_ref = _artifact_backed_context_state()
    state.pop("analysis_outcome_contract")
    state.pop("analysis_outcome")
    context = ValidatedDecisionContext.model_validate(
        state["validated_decision_context"]
    )
    direction = DirectionSelection(
        context_id=context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=(context.assertions[0].assertion_id,),
    )
    decision = TradingDecisionContract(
        decision_id="decision:pe-ratio-test",
        context_id=context.context_id,
        registry_digest=context.registry_digest,
        calculation_registry_digest=context.calculation_registry_digest,
        instrument=context.instrument,
        as_of_date=context.as_of_date,
        horizon=context.horizon,
        rating=direction.rating,
        facts=context.facts,
        assertions=context.assertions,
        integrity_status=context.integrity_status,
    )
    state["direction_selection"] = direction.model_dump(mode="json")
    state["trading_decision"] = decision.model_dump(mode="json")
    state["decision_gate_v2"] = DecisionGateResultV2(
        permitted=True,
        integrity_status=decision.integrity_status,
        diagnostics=(),
        decision=decision,
    ).model_dump(mode="json")
    state["final_trade_decision"] = render_trading_decision(decision)
    return state, digest, source_ref


@pytest.mark.unit
def test_prepare_decision_audit_persists_raw_artifacts_and_returns_safe_projection(
    tmp_path,
):
    raw_text = (
        "private provider payload Authorization: Bearer TOP_SECRET_AUDIT_TOKEN"
    )
    state, digest = _artifact_backed_outcome_state(raw_text)

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert payload["schema_version"] == "3.1"
    assert raw_text not in serialized
    assert "TOP_SECRET_AUDIT_TOKEN" not in serialized
    for unsafe_key in ("raw_text", "source_ref", "tool_call_id"):
        assert f'"{unsafe_key}"' not in serialized
    assert payload["evidence_state"]["artifact_manifest"]["artifacts"] == [
        {
            "contract_version": "1.0",
            "artifact_ref": f"artifact=sha256:{digest}",
            "encoding": "utf-8",
            "compression": "gzip",
            "byte_length": len(raw_text.encode("utf-8")),
        }
    ]
    stored = (
        tmp_path
        / "evidence_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.utf8.gz"
    )
    assert gzip.decompress(stored.read_bytes()).decode("utf-8") == raw_text


@pytest.mark.unit
def test_prepare_decision_audit_persists_safe_direction_selection_diagnostics(
    tmp_path,
):
    state, _ = _artifact_backed_outcome_state("blocked selection")
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.DECISION_GATE_BLOCKED,
        diagnostic_codes=(
            AnalysisDiagnosticCode.DIRECTION_ASSERTIONS_CONFLICTED,
        ),
    )
    state["analysis_outcome_contract"] = outcome.model_dump(mode="json")
    state["analysis_outcome"] = render_analysis_outcome(outcome)
    state["direction_selection_diagnostics"] = {
        "status": "blocked",
        "reason": "direction_assertions_target_multiple_ratings",
    }

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    assert payload["direction_selection_diagnostics"] == {
        "status": "blocked",
        "reason": "direction_assertions_target_multiple_ratings",
    }
    assert state["decision_audit_sha256"] == payload["audit_sha256"]
    assert state["run_identity"]["audit_digest"] == payload["audit_sha256"]


@pytest.mark.unit
def test_prepare_decision_audit_accepts_legacy_selector_diagnostics(tmp_path):
    state, _ = _artifact_backed_outcome_state("legacy blocked selection")
    state["direction_selector_diagnostics"] = {
        "status": "blocked",
        "reason": "validated_decision_context_invalid",
        "attempts": 0,
    }

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    assert payload["direction_selection_diagnostics"] == {
        "status": "blocked",
        "reason": "validated_decision_context_invalid",
    }


@pytest.mark.unit
def test_prepare_decision_audit_rejects_forged_fact_conflicts_before_persistence(
    tmp_path,
):
    raw_text = "close 12.34"
    state, digest = _artifact_backed_outcome_state(raw_text)
    evidence = EvidenceState.model_validate(state["evidence_state"])
    artifact = evidence.source_artifacts[0]
    fact_id = stable_source_fact_id(
        source_ref=artifact.source_ref,
        artifact_sha256=digest,
        source_span_start=0,
        source_span_end=len(raw_text),
    )
    original = SourceFact(
        fact_id=fact_id,
        source_ref=artifact.source_ref,
        tool_call_id=artifact.tool_call_id,
        tool_name=artifact.tool_name,
        artifact_sha256=digest,
        raw_text=raw_text,
        source_span_start=0,
        source_span_end=len(raw_text),
    )
    conflicting = original.model_copy(update={"raw_text": "close 98.76"})
    valid_evidence = EvidenceState.model_validate(
        {
            **evidence.model_dump(mode="python"),
            "source_facts": (original,),
        }
    )
    state["evidence_state"] = valid_evidence.model_copy(
        update={"source_facts": (original, conflicting)}
    )
    audit_directory = tmp_path / "audit"

    with pytest.raises(
        ValueError,
        match=f"Source fact ID {fact_id!r} was redefined\\.",
    ):
        decision_audit.prepare_decision_audit(state, audit_directory)

    assert not (audit_directory / "evidence_artifacts").exists()


@pytest.mark.unit
def test_build_decision_audit_cannot_bypass_artifact_persistence():
    state, _digest = _artifact_backed_outcome_state("private raw audit payload")

    with pytest.raises(ValueError, match="prepare_decision_audit"):
        decision_audit.build_decision_audit(state)

    assert "decision_audit_sha256" not in state
    assert "run_identity" not in state


@pytest.mark.unit
def test_write_immutable_decision_audit_prepares_a_safe_payload_by_default(tmp_path):
    raw_text = "private writer payload Authorization: Bearer WRITER_SECRET"
    state, digest = _artifact_backed_outcome_state(raw_text)

    target = decision_audit.write_immutable_decision_audit(state, tmp_path)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert target == tmp_path / "decision-audit.json"
    assert payload["schema_version"] == "3.1"
    assert payload["audit_sha256"] == state["decision_audit_sha256"]
    assert raw_text not in json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert (
        tmp_path
        / "evidence_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.utf8.gz"
    ).is_file()


@pytest.mark.unit
def test_write_immutable_decision_audit_rejects_prebuilt_payload_bypasses(tmp_path):
    state, _digest = _artifact_backed_outcome_state("private prebuilt payload")
    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    with pytest.raises(ValueError, match="prebuilt decision audit payloads"):
        decision_audit.write_immutable_decision_audit(
            state,
            tmp_path,
            filename="prebuilt.json",
            payload=payload,
        )

    assert not (tmp_path / "prebuilt.json").exists()


@pytest.mark.unit
def test_prepare_decision_audit_projects_gate_diagnostics_without_raw_text(tmp_path):
    state, _digest = _artifact_backed_outcome_state("safe provider artifact")
    secret = "Authorization: Bearer TOP_SECRET_PREFLIGHT_DIAGNOSTIC"
    state["evidence_preflight"] = {
        "contract_version": "1.0",
        "passed": False,
        "readiness": "insufficient",
        "blockers": [secret],
    }

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert secret not in serialized
    assert payload["evidence_preflight"] == {
        "passed": False,
        "readiness": "insufficient",
        "diagnostic_codes": ["deterministic_gate_rejected"],
    }


@pytest.mark.unit
def test_prepare_decision_audit_projects_admission_diagnostics_without_raw_text(
    tmp_path,
):
    state, _digest = _artifact_backed_outcome_state("safe provider artifact")
    secret = "private-source-id (Authorization: Bearer ADMISSION_SECRET)"
    state["admission_gate"] = {
        "contract_version": "1.0",
        "admitted": False,
        "readiness": "insufficient",
        "diagnostics": [f"Required evidence unavailable: {secret}."],
    }

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert secret not in serialized
    assert payload["admission_gate"] == {
        "admitted": False,
        "readiness": "insufficient",
        "diagnostic_codes": ["required_evidence_unavailable"],
    }


@pytest.mark.unit
def test_prepare_decision_audit_projects_decision_gate_diagnostics_without_raw_text(
    tmp_path,
):
    state, _digest = _artifact_backed_outcome_state("safe provider artifact")
    secret = "decision policy failure Authorization: Bearer DECISION_GATE_SECRET"
    state["decision_gate_v2"] = {
        "contract_version": "1.0",
        "permitted": False,
        "integrity_status": "insufficient",
        "diagnostics": [secret],
        "decision": None,
    }

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert secret not in serialized
    assert payload["decision_gate"] == {
        "permitted": False,
        "readiness": "insufficient",
        "diagnostic_codes": ["deterministic_gate_rejected"],
    }


@pytest.mark.unit
def test_prepare_decision_audit_is_invariant_to_analysis_outcome_diagnostic_order(
    tmp_path,
):
    expected_contract = {
        "contract_version": "2.0",
        "readiness": "insufficient",
        "reason": "preflight_blocked",
        "diagnostic_codes": ["identity_unavailable", "snapshot_unavailable"],
    }
    canonical_outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
        diagnostic_codes=(
            AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE,
            AnalysisDiagnosticCode.SNAPSHOT_UNAVAILABLE,
        ),
    )
    permuted_state, _digest = _artifact_backed_outcome_state("same artifact")
    canonical_state, _digest = _artifact_backed_outcome_state("same artifact")
    permuted_state["analysis_outcome_contract"] = {
        **expected_contract,
        "diagnostic_codes": [
            "snapshot_unavailable",
            "identity_unavailable",
            "snapshot_unavailable",
        ],
    }
    canonical_state["analysis_outcome_contract"] = expected_contract
    rendered = render_analysis_outcome(canonical_outcome)
    permuted_state["analysis_outcome"] = rendered
    canonical_state["analysis_outcome"] = rendered

    permuted_payload = decision_audit.prepare_decision_audit(
        permuted_state,
        tmp_path / "permuted",
    )
    canonical_payload = decision_audit.prepare_decision_audit(
        canonical_state,
        tmp_path / "canonical",
    )

    assert permuted_payload["analysis_outcome_contract"] == expected_contract
    assert canonical_payload["analysis_outcome_contract"] == expected_contract
    assert (
        permuted_payload["terminal"]["output_sha256"]
        == canonical_payload["terminal"]["output_sha256"]
    )
    assert permuted_payload["audit_sha256"] == canonical_payload["audit_sha256"]
    assert json.dumps(
        permuted_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) == json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@pytest.mark.unit
def test_prepare_decision_audit_projects_validated_context_without_raw_source_ref(
    tmp_path,
):
    state, digest, secret_source_ref = _artifact_backed_context_state()

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert secret_source_ref not in serialized
    assert '"source_ref"' not in serialized
    assert payload["validated_decision_context"] == {
        "contract_version": "1.0",
        "context_id": "context:pe-ratio-test",
        "evidence_contract_version": "1.0",
        "registry_digest": "registry:decision-policy-v1",
        "calculation_registry_digest": "registry:calculations-v1",
        "instrument": {
            "contract_version": "1.0",
            "symbol": "AAPL",
            "venue": "NASDAQ",
            "instrument_kind": "equity",
            "currency": "USD",
        },
        "capability_profile_id": "equity.v1",
        "as_of_date": "2026-07-18",
        "horizon": {"count": 20, "unit": "trading_days"},
        "facts": [
            {
                "fact_id": "fact:67802cc8efe920bfb3091763384b48165671f47cb285833342f5e7d89b0c0ef3",
                "artifact_ref": f"artifact=sha256:{digest}",
            }
        ],
        "assertion_ids": ["assertion:pe-ratio-low"],
        "integrity_status": "decision_ready",
    }


@pytest.mark.unit
def test_prepare_decision_audit_projects_trading_decision_without_raw_source_ref(
    tmp_path,
):
    state, digest, secret_source_ref = _artifact_backed_trading_decision_state()

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fact_id = "fact:67802cc8efe920bfb3091763384b48165671f47cb285833342f5e7d89b0c0ef3"
    assert secret_source_ref not in serialized
    assert '"source_ref"' not in serialized
    assert payload["trading_decision"] == {
        "contract_version": "1.0",
        "decision_id": "decision:pe-ratio-test",
        "context_id": "context:pe-ratio-test",
        "registry_digest": "registry:decision-policy-v1",
        "calculation_registry_digest": "registry:calculations-v1",
        "instrument": {
            "contract_version": "1.0",
            "symbol": "AAPL",
            "venue": "NASDAQ",
            "instrument_kind": "equity",
            "currency": "USD",
        },
        "as_of_date": "2026-07-18",
        "horizon": {"count": 20, "unit": "trading_days"},
        "rating": "Buy",
        "facts": [
            {
                "fact_id": fact_id,
                "artifact_ref": f"artifact=sha256:{digest}",
            }
        ],
        "assertions": [
            {
                "assertion_id": "assertion:pe-ratio-low",
                "rule_id": "pe.low",
                "rule_version": "1",
                "fact_ids": [fact_id],
                "target_rating": "Buy",
                "polarity": "supports",
                "horizon": {"count": 20, "unit": "trading_days"},
                "predicate_id": "decimal.less_than",
                "comparator": "lt",
                "threshold": "12",
                "evaluation_digest": "evaluation:pe-ratio-low",
            }
        ],
        "integrity_status": "decision_ready",
    }


@pytest.mark.unit
def test_prepare_decision_audit_canonicalizes_direction_selection_assertion_ids(
    tmp_path,
):
    state, _digest, _source_ref = _artifact_backed_context_state()
    context = ValidatedDecisionContext.model_validate(
        state["validated_decision_context"]
    )
    second_assertion = DecisionAssertion(
        assertion_id="assertion:z-risk-check",
        rule_id="risk.check",
        rule_version="1",
        fact_ids=(context.facts[0].fact_id,),
        target_rating=PortfolioRating.HOLD,
        polarity=RulePolarity.SUPPORTS,
        horizon=context.horizon,
        predicate_id="decimal.less_than",
        comparator="lt",
        threshold=Decimal("20"),
        evaluation_digest="evaluation:z-risk-check",
    )
    state["validated_decision_context"] = context.model_copy(
        update={"assertions": (second_assertion, context.assertions[0])}
    ).model_dump(mode="json")
    state["direction_selection"] = {
        "contract_version": "1.0",
        "context_id": "context:pe-ratio-test",
        "rating": "Buy",
        "assertion_ids": ["assertion:z-risk-check", "assertion:pe-ratio-low"],
    }

    payload = decision_audit.prepare_decision_audit(state, tmp_path)

    assert payload["direction_selection"] == {
        "contract_version": "1.0",
        "context_id": "context:pe-ratio-test",
        "rating": "Buy",
        "assertion_ids": ["assertion:pe-ratio-low", "assertion:z-risk-check"],
    }


@pytest.mark.unit
def test_prepare_decision_audit_rejects_direction_selection_context_mismatch(
    tmp_path,
):
    state, _digest, _source_ref = _artifact_backed_context_state()
    state["direction_selection"] = {
        "contract_version": "1.0",
        "context_id": "context:different",
        "rating": "Buy",
        "assertion_ids": ["assertion:pe-ratio-low"],
    }

    with pytest.raises(ValueError, match="direction selection context mismatch"):
        decision_audit.prepare_decision_audit(state, tmp_path)


@pytest.mark.unit
def test_prepare_decision_audit_rejects_incomplete_direction_selection_assertion_coverage(
    tmp_path,
):
    state, _digest, _source_ref = _artifact_backed_context_state()
    context = ValidatedDecisionContext.model_validate(
        state["validated_decision_context"]
    )
    first_assertion = context.assertions[0]
    second_assertion = DecisionAssertion(
        assertion_id="assertion:z-risk-check",
        rule_id="risk.check",
        rule_version="1",
        fact_ids=(context.facts[0].fact_id,),
        target_rating=PortfolioRating.HOLD,
        polarity=RulePolarity.SUPPORTS,
        horizon=context.horizon,
        predicate_id="decimal.less_than",
        comparator="lt",
        threshold=Decimal("20"),
        evaluation_digest="evaluation:z-risk-check",
    )
    state["validated_decision_context"] = context.model_copy(
        update={"assertions": (first_assertion, second_assertion)}
    ).model_dump(mode="json")
    state["direction_selection"] = {
        "contract_version": "1.0",
        "context_id": "context:pe-ratio-test",
        "rating": "Buy",
        "assertion_ids": [
            "assertion:pe-ratio-low",
            "assertion:pe-ratio-low",
        ],
    }

    with pytest.raises(ValueError, match="assertion coverage"):
        decision_audit.prepare_decision_audit(state, tmp_path)

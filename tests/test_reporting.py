"""Deterministic report, terminal-contract, and audit regression coverage."""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from tradingagents.agents.managers.direction_selector import render_trading_decision
from tradingagents.agents.schemas import PortfolioRating
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.acquisition import (
    AcquisitionController,
    AcquisitionFailure,
    AcquisitionRequest,
)
from tradingagents.decision_audit import prepare_decision_audit
from tradingagents.decision_policy import (
    DecisionAssertion,
    DecisionFact,
    DecisionGateResultV2,
    DecisionHorizon,
    DecisionInstrument,
    EvidenceIntegrityStatus,
    HorizonUnit,
    RulePolarity,
    TradingDecisionContract,
    ValidatedDecisionContext,
    stable_decision_value_digest,
)
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    CalculationLineage,
    EvidenceReadiness,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MarketSnapshotEvidence,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    SourceFact,
    ToolExecutionEvidenceEnvelope,
    build_tool_evidence_state,
    merge_source_acquisition_outcomes,
    render_analysis_outcome,
    stable_source_fact_id,
)
from tradingagents.graph.signal_processing import SignalProcessor
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import render_analysis_outcome_report, write_report_tree
from tradingagents.terminal_contract import (
    TerminalOutcomeKind,
    build_terminal_contract,
    canonical_json_bytes,
    configuration_digest,
)

ARTIFACT_TEXT = "P/E was 9.5."
ARTIFACT_SHA256 = sha256(ARTIFACT_TEXT.encode()).hexdigest()
SOURCE_REF = "issuer_filing:AAPL:2026-07-18"
IDENTITY_ARTIFACT_TEXT = '{"symbol":"AAPL","venue":"NASDAQ"}'
IDENTITY_ARTIFACT_SHA256 = sha256(IDENTITY_ARTIFACT_TEXT.encode()).hexdigest()
IDENTITY_SOURCE_REF = "nasdaq:AAPL"
FACT_ID = stable_source_fact_id(
    source_ref=SOURCE_REF,
    artifact_sha256=ARTIFACT_SHA256,
    source_span_start=0,
    source_span_end=len(ARTIFACT_TEXT),
    canonical_field="pe_ratio",
    instrument_symbol="AAPL",
    effective_date="2026-07-18",
)
HORIZON = DecisionHorizon(count=20, unit=HorizonUnit.TRADING_DAYS)


def _lineage() -> CalculationLineage:
    return CalculationLineage(
        calculation_id="pe_ratio.adapter",
        calculation_version="1",
        input_artifact_sha256=ARTIFACT_SHA256,
        input_snapshot_id="artifact-record",
        effective_range_start="2026-07-18",
        effective_range_end="2026-07-18",
        observations_used=1,
        adjustment_basis="source-reported",
        implementation_version="test-adapter-1",
        result_digest=stable_decision_value_digest(
            canonical_field="pe_ratio",
            normalized_value=Decimal("9.5"),
            unit="ratio",
        ),
    )


def _source_fact(*, tool_call_id: str = "runtime-call-1") -> SourceFact:
    return SourceFact(
        fact_kind="canonical",
        fact_id=FACT_ID,
        source_ref=SOURCE_REF,
        tool_call_id=tool_call_id,
        tool_name="issuer_filing",
        artifact_sha256=ARTIFACT_SHA256,
        raw_text=ARTIFACT_TEXT,
        source_span_start=0,
        source_span_end=len(ARTIFACT_TEXT),
        calculation_ids=("pe_ratio.adapter",),
        canonical_field="pe_ratio",
        normalized_value=Decimal("9.5"),
        unit="ratio",
        instrument_symbol="AAPL",
        effective_date="2026-07-18",
        calculation_lineage=_lineage(),
    )


def _evidence(*facts: SourceFact) -> EvidenceState:
    artifacts = (
        SourceArtifact(
            artifact_sha256=IDENTITY_ARTIFACT_SHA256,
            source_ref=IDENTITY_SOURCE_REF,
            tool_call_id="identity-runtime-call",
            tool_name="security-master",
            raw_text=IDENTITY_ARTIFACT_TEXT,
        ),
    )
    if facts:
        artifacts += (
            SourceArtifact(
                artifact_sha256=ARTIFACT_SHA256,
                source_ref=SOURCE_REF,
                tool_call_id="artifact-runtime-call",
                tool_name="issuer_filing",
                raw_text=ARTIFACT_TEXT,
            ),
        )
    return EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="AAPL",
            venue="NASDAQ",
            instrument_kind=InstrumentKind.EQUITY,
            currency="USD",
            display_name="Apple Inc.",
            provenance=IdentityProvenance(
                provider="security-master",
                source_ref=IDENTITY_SOURCE_REF,
                retrieved_at="2026-07-18T00:00:00+00:00",
                artifact_sha256=IDENTITY_ARTIFACT_SHA256,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="AAPL",
            provider="test",
            retrieved_at="2026-07-18T00:00:00+00:00",
            adjustment_basis="adjusted",
            requested_date="2026-07-18",
            effective_trading_date="2026-07-18",
            history_rows=250,
            frame_sha256="f" * 64,
            snapshot_id="snapshot:test",
        ),
        source_facts=facts,
        source_artifacts=artifacts,
        sources=(
            EvidenceSource(
                source_id=SOURCE_REF,
                status=EvidenceStatus.AVAILABLE,
                required=False,
            ),
        ),
    )


def _validated_components():
    fact = DecisionFact(
        fact_id=FACT_ID,
        canonical_field="pe_ratio",
        normalized_value=Decimal("9.5"),
        unit="ratio",
        instrument_symbol="AAPL",
        effective_date="2026-07-18",
        source_ref=SOURCE_REF,
        artifact_sha256=ARTIFACT_SHA256,
        source_span_start=0,
        source_span_end=len(ARTIFACT_TEXT),
        calculation_lineage=_lineage(),
    )
    assertion = DecisionAssertion(
        assertion_id="assertion:test-pe-low",
        rule_id="pe.low",
        rule_version="1",
        fact_ids=(FACT_ID,),
        target_rating=PortfolioRating.BUY,
        polarity=RulePolarity.SUPPORTS,
        horizon=HORIZON,
        predicate_id="decimal.less_than",
        comparator="lt",
        threshold=Decimal("12"),
        evaluation_digest="evaluation:test-pe-low",
    )
    instrument = DecisionInstrument(
        symbol="AAPL",
        venue="NASDAQ",
        instrument_kind=InstrumentKind.EQUITY,
        currency="USD",
    )
    context = ValidatedDecisionContext(
        context_id="context:test",
        evidence_contract_version="1.0",
        registry_digest="registry:test",
        calculation_registry_digest="calculations:test",
        instrument=instrument,
        capability_profile_id="equity.v1",
        as_of_date=date(2026, 7, 18),
        horizon=HORIZON,
        facts=(fact,),
        assertions=(assertion,),
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
    )
    decision = TradingDecisionContract(
        decision_id="decision:test",
        context_id=context.context_id,
        registry_digest=context.registry_digest,
        calculation_registry_digest=context.calculation_registry_digest,
        instrument=instrument,
        as_of_date=context.as_of_date,
        horizon=HORIZON,
        rating=PortfolioRating.BUY,
        facts=(fact,),
        assertions=(assertion,),
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
    )
    gate = DecisionGateResultV2(
        permitted=True,
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
        diagnostics=(),
        decision=decision,
    )
    return context, decision, gate


def _decision_state() -> dict:
    context, decision, gate = _validated_components()
    return {
        "company_of_interest": "AAPL",
        "trade_date": "2026-07-18",
        "asset_type": "stock",
        "graph_signature": "analysts=market|evidence_schema=4",
        "evidence_gate_mode": "enforce",
        "evidence_state": _evidence(_source_fact()).model_dump(mode="json"),
        "evidence_preflight": {"passed": True, "readiness": "decision_ready"},
        "admission_gate": {"admitted": True, "readiness": "decision_ready"},
        "validated_decision_context": context.model_dump(mode="json"),
        "direction_selection": {
            "context_id": context.context_id,
            "rating": "Buy",
            "assertion_ids": [context.assertions[0].assertion_id],
        },
        "decision_gate": gate.model_dump(mode="json"),
        "decision_gate_v2": gate.model_dump(mode="json"),
        "trading_decision": decision.model_dump(mode="json"),
        "final_trade_decision": render_trading_decision(decision),
        # Untrusted directional prose must never become the published decision.
        "market_report": "MODEL SAYS SELL",
        "investment_debate_state": {"judge_decision": "BUY EVERYTHING"},
        "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **SELL**",
        "risk_debate_state": {"judge_decision": "**Rating**: Sell"},
    }


def _outcome_state() -> dict:
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
        diagnostic_codes=(AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE,),
    )
    return {
        "company_of_interest": "AAPL",
        "trade_date": "2026-07-18",
        "asset_type": "stock",
        "graph_signature": "analysts=market|evidence_schema=4",
        "evidence_gate_mode": "enforce",
        "evidence_state": EvidenceState().model_dump(mode="json"),
        "evidence_preflight": {
            "passed": False,
            "readiness": "insufficient",
            "blockers": ["authoritative instrument identity is missing"],
        },
        "analysis_outcome_contract": outcome.model_dump(mode="json"),
        "analysis_outcome": render_analysis_outcome(outcome),
    }


@pytest.mark.unit
def test_write_report_tree_renders_only_the_validated_decision(tmp_path):
    state = _decision_state()
    out = write_report_tree(state, "AAPL", tmp_path)

    decision_report = (tmp_path / "5_portfolio" / "decision.md").read_text()
    complete = out.read_text()
    assert "`pe_ratio`" in decision_report
    assert "9.5 ratio" in decision_report
    assert "`pe.low@1`" in decision_report
    assert "`decimal.less_than` (lt 12)" in decision_report
    assert "Source Availability Coverage" in decision_report
    assert "Validated Fact Coverage" in decision_report
    assert "Decision Assertion Coverage" in decision_report
    assert "Decision Confidence" not in decision_report
    assert SOURCE_REF not in decision_report
    assert FACT_ID in decision_report
    assert f"artifact=sha256:{ARTIFACT_SHA256}" in decision_report
    assert "MODEL SAYS SELL" not in complete
    assert "BUY EVERYTHING" not in complete
    assert "FINAL TRANSACTION PROPOSAL" not in complete
    assert not (tmp_path / "1_analysts").exists()
    assert not (tmp_path / "2_research").exists()
    assert not (tmp_path / "3_trading").exists()
    assert not (tmp_path / "4_risk").exists()
    assert state["lifecycle_status"] == "completed"
    assert state["terminal_outcome_kind"] == "trading_decision"
    assert state["active_phase"] is None
    assert state["run_id"] in complete
    assert state["decision_audit_sha256"] in complete


@pytest.mark.unit
def test_decision_report_renders_sanitized_deterministic_acquisition_outcomes(
    tmp_path,
):
    state = _decision_state()
    payload = "raw payload Authorization: Bearer available-secret"
    artifact = SourceArtifact(
        artifact_sha256=sha256(payload.encode()).hexdigest(),
        source_ref="acq.v1:feed:available-secret-locator",
        tool_call_id="call-available",
        tool_name="market_feed",
        raw_text=payload,
    )
    available = SourceAcquisitionAvailable(
        provider="ZetaFeed",
        capability="market_snapshot",
        source_ref=artifact.source_ref,
        attempt=2,
        retrieved_at="2026-07-18T00:02:00+00:00",
        artifact=artifact,
    )
    unavailable = SourceAcquisitionUnavailable(
        provider="AlphaNews",
        capability="news",
        source_ref="acq.v1:news:unavailable-secret-locator",
        attempt=1,
        retrieved_at="2026-07-18T00:01:00+00:00",
        retryable=True,
        reason=AcquisitionUnavailableReason.RATE_LIMITED,
        retry_after_seconds=30,
        http_status=429,
    )
    evidence = EvidenceState.model_validate(state["evidence_state"]).model_copy(
        update={"acquisition_outcomes": (available, unavailable)}
    )
    state["evidence_state"] = evidence.model_dump(mode="json")

    out = write_report_tree(state, "AAPL", tmp_path)
    decision_report = (tmp_path / "5_portfolio" / "decision.md").read_text()
    complete = out.read_text()

    assert "## Acquisition Outcomes" in decision_report
    assert decision_report.index("AlphaNews") < decision_report.index("ZetaFeed")
    for expected in (
        "**Provider:** `AlphaNews`",
        "**Capability:** `news`",
        "**Attempt:** 1",
        "**Retrieved at:** 2026-07-18T00:01:00+00:00",
        "**Outcome:** unavailable",
        "**Unavailable reason:** rate_limited",
        "**Retryable:** true",
        "**Retry-After:** 30 seconds",
        "**Provider:** `ZetaFeed`",
        "**Capability:** `market_snapshot`",
        "**Attempt:** 2",
        "**Retrieved at:** 2026-07-18T00:02:00+00:00",
        "**Outcome:** available",
        "**Unavailable reason:** not applicable",
        "**Retryable:** false",
        "**Retry-After:** not provided",
    ):
        assert expected in decision_report
    for secret in (
        "available-secret",
        "unavailable-secret",
        "raw payload",
    ):
        assert secret not in decision_report
        assert secret not in complete


@pytest.mark.unit
def test_analysis_outcome_report_renders_sanitized_deterministic_acquisitions(
    tmp_path,
):
    state = _outcome_state()
    payload = "artifact payload Authorization: Bearer available-outcome-secret"
    artifact = SourceArtifact(
        artifact_sha256=sha256(payload.encode()).hexdigest(),
        source_ref="acq.v1:feed:available-outcome-secret-locator",
        tool_call_id="call-outcome-available",
        tool_name="market_feed",
        raw_text=payload,
    )
    available = SourceAcquisitionAvailable(
        provider="ZetaFeed",
        capability="market_snapshot",
        source_ref=artifact.source_ref,
        attempt=2,
        retrieved_at="2026-07-18T00:02:00+00:00",
        artifact=artifact,
    )
    unavailable = SourceAcquisitionUnavailable(
        provider="AlphaNews",
        capability="news",
        source_ref="acq.v1:news:unavailable-outcome-secret-locator",
        attempt=1,
        retrieved_at="2026-07-18T00:01:00+00:00",
        retryable=True,
        reason=AcquisitionUnavailableReason.RATE_LIMITED,
        retry_after_seconds=30,
        http_status=429,
    )
    state["evidence_state"] = EvidenceState(
        acquisition_outcomes=(available, unavailable)
    ).model_dump(mode="json")

    out = write_report_tree(state, "AAPL", tmp_path)
    outcome_report = (tmp_path / "5_portfolio" / "analysis_outcome.md").read_text()
    complete = out.read_text()

    assert "## Acquisition Outcomes" in outcome_report
    assert outcome_report.index("AlphaNews") < outcome_report.index("ZetaFeed")
    for expected in (
        "**Provider:** `AlphaNews`",
        "**Capability:** `news`",
        "**Attempt:** 1",
        "**Retrieved at:** 2026-07-18T00:01:00+00:00",
        "**Outcome:** unavailable",
        "**Unavailable reason:** rate_limited",
        "**Retryable:** true",
        "**Retry-After:** 30 seconds",
        "**Provider:** `ZetaFeed`",
        "**Capability:** `market_snapshot`",
        "**Attempt:** 2",
        "**Retrieved at:** 2026-07-18T00:02:00+00:00",
        "**Outcome:** available",
        "**Unavailable reason:** not applicable",
        "**Retryable:** false",
        "**Retry-After:** not provided",
    ):
        assert expected in outcome_report
    for secret in (
        "available-outcome-secret",
        "unavailable-outcome-secret",
        "artifact payload",
    ):
        assert secret not in outcome_report
        assert secret not in complete


@pytest.mark.unit
def test_acquisition_fallback_audit_persists_only_closed_failure_metadata(tmp_path):
    secret_error_code = "ARBITRARY_SECRET_ERROR_CODE_7f6c"

    def limited(_request: AcquisitionRequest) -> object:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.RATE_LIMITED,
            status_code=429,
            error_code=secret_error_code,
            retry_after_seconds=30,
        )

    controller = AcquisitionController(
        providers=(
            ("limited", limited),
            ("fallback", lambda _request: {"close": "8.80"}),
        ),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )
    result = controller.acquire(
        AcquisitionRequest(
            capability="daily_close",
            source_ref="market:600895.SS:2026-07-18",
            tool_call_id="call-audit-fallback",
            tool_name="get_market_data",
        ),
        validator=lambda value: value,
        serializer=lambda _value: '{"close":"8.80"}',
    )

    failed, fallback = result.outcomes
    assert isinstance(failed, SourceAcquisitionUnavailable)
    assert not hasattr(failed, "artifact")
    assert isinstance(fallback, SourceAcquisitionAvailable)
    assert fallback.artifact == result.artifact

    evidence = merge_source_acquisition_outcomes(EvidenceState(), result.outcomes)
    state = _outcome_state()
    state["evidence_state"] = evidence.model_dump(mode="json")
    audit = prepare_decision_audit(state, tmp_path)
    failed_audit, fallback_audit = audit["evidence_state"]["acquisition_outcomes"]

    assert {
        "provider",
        "capability",
        "attempt",
        "retrieved_at",
        "outcome",
        "reason",
        "retryable",
        "retry_after_seconds",
        "http_status",
    } <= failed_audit.keys()
    assert failed_audit["http_status"] == 429
    assert failed_audit["retry_after_seconds"] == 30.0
    assert "artifact_ref" not in failed_audit
    assert fallback_audit["outcome"] == "available"
    assert fallback_audit["artifact_ref"] == (
        f"artifact=sha256:{result.artifact.artifact_sha256}"
    )

    failed_serialized = json.dumps(failed_audit, sort_keys=True)
    assert "diagnostic" not in failed_serialized.casefold()
    assert "category" not in failed_serialized.casefold()
    assert secret_error_code not in json.dumps(audit, sort_keys=True)


@pytest.mark.unit
def test_acquisition_merge_and_audit_are_invariant_to_insertion_order(tmp_path):
    def unavailable(_request: AcquisitionRequest) -> object:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.RATE_LIMITED,
            status_code=429,
            retry_after_seconds=30,
        )

    controller = AcquisitionController(
        providers=(
            ("primary", unavailable),
            ("fallback", lambda _request: {"close": "8.80"}),
        ),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )
    result = controller.acquire(
        AcquisitionRequest(
            capability="daily_close",
            source_ref="market:600895.SS:2026-07-18",
            tool_call_id="call-invariant-fallback",
            tool_name="get_market_data",
        ),
        validator=lambda value: value,
        serializer=lambda _value: '{"close":"8.80"}',
    )

    first_evidence = merge_source_acquisition_outcomes(
        EvidenceState(), result.outcomes
    )
    second_evidence = merge_source_acquisition_outcomes(
        merge_source_acquisition_outcomes(EvidenceState(), (result.outcomes[1],)),
        (result.outcomes[0],),
    )

    assert first_evidence.acquisition_outcomes == second_evidence.acquisition_outcomes

    created_at = "2026-07-20T12:30:00+00:00"
    first_state = _outcome_state()
    first_state["evidence_state"] = first_evidence.model_dump(mode="json")
    first_state["decision_audit_created_at"] = created_at
    second_state = _outcome_state()
    second_state["evidence_state"] = second_evidence.model_dump(mode="json")
    second_state["decision_audit_created_at"] = created_at

    first_audit = prepare_decision_audit(first_state, tmp_path / "first")
    second_audit = prepare_decision_audit(second_state, tmp_path / "second")
    assert canonical_json_bytes(first_audit) == canonical_json_bytes(second_audit)
    assert first_audit["audit_sha256"] == second_audit["audit_sha256"]


@pytest.mark.unit
def test_blocked_analysis_writes_split_coverage_and_no_direction(tmp_path):
    for relative in (
        "investment_plan.md",
        "trader_investment_plan.md",
        "final_trade_decision.md",
    ):
        (tmp_path / relative).write_text("STALE DIRECTIONAL DRAFT", encoding="utf-8")

    state = _outcome_state()
    out = write_report_tree(state, "AAPL", tmp_path)
    complete = out.read_text()

    assert (tmp_path / "5_portfolio" / "analysis_outcome.md").exists()
    assert not (tmp_path / "5_portfolio" / "decision.md").exists()
    assert "Source Availability Coverage" in complete
    assert "Validated Fact Coverage" in complete
    assert "Decision Assertion Coverage" in complete
    assert "**Evidence Coverage:**" not in complete
    assert "No Trading Decision was issued" in complete
    assert "STALE DIRECTIONAL DRAFT" not in complete
    assert state["lifecycle_status"] == "completed"
    assert state["terminal_outcome_kind"] == "analysis_outcome"


@pytest.mark.unit
@pytest.mark.parametrize("outcome", (False, True))
def test_report_tree_writes_complete_immutable_audit(tmp_path, outcome):
    state = _outcome_state() if outcome else _decision_state()
    write_report_tree(state, "AAPL", tmp_path)

    audit_path = tmp_path / "decision-audit.json"
    first_bytes = audit_path.read_bytes()
    audit = json.loads(first_bytes)
    expected = "analysis_outcome" if outcome else "trading_decision"
    assert audit["schema_version"] == "3.1"
    assert audit["terminal"]["terminal_outcome_kind"] == expected
    assert audit["run"]["run_id"] == state["run_id"]
    assert audit["run"]["configuration_digest"] == state["configuration_digest"]
    assert audit["audit_sha256"] == state["decision_audit_sha256"]
    assert "confidence" not in json.dumps(audit).casefold()
    if not outcome:
        decision = audit["trading_decision"]
        assert {
            key: decision[key]
            for key in (
                "contract_version",
                "decision_id",
                "context_id",
                "rating",
                "integrity_status",
            )
        } == {
            "contract_version": "1.0",
            "decision_id": "decision:test",
            "context_id": "context:test",
            "rating": "Buy",
            "integrity_status": "decision_ready",
        }
        assert decision["facts"] == [
            {
                "fact_id": FACT_ID,
                "artifact_ref": f"artifact=sha256:{ARTIFACT_SHA256}",
            }
        ]
        assert decision["assertions"] == [
            {
                "assertion_id": "assertion:test-pe-low",
                "rule_id": "pe.low",
                "rule_version": "1",
                "fact_ids": [FACT_ID],
                "target_rating": "Buy",
                "polarity": "supports",
                "horizon": {"count": 20, "unit": "trading_days"},
                "predicate_id": "decimal.less_than",
                "comparator": "lt",
                "threshold": "12",
                "evaluation_digest": "evaluation:test-pe-low",
            }
        ]
        serialized_audit = json.dumps(audit, sort_keys=True)
        for unsafe_key in ("source_ref", "raw_text", "tool_call_id"):
            assert f'"{unsafe_key}"' not in serialized_audit
    else:
        assert audit["analysis_outcome_contract"] == state[
            "analysis_outcome_contract"
        ]

    write_report_tree(state, "AAPL", tmp_path)
    assert audit_path.read_bytes() == first_bytes

    state["graph_signature"] = "different-valid-graph-signature"
    with pytest.raises(FileExistsError, match="immutable decision audit"):
        write_report_tree(state, "AAPL", tmp_path)


@pytest.mark.unit
def test_completion_or_portfolio_prose_cannot_impersonate_a_decision():
    state = _outcome_state()
    state.pop("analysis_outcome")
    state.pop("analysis_outcome_contract")
    state.update(
        {
            "lifecycle_status": "completed",
            "final_trade_decision": "**Rating**: Buy",
            "risk_debate_state": {"judge_decision": "**Rating**: Buy"},
        }
    )

    with pytest.raises(ValueError, match="neither a validated Trading Decision"):
        build_terminal_contract(state)


@pytest.mark.unit
def test_arbitrary_analysis_outcome_prose_cannot_become_terminal():
    state = _outcome_state()
    state.pop("analysis_outcome_contract")
    state["analysis_outcome"] = "**Rating**: Buy"

    with pytest.raises(ValueError, match="typed Analysis Outcome contract"):
        build_terminal_contract(state)


@pytest.mark.unit
def test_analysis_outcome_render_must_match_its_typed_contract():
    state = _outcome_state()
    state["analysis_outcome"] += "\n\n**Rating**: Hold"

    with pytest.raises(ValueError, match="not rendered from its typed"):
        build_terminal_contract(state)


@pytest.mark.unit
def test_analysis_outcome_report_rejects_arbitrary_prose():
    terminal = build_terminal_contract(_outcome_state())

    with pytest.raises((TypeError, ValueError)):
        render_analysis_outcome_report("**Rating**: Hold", terminal)


@pytest.mark.unit
def test_terminal_decision_requires_its_validated_context():
    state = _decision_state()
    state.pop("validated_decision_context")

    with pytest.raises(ValueError, match="missing its validated decision context"):
        build_terminal_contract(state)


@pytest.mark.unit
def test_shadow_state_cannot_validate_as_a_trading_decision():
    state = _decision_state()
    state["evidence_gate_mode"] = "shadow"

    with pytest.raises(ValueError, match="shadow evidence mode"):
        build_terminal_contract(state)


@pytest.mark.unit
def test_signal_processor_requires_the_audited_terminal_publication(tmp_path):
    state = _decision_state()
    processor = SignalProcessor()

    with pytest.raises(ValueError):
        processor.process_signal(state)

    prepare_decision_audit(state, tmp_path)
    assert processor.process_signal(state) == "Buy"
    with pytest.raises((TypeError, ValueError)):
        processor.process_signal(state["trading_decision"])


def test_memory_log_requires_the_audited_terminal_publication(tmp_path):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    state = _decision_state()

    with pytest.raises((TypeError, ValueError)):
        log.store_trading_decision(
            "AAPL",
            "2026-07-18",
            state["trading_decision"],
        )
    with pytest.raises(ValueError):
        log.store_trading_decision("AAPL", "2026-07-18", state)

    prepare_decision_audit(state, tmp_path / "audit")
    log.store_trading_decision(
        "AAPL",
        "2026-07-18",
        state,
    )

    [entry] = log.load_entries()
    assert entry["rating"] == "Buy"
    assert entry["decision"] == state["final_trade_decision"]


@pytest.mark.unit
def test_coverage_is_invariant_to_duplicate_facts_and_runtime_call_ids():
    first = _decision_state()
    second = _decision_state()
    evidence = _evidence(
        _source_fact(tool_call_id="runtime-a"),
        _source_fact(tool_call_id="runtime-b"),
    )
    second["evidence_state"] = evidence.model_dump(mode="json")

    first_terminal = build_terminal_contract(first)
    second_terminal = build_terminal_contract(second)
    assert first_terminal.coverage == second_terminal.coverage
    assert second_terminal.coverage.validated_facts.covered_count == 1
    assert second_terminal.coverage.validated_facts.total_count == 1


@pytest.mark.unit
def test_validated_fact_coverage_ignores_irrelevant_discovered_facts():
    baseline = _decision_state()
    transformed = _decision_state()
    irrelevant = _source_fact().model_copy(
        update={
            "fact_id": "fact:" + "2" * 64,
            "canonical_field": "unselected_metric",
        }
    )
    transformed["evidence_state"] = _evidence(
        _source_fact(), irrelevant
    ).model_dump(mode="json")

    baseline_measure = build_terminal_contract(baseline).coverage.validated_facts
    transformed_measure = build_terminal_contract(transformed).coverage.validated_facts

    assert baseline_measure.model_dump(mode="json") == {
        "covered_count": 1,
        "total_count": 1,
        "ratio": 1.0,
    }
    assert transformed_measure == baseline_measure


@pytest.mark.unit
def test_conflicting_duplicate_required_fact_is_rejected_before_coverage():
    conflict = _source_fact().model_copy(update={"normalized_value": Decimal("8.5")})

    with pytest.raises(ValueError, match="Source fact ID .* was redefined"):
        _evidence(_source_fact(), conflict)


@pytest.mark.unit
def test_no_declared_required_facts_has_honest_empty_coverage():
    state = _outcome_state()
    state["evidence_state"] = _evidence(_source_fact()).model_dump(mode="json")

    measure = build_terminal_contract(state).coverage.validated_facts

    assert measure.model_dump(mode="json") == {
        "covered_count": 0,
        "total_count": 0,
        "ratio": 0.0,
    }


@pytest.mark.unit
def test_configuration_digest_excludes_paths_and_secret_values():
    first = configuration_digest(
        {
            "results_dir": "C:/first",
            "model": "gpt-test",
            "api_key": "secret-one",
        }
    )
    second = configuration_digest(
        {
            "results_dir": "D:/copied",
            "model": "gpt-test",
            "api_key": "secret-two",
        }
    )
    assert first == second


@pytest.mark.unit
def test_decision_audit_uses_the_full_semantic_configuration(tmp_path):
    state = _decision_state()
    config = {
        "results_dir": "C:/ignored-output",
        "decision_horizon": "20_trading_days",
        "evidence_gate_mode": "enforce",
    }

    audit = prepare_decision_audit(state, tmp_path, config=config)

    assert state["configuration_digest"] == configuration_digest(config)
    assert audit["run"]["configuration_digest"] == configuration_digest(config)


@pytest.mark.unit
def test_decision_audit_projects_hashed_source_artifact(tmp_path):
    content = "The effective-date close was 4.31."
    source_ref = "issuer_filing:AAPL:2026-07-18"
    artifact_digest = sha256(content.encode()).hexdigest()
    source_artifact = SourceArtifact(
        artifact_sha256=artifact_digest,
        source_ref=source_ref,
        tool_call_id="call-filing",
        tool_name="issuer_filing",
        raw_text=content,
    )
    envelope = ToolExecutionEvidenceEnvelope(
        tool_call_id="call-filing",
        tool_name="issuer_filing",
        source_ref=source_ref,
        capability="issuer_filing",
        acquisition_outcomes=(
            SourceAcquisitionAvailable(
                provider="fixture-provider",
                capability="issuer_filing",
                source_ref=source_ref,
                attempt=1,
                retrieved_at="2026-07-18T12:00:00+00:00",
                artifact=source_artifact,
            ),
        ),
        selected_artifact=source_artifact,
    )
    claim = MaterialClaim(
        claim_id="fundamentals.close",
        analyst="fundamentals",
        statement=content,
        source_quote=content,
        source_refs=(source_ref,),
    )
    evidence = build_tool_evidence_state(
        (
            ToolMessage(
                content=content,
                name="issuer_filing",
                tool_call_id="call-filing",
                artifact=envelope.model_dump(mode="json"),
            ),
        ),
        (claim,),
        tool_call_ids_by_source={source_ref: ("call-filing",)},
    )
    state = _outcome_state()
    state["evidence_state"] = evidence.model_dump(mode="json")

    audit = prepare_decision_audit(state, tmp_path)
    [artifact] = audit["evidence_state"]["artifact_manifest"]["artifacts"]
    fact = audit["evidence_state"]["source_facts"][0]
    digest = artifact_digest
    assert artifact["artifact_ref"] == f"artifact=sha256:{digest}"
    assert fact["artifact_ref"] == artifact["artifact_ref"]
    serialized = json.dumps(audit, ensure_ascii=False, sort_keys=True)
    assert content not in serialized
    stored_text = gzip.decompress(
        (
            tmp_path
            / "evidence_artifacts"
            / "sha256"
            / digest[:2]
            / f"{digest}.utf8.gz"
        ).read_bytes()
    ).decode("utf-8")
    assert stored_text[fact["source_span_start"] : fact["source_span_end"]] == (
        content
    )


@pytest.mark.unit
def test_save_reports_explicit_path(tmp_path):
    out = TradingAgentsGraph.save_reports(
        None,
        _decision_state(),
        "AAPL",
        save_path=tmp_path,
    )
    assert out == tmp_path / "complete_report.md"


@pytest.mark.unit
def test_save_reports_defaults_under_results_dir(tmp_path):
    graph = SimpleNamespace(config={"results_dir": str(tmp_path)})
    out = TradingAgentsGraph.save_reports(graph, _decision_state(), "AAPL")
    assert out.exists()
    assert out.parent.parent.name == "reports"
    assert out.parent.name.startswith("AAPL_")


@pytest.mark.unit
def test_terminal_kind_enum_values_are_export_contract_values():
    assert TerminalOutcomeKind.TRADING_DECISION.value == "trading_decision"
    assert TerminalOutcomeKind.ANALYSIS_OUTCOME.value == "analysis_outcome"

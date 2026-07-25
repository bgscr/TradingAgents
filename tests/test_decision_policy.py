from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st
from pydantic import TypeAdapter, ValidationError

from tradingagents.agents.schemas import PortfolioDecisionSelection, PortfolioRating
from tradingagents.decision_policy import (
    AdmittedEvidenceBinding,
    CanonicalFactAdapterResult,
    DecisionContextBlocked,
    DecisionContextBuildResult,
    DecisionContextBuilt,
    DecisionGateResultV2,
    DecisionHorizon,
    DecisionPolicyEngine,
    DirectionSelection,
    EvidenceIntegrityStatus,
    HorizonUnit,
    MissingFactBehavior,
    RuleApplicationRequest,
    RulePolarity,
    RulePredicateResult,
    StrategyRuleDefinition,
    ValidatedDecisionContext,
    stable_decision_value_digest,
    stable_evidence_semantic_digest,
)
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    AnalysisDiagnosticCode,
    CalculationDefinition,
    CalculationLineage,
    ClaimValidation,
    ClaimValidationStatus,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MarketSnapshotEvidence,
    MaterialClaim,
    MissingValuePolicy,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    SourceFact,
    stable_market_snapshot_id,
    stable_source_fact_id,
)

_ALPHANUMERIC_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)
_ADVISORY_TEXT_ALPHABET = f"{_ALPHANUMERIC_ALPHABET} "

HORIZON = DecisionHorizon(count=20, unit=HorizonUnit.TRADING_DAYS)
TEST_RUN_ID = "run:" + "1" * 64
ARTIFACT_TEXT = "P/E was 9.5; P/B was 1.2."
ARTIFACT_SHA256 = sha256(ARTIFACT_TEXT.encode()).hexdigest()
SOURCE_REF = "market:600895.SS:2026-07-18"


def _fact_span(field: str) -> tuple[int, int]:
    if field == "pb_ratio":
        return ARTIFACT_TEXT.index("P/B"), len(ARTIFACT_TEXT)
    return 0, ARTIFACT_TEXT.index(" P/B")


def _fact_id(
    field: str = "pe_ratio",
    *,
    symbol: str = "600895.SS",
    effective_date: str = "2026-07-18",
) -> str:
    span_start, span_end = _fact_span(field)
    return stable_source_fact_id(
        source_ref=SOURCE_REF,
        artifact_sha256=ARTIFACT_SHA256,
        source_span_start=span_start,
        source_span_end=span_end,
        canonical_field=field,
        instrument_symbol=symbol,
        effective_date=effective_date,
    )


PE_FACT_ID = _fact_id()
_AUTO_LINEAGE = object()


def _rule(
    *,
    rule_id: str = "pe.low",
    version: str = "1",
    field: str = "pe_ratio",
    rating: PortfolioRating = PortfolioRating.BUY,
    polarity: RulePolarity = RulePolarity.SUPPORTS,
    horizon: DecisionHorizon = HORIZON,
    max_age: int | None = 5,
    minimum_history_rows: int = 1,
    missing_fact_behavior: MissingFactBehavior = MissingFactBehavior.BLOCK,
) -> StrategyRuleDefinition:
    return StrategyRuleDefinition(
        rule_id=rule_id,
        version=version,
        applicable_instrument_kinds=(InstrumentKind.EQUITY,),
        required_canonical_fields=(field,),
        target_rating=rating,
        polarity=polarity,
        horizon=horizon,
        predicate_id="decimal.less_than",
        comparator="lt",
        threshold=Decimal("12"),
        max_fact_age_days=max_age,
        minimum_history_rows=minimum_history_rows,
        missing_fact_behavior=missing_fact_behavior,
        implementation_version="test-1",
    )


def _evaluator(
    rule: StrategyRuleDefinition, facts, as_of_date: date
) -> RulePredicateResult:
    satisfied = Decimal(str(facts[0].normalized_value)) < rule.threshold
    return RulePredicateResult(
        satisfied=satisfied,
        fact_ids=tuple(fact.fact_id for fact in facts),
        evaluation_digest=(
            f"eval:{rule.rule_id}:{rule.version}:{facts[0].normalized_value}:"
            f"{as_of_date.isoformat()}"
        ),
    )


def _canonical_fact_adapter(
    artifact: SourceArtifact, span_start: int, span_end: int
) -> CanonicalFactAdapterResult:
    excerpt = artifact.raw_text[span_start:span_end]
    if excerpt == "P/E was 9.5;":
        return CanonicalFactAdapterResult(
            canonical_field="pe_ratio",
            normalized_value=Decimal("9.5"),
            unit="ratio",
        )
    if excerpt == "P/B was 1.2.":
        return CanonicalFactAdapterResult(
            canonical_field="pb_ratio",
            normalized_value=Decimal("1.2"),
            unit="ratio",
        )
    raise ValueError("unrecognized canonical record")


def _artifact_resolver(artifact_sha256: str, source_ref: str) -> SourceArtifact | None:
    if artifact_sha256 != ARTIFACT_SHA256 or source_ref != SOURCE_REF:
        return None
    return SourceArtifact(
        artifact_sha256=ARTIFACT_SHA256,
        source_ref=SOURCE_REF,
        tool_call_id="resolver-runtime-call",
        tool_name="provider",
        raw_text=ARTIFACT_TEXT,
    )


def _calculation_definition(field: str) -> CalculationDefinition:
    return CalculationDefinition(
        calculation_id=f"{field}.adapter",
        version="1",
        input_fields=("artifact_excerpt",),
        input_frequency="event",
        minimum_history_rows=1,
        warmup_rows=0,
        adjustment_basis="source-reported",
        missing_value_policy=MissingValuePolicy.FAIL,
        formula=f"trusted test adapter for {field}",
        implementation_version="test-adapter-1",
        output_field=field,
        output_unit="ratio",
        precision=6,
    )


def _fact(
    *,
    fact_id: str | None = None,
    field: str = "pe_ratio",
    value: Decimal = Decimal("9.5"),
    symbol: str = "600895.SS",
    effective_date: str = "2026-07-18",
    raw_text: str | None = None,
    tool_call_id: str = "runtime-call-1",
    calculation_lineage: CalculationLineage | None | object = _AUTO_LINEAGE,
    fact_kind: str = "canonical",
) -> SourceFact:
    span_start, span_end = _fact_span(field)
    if calculation_lineage is _AUTO_LINEAGE:
        calculation_lineage = CalculationLineage(
            calculation_id=f"{field}.adapter",
            calculation_version="1",
            input_artifact_sha256=ARTIFACT_SHA256,
            input_snapshot_id="artifact-record",
            effective_range_start=effective_date,
            effective_range_end=effective_date,
            observations_used=1,
            adjustment_basis="source-reported",
            implementation_version="test-adapter-1",
            result_digest=stable_decision_value_digest(
                canonical_field=field,
                normalized_value=value,
                unit="ratio",
            ),
        )
    assert calculation_lineage is None or isinstance(
        calculation_lineage, CalculationLineage
    )
    return SourceFact(
        fact_kind=fact_kind,
        fact_id=fact_id or _fact_id(field, symbol=symbol, effective_date=effective_date),
        source_ref=SOURCE_REF,
        tool_call_id=tool_call_id,
        tool_name="provider",
        artifact_sha256=ARTIFACT_SHA256,
        raw_text=raw_text or ARTIFACT_TEXT[span_start:span_end],
        source_span_start=span_start,
        source_span_end=span_end,
        calculation_ids=(calculation_lineage.calculation_id,)
        if calculation_lineage is not None
        else (),
        canonical_field=field,
        normalized_value=value,
        unit="ratio",
        instrument_symbol=symbol,
        effective_date=effective_date,
        calculation_lineage=calculation_lineage,
    )


def _evidence(*facts: SourceFact) -> EvidenceState:
    artifacts = tuple(
        SourceArtifact(
            artifact_sha256=artifact_sha256,
            source_ref=source_ref,
            tool_call_id="artifact-runtime-call",
            tool_name="provider",
            raw_text=ARTIFACT_TEXT,
        )
        for artifact_sha256, source_ref in sorted(
            {(fact.artifact_sha256, fact.source_ref) for fact in facts}
        )
    )
    return EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="600895.SS",
            venue="SSE",
            instrument_kind=InstrumentKind.EQUITY,
            currency="CNY",
            display_name="Shanghai Zhangjiang Hi-Tech Park Development",
            provenance=IdentityProvenance(
                provider="security-master",
                source_ref="sse:600895",
                retrieved_at="2026-07-19T12:00:00+00:00",
                artifact_sha256="c" * 64,
            ),
        ),
        source_facts=facts,
        source_artifacts=artifacts,
    )


def _application(
    *, rule_id: str = "pe.low", version: str = "1", fact_id: str = PE_FACT_ID
) -> RuleApplicationRequest:
    return RuleApplicationRequest(
        rule_id=rule_id,
        rule_version=version,
        fact_ids=(fact_id,),
    )


def _engine(*rules: StrategyRuleDefinition) -> DecisionPolicyEngine:
    return DecisionPolicyEngine(
        rules or (_rule(),),
        evaluators={"decimal.less_than": _evaluator},
        calculation_definitions=(
            _calculation_definition("pe_ratio"),
            _calculation_definition("pb_ratio"),
            _calculation_definition("beta"),
        ),
        fact_adapters={
            ("pe_ratio.adapter", "1"): _canonical_fact_adapter,
            ("pb_ratio.adapter", "1"): _canonical_fact_adapter,
            ("beta.adapter", "1"): _canonical_fact_adapter,
        },
        artifact_resolver=_artifact_resolver,
    )


def _build(
    engine: DecisionPolicyEngine | None = None,
    evidence: EvidenceState | None = None,
    applications: tuple[RuleApplicationRequest, ...] | None = None,
    horizon: DecisionHorizon = HORIZON,
):
    policy = engine or _engine()
    run_evidence = evidence or _evidence(_fact())
    result = policy.build_context(
        run_evidence,
        applications or (_application(),),
        horizon=horizon,
        as_of_date=date(2026, 7, 19),
    )
    return result


def _gate(
    engine: DecisionPolicyEngine,
    context: ValidatedDecisionContext,
    proposal: DirectionSelection,
    evidence: EvidenceState,
    *,
    binding=None,
    run_id: str = TEST_RUN_ID,
    expected_run_id: str | None = None,
):
    admitted_binding = binding or engine.admit_evidence(
        evidence,
        context,
        run_id=run_id,
    )
    return engine.gate(
        context,
        proposal,
        evidence=evidence,
        admitted_evidence_binding=admitted_binding,
        run_id=run_id,
        expected_run_id=expected_run_id or run_id,
    )


def test_admission_returns_a_checkpoint_binding_for_the_run_and_context():
    engine = _engine()
    evidence = _evidence(_fact())
    built = engine.build_context(
        evidence,
        (_application(),),
        horizon=HORIZON,
        as_of_date=date(2026, 7, 19),
    )
    assert isinstance(built, DecisionContextBuilt)

    binding = engine.admit_evidence(
        evidence,
        built.context,
        run_id="run:" + "2" * 64,
    )

    assert binding.run_id == "run:" + "2" * 64
    assert binding.evidence_semantic_digest == stable_evidence_semantic_digest(
        evidence
    )
    assert binding.context_id == built.context.context_id
    assert binding.registry_digest == engine.registry_digest
    assert (
        binding.calculation_registry_digest
        == engine.calculation_registry_digest
    )
    assert binding.integrity_status is built.context.integrity_status


def test_policy_gate_fails_closed_without_checkpoint_admission_binding():
    engine = _engine()
    evidence = _evidence(_fact())
    built = _build(engine=engine, evidence=evidence)
    assert isinstance(built, DecisionContextBuilt)
    proposal = DirectionSelection(
        context_id=built.context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=(built.context.assertions[0].assertion_id,),
    )
    result = engine.gate(built.context, proposal, evidence=evidence)

    assert result.permitted is False
    assert "decision admitted evidence unavailable" in result.diagnostics


def test_distinct_run_bindings_are_isolated_on_one_policy_instance():
    engine = _engine()
    evidence = _evidence(_fact())
    built = _build(engine=engine, evidence=evidence)
    assert isinstance(built, DecisionContextBuilt)
    proposal = DirectionSelection(
        context_id=built.context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=(built.context.assertions[0].assertion_id,),
    )
    binding_a = engine.admit_evidence(
        evidence,
        built.context,
        run_id="run:" + "a" * 64,
    )
    binding_b = engine.admit_evidence(
        evidence,
        built.context,
        run_id="run:" + "b" * 64,
    )

    result_b = _gate(
        engine,
        built.context,
        proposal,
        evidence,
        binding=binding_b,
        run_id=binding_b.run_id,
    )
    result_a = _gate(
        engine,
        built.context,
        proposal,
        evidence,
        binding=binding_a,
        run_id=binding_a.run_id,
    )
    crossed = _gate(
        engine,
        built.context,
        proposal,
        evidence,
        binding=binding_b,
        run_id=binding_a.run_id,
    )

    assert result_a.permitted is True
    assert result_b.permitted is True
    assert crossed.permitted is False
    assert "decision admitted evidence run mismatch" in crossed.diagnostics


def test_run_identity_and_binding_cannot_be_mutated_together():
    engine = _engine()
    evidence = _evidence(_fact())
    built = _build(engine=engine, evidence=evidence)
    assert isinstance(built, DecisionContextBuilt)
    proposal = DirectionSelection(
        context_id=built.context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=(built.context.assertions[0].assertion_id,),
    )
    binding = engine.admit_evidence(
        evidence,
        built.context,
        run_id=TEST_RUN_ID,
    )
    mutated_run_id = "run:" + "c" * 64
    mutated_binding = AdmittedEvidenceBinding.create(
        run_id=mutated_run_id,
        evidence_semantic_digest=binding.evidence_semantic_digest,
        context_id=binding.context_id,
        registry_digest=binding.registry_digest,
        calculation_registry_digest=binding.calculation_registry_digest,
        integrity_status=binding.integrity_status,
    )

    result = _gate(
        engine,
        built.context,
        proposal,
        evidence,
        binding=mutated_binding,
        run_id=mutated_run_id,
        expected_run_id=TEST_RUN_ID,
    )

    assert result.permitted is False
    assert "decision admitted evidence run mismatch" in result.diagnostics


def test_admission_binding_mutations_fail_closed():
    engine = _engine()
    evidence = _evidence(_fact())
    built = _build(engine=engine, evidence=evidence)
    assert isinstance(built, DecisionContextBuilt)
    proposal = DirectionSelection(
        context_id=built.context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=(built.context.assertions[0].assertion_id,),
    )
    binding = engine.admit_evidence(
        evidence,
        built.context,
        run_id=TEST_RUN_ID,
    )
    mutations = (
        (
            binding.model_copy(update={"run_id": "run:" + "d" * 64}),
            "decision admitted evidence run mismatch",
        ),
        (
            binding.model_copy(
                update={"evidence_semantic_digest": "evidence:" + "0" * 64}
            ),
            "decision admitted evidence mismatch",
        ),
        (
            binding.model_copy(update={"context_id": "context:" + "0" * 64}),
            "decision admitted evidence context mismatch",
        ),
        (
            binding.model_copy(
                update={"registry_digest": "registry:" + "0" * 64}
            ),
            "decision admitted evidence registry mismatch",
        ),
        (
            binding.model_copy(
                update={
                    "calculation_registry_digest": "calculations:" + "0" * 64
                }
            ),
            "decision admitted evidence calculation registry mismatch",
        ),
        (
            binding.model_copy(
                update={"integrity_status": EvidenceIntegrityStatus.DEGRADED}
            ),
            "decision admitted evidence integrity mismatch",
        ),
    )

    for mutated, diagnostic in mutations:
        result = _gate(
            engine,
            built.context,
            proposal,
            evidence,
            binding=mutated,
        )
        assert result.permitted is False
        assert diagnostic in result.diagnostics


def test_registry_digest_is_order_independent_and_duplicate_rules_are_rejected():
    first = _rule()
    second = _rule(rule_id="pb.low", field="pb_ratio")
    assert _engine(first, second).registry_digest == _engine(second, first).registry_digest
    with pytest.raises(ValueError, match="duplicate strategy rule"):
        _engine(first, first)


def test_registry_digest_canonicalizes_set_like_rule_fields():
    first = _rule().model_copy(
        update={
            "applicable_instrument_kinds": (
                InstrumentKind.EQUITY,
                InstrumentKind.FUND,
            ),
            "required_canonical_fields": ("pe_ratio", "pb_ratio"),
        }
    )
    permuted = first.model_copy(
        update={
            "applicable_instrument_kinds": (
                InstrumentKind.FUND,
                InstrumentKind.EQUITY,
            ),
            "required_canonical_fields": ("pb_ratio", "pe_ratio"),
        }
    )
    assert _engine(first).registry_digest == _engine(permuted).registry_digest


def test_empty_registry_fails_closed():
    result = DecisionPolicyEngine().build_context(
        _evidence(_fact()),
        (_application(),),
        horizon=HORIZON,
        as_of_date=date(2026, 7, 19),
    )
    assert isinstance(result, DecisionContextBlocked)
    assert result.blocker_codes == ("no_applicable_registered_strategy_rule",)


def test_configuration_blockers_report_default_and_partial_wiring():
    assert DecisionPolicyEngine().configuration_blockers == (
        "no_applicable_registered_strategy_rule",
        "no_registered_calculation_definition",
    )
    partial = DecisionPolicyEngine(
        rules=(_rule(),),
        calculation_definitions=(_calculation_definition("pe_ratio"),),
    )
    assert partial.configuration_blockers == (
        "canonical_fact_adapter_required",
        "strategy_rule_evaluator_unavailable",
    )


def test_fully_injected_engine_has_no_configuration_blockers():
    assert _engine().configuration_blockers == ()


def test_registered_rules_deterministically_produce_candidate_applications():
    engine = _engine()
    evidence = _evidence(_fact())

    applications = engine.candidate_applications(evidence, horizon=HORIZON)
    result = engine.build_context(
        evidence,
        applications,
        horizon=HORIZON,
        as_of_date=date(2026, 7, 19),
        tolerate_unsatisfied_applications=True,
    )

    assert applications == (_application(),)
    assert isinstance(result, DecisionContextBuilt)


def test_candidate_mode_skips_false_predicates_but_explicit_selection_blocks():
    rule = _rule().model_copy(update={"threshold": Decimal("5")})
    engine = _engine(rule)
    evidence = _evidence(_fact())
    applications = engine.candidate_applications(evidence, horizon=HORIZON)

    discovered = engine.build_context(
        evidence,
        applications,
        horizon=HORIZON,
        as_of_date=date(2026, 7, 19),
        tolerate_unsatisfied_applications=True,
    )
    explicit = engine.build_context(
        evidence,
        applications,
        horizon=HORIZON,
        as_of_date=date(2026, 7, 19),
    )

    assert isinstance(discovered, DecisionContextBlocked)
    assert discovered.blocker_codes == ("no_applicable_registered_strategy_rule",)
    assert isinstance(explicit, DecisionContextBlocked)
    assert explicit.blocker_codes == ("strategy_rule_predicate_not_satisfied",)


def test_candidate_application_enumeration_is_bounded():
    evidence = _evidence(
        _fact(),
        _fact(effective_date="2026-07-17"),
    )
    with pytest.raises(ValueError, match="candidate limit"):
        _engine().candidate_applications(
            evidence,
            horizon=HORIZON,
            max_candidates=1,
        )


def test_selected_fact_must_bind_to_a_content_addressed_artifact():
    evidence = _evidence(_fact()).model_copy(update={"source_artifacts": ()})
    result = _build(evidence=evidence)
    assert isinstance(result, DecisionContextBlocked)
    assert "selected_source_fact_artifact_missing" in result.blocker_codes


def test_excerpt_fact_cannot_enter_validated_decision_context():
    result = _build(evidence=_evidence(_fact(fact_kind="excerpt")))

    assert isinstance(result, DecisionContextBlocked)
    assert result.blocker_codes == ("selected_source_fact_missing",)


def _forge_value_and_self_attestation(fact: SourceFact) -> SourceFact:
    forged_value = Decimal("61.8")
    assert fact.calculation_lineage is not None
    forged_lineage = fact.calculation_lineage.model_copy(
        update={
            "result_digest": stable_decision_value_digest(
                canonical_field=fact.canonical_field,
                normalized_value=forged_value,
                unit=fact.unit,
            )
        }
    )
    return fact.model_copy(
        update={
            "normalized_value": forged_value,
            "calculation_lineage": forged_lineage,
        }
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda fact: fact.model_copy(update={"raw_text": "P/E was 8.5;"}),
            "selected_source_fact_excerpt_mismatch",
        ),
        (
            lambda fact: fact.model_copy(update={"fact_id": "fact:" + "f" * 64}),
            "selected_source_fact_id_mismatch",
        ),
        (
            _forge_value_and_self_attestation,
            "canonical_fact_adapter_output_mismatch",
        ),
    ],
)
def test_forged_fact_content_identity_or_value_cannot_enter_context(mutate, code):
    fact = mutate(_fact())
    application = _application(fact_id=fact.fact_id)
    result = _build(evidence=_evidence(fact), applications=(application,))
    assert isinstance(result, DecisionContextBlocked)
    assert code in result.blocker_codes


def test_unattested_legacy_normalized_fact_fails_closed():
    result = _build(evidence=_evidence(_fact(calculation_lineage=None)))
    assert isinstance(result, DecisionContextBlocked)
    assert (
        "canonical_fact_adapter_required" in result.blocker_codes
    )


def test_context_is_invariant_to_order_duplication_prose_and_runtime_ids():
    fact = _fact()
    mutated = fact.model_copy(
        update={
            "tool_call_id": "unrelated-runtime-call-999",
        }
    )
    unsupported_claim = MaterialClaim(
        claim_id="advisory.claim",
        analyst="advisory",
        statement="Sensational unsupported prose with a SELL instruction",
        source_refs=(SOURCE_REF,),
        source_quote="P/E was 9.5",
    )
    first_evidence = _evidence(fact)
    first = _build(evidence=first_evidence)
    second = _build(
        evidence=_evidence(mutated, mutated).model_copy(
            update={"material_claims": (unsupported_claim,)}
        ),
        applications=(_application(), _application()),
    )
    assert isinstance(first, DecisionContextBuilt)
    assert isinstance(second, DecisionContextBuilt)
    assert first.context == second.context
    serialized = first.context.model_dump_json()
    assert "raw_text" not in serialized
    assert "tool_call_id" not in serialized
    assert "SELL instruction" not in serialized


def test_context_is_invariant_to_fact_and_application_permutation():
    pe_rule = _rule()
    pb_rule = _rule(rule_id="pb.low", field="pb_ratio")
    engine = _engine(pe_rule, pb_rule)
    pe_fact = _fact()
    pb_fact = _fact(
        field="pb_ratio",
        value=Decimal("1.2"),
    )
    pe_application = _application()
    pb_application = _application(rule_id="pb.low", fact_id=_fact_id("pb_ratio"))
    first = _build(
        engine=engine,
        evidence=_evidence(pe_fact, pb_fact),
        applications=(pe_application, pb_application),
    )
    second = _build(
        engine=engine,
        evidence=_evidence(pb_fact, pe_fact),
        applications=(pb_application, pe_application),
    )
    assert isinstance(first, DecisionContextBuilt)
    assert isinstance(second, DecisionContextBuilt)
    assert first.context == second.context


@pytest.mark.parametrize(
    ("evidence", "applications", "horizon", "code"),
    [
        (
            _evidence(_fact(field="beta")),
            (_application(fact_id=_fact_id("beta")),),
            HORIZON,
            "canonical_fact_adapter_output_mismatch",
        ),
        (
            _evidence(_fact()),
            (_application(version="999"),),
            HORIZON,
            "strategy_rule_version_mismatch",
        ),
        (
            _evidence(_fact(symbol="510500.SS")),
            (_application(fact_id=_fact_id(symbol="510500.SS")),),
            HORIZON,
            "source_fact_instrument_mismatch",
        ),
        (
            _evidence(_fact(effective_date="2026-06-01")),
            (_application(fact_id=_fact_id(effective_date="2026-06-01")),),
            HORIZON,
            "source_fact_stale",
        ),
        (
            _evidence(_fact()),
            (_application(fact_id="fact:missing"),),
            HORIZON,
            "selected_source_fact_missing",
        ),
        (
            _evidence(_fact()),
            (_application(),),
            DecisionHorizon(count=30, unit=HorizonUnit.CALENDAR_DAYS),
            "strategy_rule_horizon_mismatch",
        ),
    ],
)
def test_context_build_blocks_incompatible_rule_applications(
    evidence, applications, horizon, code
):
    result = _build(evidence=evidence, applications=applications, horizon=horizon)
    assert isinstance(result, DecisionContextBlocked)
    assert code in result.blocker_codes


def test_context_build_fails_closed_on_missing_required_history():
    result = _build(engine=_engine(_rule(minimum_history_rows=200)))
    assert isinstance(result, DecisionContextBlocked)
    assert "source_fact_insufficient_history" in result.blocker_codes


def test_minimum_history_requires_lineage_on_every_selected_fact():
    pe_fact = _fact()
    assert pe_fact.calculation_lineage is not None
    pe_fact = pe_fact.model_copy(
        update={
            "calculation_lineage": pe_fact.calculation_lineage.model_copy(
                update={
                    "effective_range_start": "2025-01-01",
                    "observations_used": 200,
                }
            )
        }
    )
    rule = _rule(minimum_history_rows=200).model_copy(
        update={"required_canonical_fields": ("pb_ratio", "pe_ratio")}
    )
    evidence = _evidence(
        pe_fact,
        _fact(field="pb_ratio", value=Decimal("1.2")),
    )
    result = _build(
        engine=_engine(rule),
        evidence=evidence,
        applications=(
            RuleApplicationRequest(
                rule_id=rule.rule_id,
                rule_version=rule.version,
                fact_ids=(PE_FACT_ID, _fact_id("pb_ratio")),
            ),
        ),
    )
    assert isinstance(result, DecisionContextBlocked)
    assert "source_fact_insufficient_history" in result.blocker_codes


def test_not_satisfied_missing_behavior_skips_missing_candidate():
    missing_rule = _rule(
        rule_id="pb.missing",
        field="pb_ratio",
        missing_fact_behavior=MissingFactBehavior.NOT_SATISFIED,
    )
    valid_rule = _rule()
    engine = _engine(missing_rule, valid_rule)
    result = _build(
        engine=engine,
        applications=(
            _application(rule_id="pb.missing", fact_id="fact:not-present"),
            _application(),
        ),
    )
    assert isinstance(result, DecisionContextBuilt)
    assert tuple(assertion.rule_id for assertion in result.context.assertions) == (
        "pe.low",
    )


def test_gate_accepts_only_the_rule_supported_rating():
    engine = _engine()
    built = _build(engine=engine)
    assert isinstance(built, DecisionContextBuilt)
    assertion_id = built.context.assertions[0].assertion_id

    accepted = _gate(
        engine,
        built.context,
        DirectionSelection(
            context_id=built.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(assertion_id,),
        ),
        _evidence(_fact()),
    )
    swapped = _gate(
        engine,
        built.context,
        DirectionSelection(
            context_id=built.context.context_id,
            rating=PortfolioRating.SELL,
            assertion_ids=(assertion_id,),
        ),
        _evidence(_fact()),
    )
    assert accepted.permitted is True
    assert accepted.decision is not None
    assert accepted.decision.rating is PortfolioRating.BUY
    assert accepted.diagnostic_codes == ()
    assert swapped.permitted is False
    assert swapped.decision is None
    assert "assertion does not support the proposed rating" in swapped.diagnostics
    assert swapped.diagnostic_codes == (
        AnalysisDiagnosticCode.DECISION_ASSERTION_INVALID,
    )
    with pytest.raises(ValidationError, match="permitted gate"):
        DecisionGateResultV2.model_validate(
            {
                **accepted.model_dump(mode="json"),
                "diagnostic_codes": [
                    AnalysisDiagnosticCode.DETERMINISTIC_GATE_REJECTED.value
                ],
            }
        )


def test_gate_uses_configured_artifact_resolver_with_trusted_ledger():
    engine = _engine()
    built = _build(engine=engine)
    assert isinstance(built, DecisionContextBuilt)
    assertion_id = built.context.assertions[0].assertion_id

    result = _gate(
        engine,
        built.context,
        DirectionSelection(
            context_id=built.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(assertion_id,),
        ),
        _evidence(_fact()),
    )

    assert result.permitted is True


def test_gate_rejects_checkpoint_rewrite_of_required_source_conflict():
    engine = _engine()
    available_source = EvidenceSource(
        source_id="required.market",
        status=EvidenceStatus.AVAILABLE,
        required=True,
    )
    checkpoint_evidence = _evidence(_fact()).model_copy(
        update={"sources": (available_source,)}
    )
    built = engine.build_context(
        checkpoint_evidence,
        (_application(),),
        horizon=HORIZON,
        as_of_date=date(2026, 7, 19),
    )
    assert isinstance(built, DecisionContextBuilt)

    admitted_binding = engine.admit_evidence(
        checkpoint_evidence,
        built.context,
        run_id=TEST_RUN_ID,
    )
    rewritten_evidence = checkpoint_evidence.model_copy(
        update={
            "sources": (
                available_source.model_copy(
                    update={
                        "status": EvidenceStatus.CONFLICTED,
                        "detail": "material provider contradiction",
                    }
                ),
            )
        }
    )
    result = _gate(
        engine,
        built.context,
        DirectionSelection(
            context_id=built.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(built.context.assertions[0].assertion_id,),
        ),
        rewritten_evidence,
        binding=admitted_binding,
    )

    assert result.permitted is False
    assert result.decision is None
    assert "decision admitted evidence mismatch" in result.diagnostics


def test_gate_rejects_post_admission_evidence_ledger_rewrites():
    engine = _engine()
    optional_source = EvidenceSource(
        source_id="optional.news",
        status=EvidenceStatus.UNAVAILABLE,
        required=False,
        detail="provider timed out",
    )
    acquisition = SourceAcquisitionUnavailable(
        provider="news-provider",
        capability="news",
        source_ref="source:news",
        attempt=1,
        retrieved_at="2026-07-19T12:00:00+00:00",
        retryable=True,
        reason=AcquisitionUnavailableReason.TIMEOUT,
    )
    validation = ClaimValidation(
        claim_id="market.pe",
        status=ClaimValidationStatus.SUPPORTED,
        fact_ids=(PE_FACT_ID,),
    )
    admitted_evidence = _evidence(_fact()).model_copy(
        update={
            "sources": (optional_source,),
            "acquisition_outcomes": (acquisition,),
            "claim_validations": (validation,),
        }
    )
    built = _build(engine=engine, evidence=admitted_evidence)
    assert isinstance(built, DecisionContextBuilt)
    proposal = DirectionSelection(
        context_id=built.context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=(built.context.assertions[0].assertion_id,),
    )
    admitted_binding = engine.admit_evidence(
        admitted_evidence,
        built.context,
        run_id=TEST_RUN_ID,
    )

    rewrites = (
        admitted_evidence.model_copy(
            update={
                "sources": (
                    optional_source.model_copy(
                        update={"status": EvidenceStatus.AVAILABLE, "detail": ""}
                    ),
                )
            }
        ),
        admitted_evidence.model_copy(
            update={
                "acquisition_outcomes": (
                    acquisition.model_copy(
                        update={"reason": AcquisitionUnavailableReason.NO_DATA}
                    ),
                )
            }
        ),
        admitted_evidence.model_copy(
            update={
                "claim_validations": (
                    validation.model_copy(
                        update={"status": ClaimValidationStatus.UNSUPPORTED}
                    ),
                )
            }
        ),
    )

    for rewritten_evidence in rewrites:
        result = _gate(
            engine,
            built.context,
            proposal,
            rewritten_evidence,
            binding=admitted_binding,
        )
        assert result.permitted is False
        assert "decision admitted evidence mismatch" in result.diagnostics


def test_gate_rejects_as_of_date_after_trusted_snapshot_request_date():
    engine = _engine()
    snapshot_fields = {
        "symbol": "600895.SS",
        "provider": "test-market",
        "adjustment_basis": "adjusted",
        "requested_date": "2026-07-19",
        "effective_trading_date": "2026-07-18",
        "frame_sha256": "f" * 64,
        "history_rows": 250,
    }
    evidence = _evidence(_fact()).model_copy(
        update={
            "market_snapshot": MarketSnapshotEvidence(
                retrieved_at="2026-07-19T12:00:00+00:00",
                snapshot_id=stable_market_snapshot_id(**snapshot_fields),
                **snapshot_fields,
            )
        }
    )
    built = engine.build_context(
        evidence,
        (_application(),),
        horizon=HORIZON,
        as_of_date=date(2026, 7, 20),
    )
    assert isinstance(built, DecisionContextBuilt)

    result = _gate(
        engine,
        built.context,
        DirectionSelection(
            context_id=built.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(built.context.assertions[0].assertion_id,),
        ),
        evidence,
    )

    assert result.permitted is False
    assert result.decision is None


def test_opposing_and_multiple_rating_assertions_cannot_authorize_a_decision():
    opposing_engine = _engine(_rule(polarity=RulePolarity.OPPOSES))
    opposing = _build(engine=opposing_engine)
    assert isinstance(opposing, DecisionContextBuilt)
    opposing_result = _gate(
        opposing_engine,
        opposing.context,
        DirectionSelection(
            context_id=opposing.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(opposing.context.assertions[0].assertion_id,),
        ),
        _evidence(_fact()),
    )
    assert opposing_result.permitted is False
    assert "opposing assertions cannot authorize a rating" in opposing_result.diagnostics

    buy = _rule()
    sell = _rule(rule_id="pe.sell", rating=PortfolioRating.SELL)
    multiple_engine = _engine(buy, sell)
    multiple = _build(
        engine=multiple_engine,
        applications=(
            _application(),
            _application(rule_id="pe.sell"),
        ),
    )
    assert isinstance(multiple, DecisionContextBuilt)
    multiple_result = _gate(
        multiple_engine,
        multiple.context,
        DirectionSelection(
            context_id=multiple.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=tuple(
                assertion.assertion_id for assertion in multiple.context.assertions
            ),
        ),
        _evidence(_fact()),
    )
    assert multiple_result.permitted is False
    assert "selected assertions target multiple ratings" in multiple_result.diagnostics

    cherry_picked = _gate(
        multiple_engine,
        multiple.context,
        DirectionSelection(
            context_id=multiple.context.context_id,
            rating=multiple.context.assertions[0].target_rating,
            assertion_ids=(multiple.context.assertions[0].assertion_id,),
        ),
        _evidence(_fact()),
    )
    assert cherry_picked.permitted is False
    assert (
        "direction proposal does not cover every validated assertion"
        in cherry_picked.diagnostics
    )


def test_gate_recomputes_and_rejects_changed_evaluation_digest():
    calls = 0

    def unstable(rule, facts, as_of_date):
        nonlocal calls
        calls += 1
        return RulePredicateResult(
            satisfied=True,
            fact_ids=tuple(fact.fact_id for fact in facts),
            evaluation_digest=f"evaluation-{calls}",
        )

    engine = DecisionPolicyEngine(
        (_rule(),),
        evaluators={"decimal.less_than": unstable},
        calculation_definitions=(_calculation_definition("pe_ratio"),),
        fact_adapters={("pe_ratio.adapter", "1"): _canonical_fact_adapter},
        artifact_resolver=_artifact_resolver,
    )
    built = _build(engine=engine)
    assert isinstance(built, DecisionContextBuilt)
    result = _gate(
        engine,
        built.context,
        DirectionSelection(
            context_id=built.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(built.context.assertions[0].assertion_id,),
        ),
        _evidence(_fact()),
    )
    assert result.permitted is False
    assert "decision assertion reevaluation mismatch" in result.diagnostics


def test_gate_rejects_duplicate_context_members_and_preserves_conflicted_status():
    engine = _engine()
    built = _build(engine=engine)
    assert isinstance(built, DecisionContextBuilt)
    assertion = built.context.assertions[0]
    tampered = built.context.model_copy(
        update={
            "facts": (*built.context.facts, built.context.facts[0]),
            "assertions": (*built.context.assertions, assertion),
            "integrity_status": EvidenceIntegrityStatus.CONFLICTED,
        }
    )
    binding = engine.admit_evidence(
        _evidence(_fact()),
        built.context,
        run_id=TEST_RUN_ID,
    )
    result = _gate(
        engine,
        tampered,
        DirectionSelection(
            context_id=tampered.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(assertion.assertion_id,),
        ),
        _evidence(_fact()),
        binding=binding,
    )
    assert result.permitted is False
    assert result.integrity_status.value == "conflicted"
    assert "validated context contains duplicate fact ids" in result.diagnostics
    assert "validated context contains duplicate assertion ids" in result.diagnostics


def test_policy_models_are_closed_and_round_trip_through_json():
    built = _build()
    assert isinstance(built, DecisionContextBuilt)
    adapter = TypeAdapter(DecisionContextBuildResult)
    restored_build = adapter.validate_json(adapter.dump_json(built))
    restored_context = ValidatedDecisionContext.model_validate_json(
        built.context.model_dump_json()
    )
    assert restored_build == built
    assert restored_context == built.context
    with pytest.raises(ValidationError):
        DecisionHorizon.model_validate(
            {"count": 20, "unit": "trading_days", "prose": "buy now"}
        )


@pytest.mark.parametrize(
    "fixture_name",
    (
        "legacy_recorded_decision_600895_20260719.json",
        "legacy_recorded_decision_601658_20260719.json",
    ),
)
def test_legacy_recorded_claim_selection_replays_fail_closed_without_rules(
    fixture_name,
):
    fixture_path = Path(__file__).parent / "fixtures" / fixture_name
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert payload["fixture_kind"] == "legacy_recorded_input"
    selection = PortfolioDecisionSelection.model_validate(payload["selection"])
    claims = tuple(
        MaterialClaim.model_validate(claim) for claim in payload["selected_claims"]
    )
    claims_by_id = {claim.claim_id: claim for claim in claims}
    assert selection.material_claim_ids == tuple(claims_by_id)
    for assertion in selection.decision_assertions:
        assert assertion.fact_ids == claims_by_id[assertion.claim_id].fact_ids
        with pytest.raises(ValidationError):
            RuleApplicationRequest.model_validate(
                assertion.model_dump(mode="json")
            )
    with pytest.raises(ValidationError):
        DirectionSelection.model_validate(payload["selection"])

    legacy_evidence = EvidenceState(material_claims=claims)
    engine = DecisionPolicyEngine()
    applications = engine.candidate_applications(legacy_evidence, horizon=HORIZON)
    result = engine.build_context(
        legacy_evidence,
        applications,
        horizon=HORIZON,
        as_of_date=date.fromisoformat(payload["trade_date"]),
    )

    assert applications == ()
    assert isinstance(result, DecisionContextBlocked)


@settings(deadline=None)
@given(
    runtime_call_id=st.text(
        alphabet=_ALPHANUMERIC_ALPHABET,
        min_size=1,
        max_size=32,
    )
)
def test_metamorphic_context_ignores_runtime_call_ids(runtime_call_id):
    baseline = _build()
    transformed = _build(evidence=_evidence(_fact(tool_call_id=runtime_call_id)))

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBuilt)
    assert transformed.context == baseline.context


@settings(deadline=None)
@given(
    value=st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    trailing_zeros=st.integers(min_value=1, max_value=6),
)
def test_metamorphic_equivalent_decimal_encodings_share_decision_value_digest(
    value,
    trailing_zeros,
):
    equivalent_encoding = Decimal(f"{format(value, 'f')}{'0' * trailing_zeros}")

    baseline = stable_decision_value_digest(
        canonical_field="pe_ratio",
        normalized_value=value,
        unit="ratio",
    )
    transformed = stable_decision_value_digest(
        canonical_field="pe_ratio",
        normalized_value=equivalent_encoding,
        unit="ratio",
    )

    assert equivalent_encoding == value
    assert transformed == baseline


@settings(deadline=None)
@given(duplicate_count=st.integers(min_value=2, max_value=5))
def test_metamorphic_context_ignores_duplicate_facts_and_applications(duplicate_count):
    baseline = _build()
    fact = _fact()
    transformed = _build(
        evidence=_evidence(*(fact for _ in range(duplicate_count))),
        applications=tuple(_application() for _ in range(duplicate_count)),
    )

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBuilt)
    assert transformed.context == baseline.context


@settings(deadline=None)
@given(
    advisory_prose=st.text(
        alphabet=_ADVISORY_TEXT_ALPHABET,
        max_size=80,
    )
)
def test_metamorphic_context_ignores_advisory_prose(advisory_prose):
    baseline = _build()
    advisory = MaterialClaim(
        claim_id="advisory.untrusted",
        analyst="advisory",
        statement=advisory_prose or "SELL everything",
        source_refs=(SOURCE_REF,),
        source_quote="P/E was 9.5",
    )
    transformed = _build(
        evidence=_evidence(_fact()).model_copy(
            update={"material_claims": (advisory,)}
        ),
    )

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBuilt)
    assert transformed.context == baseline.context


@settings(deadline=None)
@given(
    source_id=st.text(
        alphabet=_ALPHANUMERIC_ALPHABET,
        min_size=1,
        max_size=32,
    ),
    unavailable_detail=st.text(
        alphabet=_ADVISORY_TEXT_ALPHABET,
        max_size=80,
    ),
)
def test_metamorphic_optional_unused_source_loss_preserves_direction(source_id, unavailable_detail):
    engine = _engine()
    available = EvidenceSource(
        source_id=source_id,
        status=EvidenceStatus.AVAILABLE,
        required=False,
    )
    unavailable = available.model_copy(
        update={
            "status": EvidenceStatus.UNAVAILABLE,
            "detail": unavailable_detail,
        }
    )
    baseline_evidence = _evidence(_fact()).model_copy(
        update={"sources": (available,)}
    )
    transformed_evidence = _evidence(_fact()).model_copy(
        update={"sources": (unavailable,)}
    )
    baseline = _build(
        engine=engine,
        evidence=baseline_evidence,
    )
    transformed = _build(
        engine=engine,
        evidence=transformed_evidence,
    )

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBuilt)
    assert baseline.context.integrity_status is EvidenceIntegrityStatus.DECISION_READY
    assert transformed.context.integrity_status is EvidenceIntegrityStatus.DEGRADED
    assert transformed.context.facts == baseline.context.facts
    assert transformed.context.assertions == baseline.context.assertions

    baseline_gate = _gate(
        engine,
        baseline.context,
        DirectionSelection(
            context_id=baseline.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(baseline.context.assertions[0].assertion_id,),
        ),
        baseline_evidence,
        run_id="run:" + "e" * 64,
    )
    transformed_gate = _gate(
        engine,
        transformed.context,
        DirectionSelection(
            context_id=transformed.context.context_id,
            rating=PortfolioRating.BUY,
            assertion_ids=(transformed.context.assertions[0].assertion_id,),
        ),
        transformed_evidence,
        run_id="run:" + "f" * 64,
    )
    assert baseline_gate.permitted is True
    assert transformed_gate.permitted is True
    assert baseline_gate.decision is not None
    assert transformed_gate.decision is not None
    assert transformed_gate.decision.rating is baseline_gate.decision.rating
    assert transformed_gate.decision.facts == baseline_gate.decision.facts
    assert transformed_gate.decision.assertions == baseline_gate.decision.assertions


@settings(deadline=None)
@given(
    missing_suffix=st.text(
        alphabet=_ALPHANUMERIC_ALPHABET,
        min_size=1,
        max_size=32,
    )
)
def test_metamorphic_malformed_fact_reference_cannot_preserve_a_valid_context(
    missing_suffix,
):
    baseline = _build()
    transformed = _build(
        applications=(_application(fact_id=f"fact:missing:{missing_suffix}"),)
    )

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBlocked)


@settings(deadline=None)
@given(cross_field=st.sampled_from(("pb_ratio", "beta")))
def test_metamorphic_cross_bound_fact_reference_cannot_preserve_a_valid_context(
    cross_field,
):
    baseline = _build()
    cross_bound = _fact(field=cross_field, value=Decimal("1.2"))
    transformed = _build(
        evidence=_evidence(_fact(), cross_bound),
        applications=(_application(fact_id=cross_bound.fact_id),),
    )

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBlocked)


@settings(deadline=None)
@given(swapped_field=st.sampled_from(("pb_ratio", "beta")))
def test_metamorphic_canonical_field_semantic_swap_invalidates_fact_binding(
    swapped_field,
):
    baseline = _build()
    original = _fact()
    assert original.calculation_lineage is not None
    swapped_lineage = original.calculation_lineage.model_copy(
        update={
            "calculation_id": f"{swapped_field}.adapter",
            "result_digest": stable_decision_value_digest(
                canonical_field=swapped_field,
                normalized_value=original.normalized_value,
                unit=original.unit,
            ),
        }
    )
    swapped_fact_id = stable_source_fact_id(
        source_ref=original.source_ref,
        artifact_sha256=original.artifact_sha256,
        source_span_start=original.source_span_start,
        source_span_end=original.source_span_end,
        canonical_field=swapped_field,
        instrument_symbol=original.instrument_symbol,
        effective_date=original.effective_date,
    )
    swapped = original.model_copy(
        update={
            "fact_id": swapped_fact_id,
            "calculation_ids": (swapped_lineage.calculation_id,),
            "canonical_field": swapped_field,
            "calculation_lineage": swapped_lineage,
        }
    )
    transformed = _build(
        evidence=_evidence(swapped),
        applications=(_application(fact_id=swapped_fact_id),),
    )

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBlocked)


@settings(deadline=None)
@given(
    magnitude=st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("1000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_metamorphic_sign_change_invalidates_fact_binding(magnitude):
    baseline = _build()
    original = _fact()
    assert original.calculation_lineage is not None
    changed_value = -magnitude
    changed = original.model_copy(
        update={
            "normalized_value": changed_value,
            "calculation_lineage": original.calculation_lineage.model_copy(
                update={
                    "result_digest": stable_decision_value_digest(
                        canonical_field=original.canonical_field,
                        normalized_value=changed_value,
                        unit=original.unit,
                    )
                }
            ),
        }
    )
    transformed = _build(evidence=_evidence(changed))

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBlocked)


@settings(deadline=None)
@given(
    date_offset_days=st.one_of(
        st.integers(min_value=-365, max_value=-6),
        st.integers(min_value=1, max_value=365),
    )
)
def test_metamorphic_stale_or_future_effective_date_blocks_prior_context(
    date_offset_days,
):
    baseline = _build()
    original = _fact()
    assert original.calculation_lineage is not None
    changed_effective_date = (
        date(2026, 7, 19) + timedelta(days=date_offset_days)
    ).isoformat()
    changed_fact_id = stable_source_fact_id(
        source_ref=original.source_ref,
        artifact_sha256=original.artifact_sha256,
        source_span_start=original.source_span_start,
        source_span_end=original.source_span_end,
        canonical_field=original.canonical_field,
        instrument_symbol=original.instrument_symbol,
        effective_date=changed_effective_date,
    )
    changed = original.model_copy(
        update={
            "fact_id": changed_fact_id,
            "effective_date": changed_effective_date,
            "calculation_lineage": original.calculation_lineage.model_copy(
                update={
                    "effective_range_start": changed_effective_date,
                    "effective_range_end": changed_effective_date,
                }
            ),
        }
    )
    transformed = _build(
        evidence=_evidence(changed),
        applications=(_application(fact_id=changed_fact_id),),
    )

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBlocked)


@settings(deadline=None)
@given(order=st.permutations(("pe", "pb")))
def test_metamorphic_context_is_invariant_to_fact_and_application_order(order):
    rules = {
        "pe": _rule(),
        "pb": _rule(rule_id="pb.low", field="pb_ratio"),
    }
    facts = {
        "pe": _fact(),
        "pb": _fact(field="pb_ratio", value=Decimal("1.2")),
    }
    applications = {
        "pe": _application(),
        "pb": _application(rule_id="pb.low", fact_id=_fact_id("pb_ratio")),
    }
    engine = _engine(rules["pe"], rules["pb"])
    baseline = _build(
        engine=engine,
        evidence=_evidence(facts["pe"], facts["pb"]),
        applications=(applications["pe"], applications["pb"]),
    )
    transformed = _build(
        engine=engine,
        evidence=_evidence(*(facts[key] for key in order)),
        applications=tuple(applications[key] for key in order),
    )

    assert isinstance(baseline, DecisionContextBuilt)
    assert isinstance(transformed, DecisionContextBuilt)
    assert transformed.context == baseline.context


@settings(deadline=None)
@given(proposed_rating=st.sampled_from(tuple(PortfolioRating)))
def test_metamorphic_gate_accepts_exactly_the_rule_supported_rating(proposed_rating):
    engine = _engine()
    built = _build(engine=engine)
    assert isinstance(built, DecisionContextBuilt)
    result = _gate(
        engine,
        built.context,
        DirectionSelection(
            context_id=built.context.context_id,
            rating=proposed_rating,
            assertion_ids=(built.context.assertions[0].assertion_id,),
        ),
        _evidence(_fact()),
    )

    assert result.permitted is (proposed_rating is PortfolioRating.BUY)
    assert (result.decision is not None) is result.permitted


@settings(deadline=None)
@given(
    changed_value=st.decimals(
        min_value=Decimal("12.1"),
        max_value=Decimal("1000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_metamorphic_material_value_change_invalidates_prior_fact_binding(changed_value):
    original = _fact()
    assert original.calculation_lineage is not None
    changed = original.model_copy(
        update={
            "normalized_value": changed_value,
            "calculation_lineage": original.calculation_lineage.model_copy(
                update={
                    "result_digest": stable_decision_value_digest(
                        canonical_field=original.canonical_field,
                        normalized_value=changed_value,
                        unit=original.unit,
                    )
                }
            ),
        }
    )
    result = _build(evidence=_evidence(changed))

    assert isinstance(result, DecisionContextBlocked)
    assert "canonical_fact_adapter_output_mismatch" in result.blocker_codes

"""Focused contracts for the fail-closed, three-gate decision graph."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.managers.direction_selector import (
    create_decision_gate_node,
    create_direction_selector,
)
from tradingagents.agents.schemas import (
    PortfolioRating,
    ResearchPlan,
    TraderAction,
    TraderProposal,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.decision_policy import (
    CanonicalFactAdapterResult,
    DecisionAssertion,
    DecisionContextBlocked,
    DecisionContextBuilt,
    DecisionFact,
    DecisionGateResultV2,
    DecisionHorizon,
    DecisionInstrument,
    DecisionPolicyEngine,
    DirectionSelection,
    EvidenceIntegrityStatus,
    HorizonUnit,
    RuleApplicationRequest,
    RulePolarity,
    RulePredicateResult,
    StrategyRuleDefinition,
    TradingDecisionContract,
    ValidatedDecisionContext,
    stable_decision_value_digest,
)
from tradingagents.evidence import (
    AdmissionGateResult,
    AnalysisOutcome,
    CalculationDefinition,
    CalculationLineage,
    EvidenceReadiness,
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MarketSnapshotEvidence,
    MissingValuePolicy,
    SourceArtifact,
    SourceFact,
    render_analysis_outcome,
    stable_source_fact_id,
)
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.evidence_gate import (
    create_admission_gate_node,
    create_preflight_gate_node,
    route_after_preflight,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.signal_processing import SignalProcessor
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.terminal_contract import TerminalContract, TerminalOutcomeKind

HORIZON = DecisionHorizon(count=20, unit=HorizonUnit.TRADING_DAYS)


def _context() -> ValidatedDecisionContext:
    fact = DecisionFact(
        fact_id="fact:pe", canonical_field="pe_ratio", normalized_value=Decimal("9.5"),
        unit="ratio", instrument_symbol="NVDA", effective_date="2026-01-10",
        source_ref="market:NVDA", artifact_sha256="a" * 64,
        source_span_start=0, source_span_end=3,
    )
    assertion = DecisionAssertion(
        assertion_id="assertion:pe", rule_id="pe.low", rule_version="1",
        fact_ids=(fact.fact_id,), target_rating=PortfolioRating.BUY,
        polarity=RulePolarity.SUPPORTS, horizon=HORIZON,
        predicate_id="decimal.less_than", comparator="lt", threshold=Decimal("12"),
        evaluation_digest="evaluation:pe",
    )
    return ValidatedDecisionContext(
        context_id="context:NVDA", evidence_contract_version="1.0",
        registry_digest="registry:test", calculation_registry_digest="calculations:test",
        instrument=DecisionInstrument(
            symbol="NVDA", venue="XNAS", instrument_kind="equity", currency="USD"
        ),
        capability_profile_id="equity-v1", as_of_date=date(2026, 1, 10),
        horizon=HORIZON, facts=(fact,), assertions=(assertion,),
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
    )


def _selection(context: ValidatedDecisionContext) -> DirectionSelection:
    return DirectionSelection(
        context_id=context.context_id, rating=PortfolioRating.BUY,
        assertion_ids=(context.assertions[0].assertion_id,),
    )


def _permitted_policy_case() -> tuple[
    DecisionPolicyEngine,
    ValidatedDecisionContext,
    DirectionSelection,
    EvidenceState,
]:
    artifact_text = "P/E was 9.5;"
    artifact_sha256 = sha256(artifact_text.encode()).hexdigest()
    source_ref = "market:NVDA:2026-01-10"
    fact_id = stable_source_fact_id(
        source_ref=source_ref,
        artifact_sha256=artifact_sha256,
        source_span_start=0,
        source_span_end=len(artifact_text),
        canonical_field="pe_ratio",
        instrument_symbol="NVDA",
        effective_date="2026-01-10",
    )
    fact = SourceFact(
        fact_kind="canonical",
        fact_id=fact_id,
        source_ref=source_ref,
        tool_call_id="runtime-call",
        tool_name="provider",
        artifact_sha256=artifact_sha256,
        raw_text=artifact_text,
        source_span_start=0,
        source_span_end=len(artifact_text),
        calculation_ids=("pe_ratio.adapter",),
        canonical_field="pe_ratio",
        normalized_value=Decimal("9.5"),
        unit="ratio",
        instrument_symbol="NVDA",
        effective_date="2026-01-10",
        calculation_lineage=CalculationLineage(
            calculation_id="pe_ratio.adapter",
            calculation_version="1",
            input_artifact_sha256=artifact_sha256,
            input_snapshot_id="artifact-record",
            effective_range_start="2026-01-10",
            effective_range_end="2026-01-10",
            observations_used=1,
            adjustment_basis="source-reported",
            implementation_version="test-adapter-1",
            result_digest=stable_decision_value_digest(
                canonical_field="pe_ratio",
                normalized_value=Decimal("9.5"),
                unit="ratio",
            ),
        ),
    )
    artifact = SourceArtifact(
        artifact_sha256=artifact_sha256,
        source_ref=source_ref,
        tool_call_id="artifact-runtime-call",
        tool_name="provider",
        raw_text=artifact_text,
    )
    identity_artifact_text = '{"symbol":"NVDA","venue":"XNAS"}'
    identity_artifact_sha256 = sha256(identity_artifact_text.encode()).hexdigest()
    identity_artifact = SourceArtifact(
        artifact_sha256=identity_artifact_sha256,
        source_ref="nasdaq:NVDA",
        tool_call_id="identity-runtime-call",
        tool_name="security-master",
        raw_text=identity_artifact_text,
    )
    rule = StrategyRuleDefinition(
        rule_id="pe.low",
        version="1",
        applicable_instrument_kinds=(InstrumentKind.EQUITY,),
        required_canonical_fields=("pe_ratio",),
        target_rating=PortfolioRating.BUY,
        polarity=RulePolarity.SUPPORTS,
        horizon=HORIZON,
        predicate_id="decimal.less_than",
        comparator="lt",
        threshold=Decimal("12"),
        max_fact_age_days=5,
        minimum_history_rows=1,
        implementation_version="test-1",
    )
    calculation = CalculationDefinition(
        calculation_id="pe_ratio.adapter",
        version="1",
        input_fields=("artifact_excerpt",),
        input_frequency="event",
        minimum_history_rows=1,
        warmup_rows=0,
        adjustment_basis="source-reported",
        missing_value_policy=MissingValuePolicy.FAIL,
        formula="trusted test adapter for pe_ratio",
        implementation_version="test-adapter-1",
        output_field="pe_ratio",
        output_unit="ratio",
        precision=6,
    )

    def evaluator(rule, facts, as_of_date):
        return RulePredicateResult(
            satisfied=Decimal(str(facts[0].normalized_value)) < rule.threshold,
            fact_ids=(facts[0].fact_id,),
            evaluation_digest=f"eval:{facts[0].normalized_value}:{as_of_date.isoformat()}",
        )

    def adapter(resolved_artifact, span_start, span_end):
        assert resolved_artifact.raw_text[span_start:span_end] == artifact_text
        return CanonicalFactAdapterResult(
            canonical_field="pe_ratio",
            normalized_value=Decimal("9.5"),
            unit="ratio",
        )

    def resolver(digest, resolved_source_ref):
        if (digest, resolved_source_ref) == (artifact_sha256, source_ref):
            return artifact
        return None

    policy = DecisionPolicyEngine(
        rules=(rule,),
        evaluators={"decimal.less_than": evaluator},
        calculation_definitions=(calculation,),
        fact_adapters={("pe_ratio.adapter", "1"): adapter},
        artifact_resolver=resolver,
    )
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="NVDA",
            venue="XNAS",
            instrument_kind=InstrumentKind.EQUITY,
            currency="USD",
            provenance=IdentityProvenance(
                provider="security-master",
                source_ref="nasdaq:NVDA",
                retrieved_at="2026-01-10T12:00:00+00:00",
                artifact_sha256=identity_artifact_sha256,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="NVDA",
            provider="test",
            retrieved_at="2026-01-10T12:00:00+00:00",
            adjustment_basis="adjusted",
            requested_date="2026-01-10",
            effective_trading_date="2026-01-10",
            history_rows=250,
            frame_sha256="f" * 64,
            snapshot_id="snapshot:test",
        ),
        source_facts=(fact,),
        source_artifacts=(artifact, identity_artifact),
    )
    built = policy.build_context(
        evidence,
        (
            RuleApplicationRequest(
                rule_id=rule.rule_id,
                rule_version=rule.version,
                fact_ids=(fact.fact_id,),
            ),
        ),
        horizon=HORIZON,
        as_of_date=date(2026, 1, 10),
    )
    assert isinstance(built, DecisionContextBuilt)
    selection = DirectionSelection(
        context_id=built.context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=(built.context.assertions[0].assertion_id,),
    )
    assert policy.gate(built.context, selection).permitted is True
    return policy, built.context, selection, evidence


class _FixtureStructuredBinding:
    def __init__(self, schema, selection: DirectionSelection):
        self.schema = schema
        self.selection = selection

    def invoke(self, _prompt):
        if self.schema is ResearchPlan:
            return ResearchPlan(
                recommendation=PortfolioRating.BUY,
                rationale="Fixture debate supports the registered rule.",
                strategic_actions="Proceed to deterministic final selection.",
            )
        if self.schema is TraderProposal:
            return TraderProposal(
                action=TraderAction.BUY,
                reasoning="Fixture proposal remains outside the directional trust path.",
            )
        if self.schema is DirectionSelection:
            return self.selection
        raise AssertionError(f"unexpected structured invocation: {self.schema}")


class _FixtureBoundaryLLM:
    model_name = "fixture-boundary"

    def __init__(self, selection: DirectionSelection):
        self.selection = selection

    def with_structured_output(self, schema, **_kwargs):
        return _FixtureStructuredBinding(schema, self.selection)

    def bind_tools(self, _tools):
        return RunnableLambda(
            lambda _: AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "AnalystEvidenceReport",
                        "args": {
                            "report_markdown": (
                                "## Market Analysis\n\n"
                                "Canonical facts are already present."
                            ),
                            "material_claims": [],
                        },
                        "id": "market-report",
                        "type": "tool_call",
                    }
                ],
            )
        )

    def invoke(self, _prompt):
        return AIMessage(content="Fixture advisory debate response.")


class _PermittedPropagateHarness(TradingAgentsGraph):
    def __init__(self, tmp_path, policy, selection, evidence):
        self.debug = False
        self.config = {
            "checkpoint_enabled": False,
            "data_cache_dir": str(tmp_path / "cache"),
            "results_dir": str(tmp_path),
            "evidence_gate_mode": "enforce",
            "max_debate_rounds": 0,
            "max_risk_discuss_rounds": 0,
        }
        self.callbacks = []
        self.decision_policy = policy
        self.decision_horizon = HORIZON
        self.selected_analysts = ("market",)
        llm = _FixtureBoundaryLLM(selection)
        conditional_logic = ConditionalLogic(
            max_debate_rounds=0,
            max_risk_discuss_rounds=0,
        )
        def tool_node(state):
            return state

        self.graph_setup = GraphSetup(
            llm,
            llm,
            dict.fromkeys(
                ("market", "social", "news", "fundamentals"),
                tool_node,
            ),
            conditional_logic,
            evidence_gate_mode="enforce",
            decision_policy=policy,
            decision_horizon=HORIZON,
        )
        self.workflow = self.graph_setup.setup_graph(self.selected_analysts)
        self.graph = self.workflow.compile()
        self.propagator = Propagator()
        self.signal_processor = SignalProcessor()
        self.memory_log = TradingMemoryLog(
            {"memory_log_path": str(tmp_path / "memory.md")}
        )
        self.log_states_dict = {}
        self.curr_state = None
        self.ticker = None
        self._checkpointer_ctx = None
        self._fixture_evidence = evidence

    def resolve_evidence_state(self, ticker: str, trade_date: str) -> EvidenceState:
        assert (ticker, trade_date) == ("NVDA", "2026-01-10")
        return self._fixture_evidence


class _StructuredLLM:
    def __init__(self, result):
        self.result = result
        self.prompts: list[str] = []

    def with_structured_output(self, schema, include_raw=True):
        assert include_raw is True
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.result


@pytest.mark.unit
def test_unconfigured_preflight_short_circuits_without_downstream_model_or_hold():
    selector_llm = MagicMock()
    state = Propagator().create_initial_state("NVDA", "2026-01-10")
    update = create_preflight_gate_node()(state)

    assert update["evidence_preflight"]["passed"] is False
    assert route_after_preflight(update) == "blocked"
    assert "decision_horizon_not_configured" in update["evidence_preflight"]["blockers"]
    assert "Hold" not in update["analysis_outcome"]
    selector_llm.assert_not_called()


@pytest.mark.unit
def test_admission_empty_applications_short_circuits_policy(monkeypatch):
    normalized_value = Decimal("9.5")
    fact = SourceFact(
        fact_id=stable_source_fact_id(
            source_ref="market:NVDA",
            artifact_sha256="a" * 64,
            source_span_start=0,
            source_span_end=3,
            canonical_field="pe_ratio",
            instrument_symbol="NVDA",
            effective_date="2026-01-10",
        ),
        source_ref="market:NVDA",
        tool_call_id="call:test",
        tool_name="provider",
        artifact_sha256="a" * 64,
        raw_text="9.5",
        source_span_start=0,
        source_span_end=3,
        calculation_ids=("pe_ratio.adapter",),
        canonical_field="pe_ratio",
        normalized_value=normalized_value,
        unit="ratio",
        instrument_symbol="NVDA",
        effective_date="2026-01-10",
        calculation_lineage=CalculationLineage(
            calculation_id="pe_ratio.adapter",
            calculation_version="1",
            input_artifact_sha256="a" * 64,
            input_snapshot_id="snapshot:test",
            effective_range_start="2026-01-10",
            effective_range_end="2026-01-10",
            observations_used=1,
            adjustment_basis="source-reported",
            implementation_version="test-1",
            result_digest=stable_decision_value_digest(
                canonical_field="pe_ratio",
                normalized_value=normalized_value,
                unit="ratio",
            ),
        ),
    )
    policy = MagicMock()
    policy.candidate_applications.return_value = ()
    policy.build_context.return_value = DecisionContextBlocked(
        integrity_status=EvidenceIntegrityStatus.INSUFFICIENT,
        blocker_codes=("no_applicable_registered_strategy_rule",),
        diagnostics=("no applicable rule",),
    )
    monkeypatch.setattr(
        "tradingagents.graph.evidence_gate.evaluate_admission_gate",
        lambda evidence, minimum_history_rows: AdmissionGateResult.model_construct(
            admitted=True, readiness=EvidenceReadiness.DECISION_READY, diagnostics=()
        ),
    )

    update = create_admission_gate_node(policy, HORIZON)(
        {
            "evidence_state": EvidenceState(source_facts=(fact,)).model_dump(mode="json"),
            "trade_date": "2026-01-10",
        }
    )

    assert update["admission_gate"]["admitted"] is False
    assert "analysis_outcome" in update
    policy.build_context.assert_called_once()
    restored = policy.build_context.call_args.args[0].source_facts[0].normalized_value
    assert restored == normalized_value
    assert isinstance(restored, Decimal)
    assert policy.build_context.call_args.args[1] == ()
    assert policy.build_context.call_args.kwargs[
        "tolerate_unsatisfied_applications"
    ] is True


@pytest.mark.unit
def test_admission_discovers_registered_candidates_when_state_selection_is_empty(
    monkeypatch,
):
    context = _context()
    application = RuleApplicationRequest(
        rule_id="pe.low",
        rule_version="1",
        fact_ids=(context.facts[0].fact_id,),
    )
    policy = MagicMock()
    policy.candidate_applications.return_value = (application,)
    policy.build_context.return_value = DecisionContextBuilt(context=context)
    evidence = EvidenceState()
    monkeypatch.setattr(
        "tradingagents.graph.evidence_gate.evaluate_admission_gate",
        lambda evidence, minimum_history_rows: AdmissionGateResult.model_construct(
            admitted=True,
            readiness=EvidenceReadiness.DECISION_READY,
            diagnostics=(),
        ),
    )

    update = create_admission_gate_node(policy, HORIZON)(
        {
            "evidence_state": evidence.model_dump(mode="json"),
            "strategy_rule_applications": [],
            "trade_date": "2026-01-10",
        }
    )

    assert update["admission_gate"]["admitted"] is True
    assert update["validated_decision_context"] == context.model_dump(mode="json")
    policy.candidate_applications.assert_called_once()
    assert policy.build_context.call_args.args[1] == (application,)
    assert policy.build_context.call_args.kwargs[
        "tolerate_unsatisfied_applications"
    ] is True


@pytest.mark.unit
def test_compiled_admission_block_stops_all_post_analyst_model_boundaries():
    policy, _, _, _ = _permitted_policy_case()
    boundary_invocations: list[str] = []
    structured = MagicMock()
    structured.invoke.side_effect = lambda _: boundary_invocations.append("structured")
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    def complete_market_analysis(_):
        boundary_invocations.append("market_analyst")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "AnalystEvidenceReport",
                    "args": {
                        "report_markdown": "## Market Analysis\n\nNo rule-ready facts.",
                        "material_claims": [],
                    },
                    "id": "market-report",
                    "type": "tool_call",
                }
            ],
        )

    llm.bind_tools.return_value = RunnableLambda(complete_market_analysis)
    def tool_node(state):
        return state

    graph = GraphSetup(
        llm,
        llm,
        dict.fromkeys(("market", "social", "news", "fundamentals"), tool_node),
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        decision_policy=policy,
        decision_horizon=HORIZON,
    ).setup_graph(["market"]).compile()
    baseline = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="NVDA",
            venue="XNAS",
            instrument_kind=InstrumentKind.EQUITY,
            currency="USD",
            provenance=IdentityProvenance(
                provider="security-master",
                source_ref="nasdaq:NVDA",
                retrieved_at="2026-01-10T12:00:00+00:00",
                artifact_sha256="b" * 64,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="NVDA",
            provider="test",
            retrieved_at="2026-01-10T12:00:00+00:00",
            adjustment_basis="adjusted",
            requested_date="2026-01-10",
            effective_trading_date="2026-01-10",
            history_rows=250,
            frame_sha256="f" * 64,
            snapshot_id="snapshot:test",
        ),
    )

    result = graph.invoke(
        Propagator().create_initial_state(
            "NVDA",
            "2026-01-10",
            evidence_state=baseline,
        )
    )

    assert result["market_report"] == "## Market Analysis\n\nNo rule-ready facts."
    assert result["admission_gate"]["admitted"] is False
    assert boundary_invocations == ["market_analyst"]
    assert structured.invoke.call_count == 0
    assert result["analysis_outcome_contract"] is not None
    assert result["trading_decision"] is None
    assert result["final_trade_decision"] is None


@pytest.mark.unit
def test_public_propagate_publishes_audited_signal_and_memory(tmp_path):
    policy, context, selection, evidence = _permitted_policy_case()
    graph = _PermittedPropagateHarness(
        tmp_path,
        policy,
        selection,
        evidence,
    )

    final_state, signal = graph.propagate("NVDA", "2026-01-10")

    terminal = TerminalContract.model_validate(final_state["terminal_contract"])
    assert terminal.terminal_outcome_kind is TerminalOutcomeKind.TRADING_DECISION
    assert final_state["validated_decision_context"] == context.model_dump(mode="json")
    assert signal == PortfolioRating.BUY.value
    assert final_state["decision_audit_sha256"] == final_state["run_identity"][
        "audit_digest"
    ]
    assert final_state["decision_audit_path"]
    assert graph.curr_state is final_state
    [entry] = graph.memory_log.load_entries()
    assert entry["ticker"] == "NVDA"
    assert entry["rating"] == PortfolioRating.BUY.value
    assert entry["decision"] == final_state["final_trade_decision"]

    audit = json.loads(
        Path(final_state["decision_audit_path"]).read_text(encoding="utf-8")
    )
    state_log = json.loads(
        (
            tmp_path
            / "NVDA"
            / "TradingAgentsStrategy_logs"
            / "full_states_log_2026-01-10.json"
        ).read_text(encoding="utf-8")
    )
    assert state_log["evidence_audit_projection"] == audit["evidence_state"]
    assert state_log["decision_audit_sha256"] == audit["audit_sha256"]
    assert state_log["decision_audit_path"] == final_state["decision_audit_path"]
    serialized_log = json.dumps(state_log, ensure_ascii=False, sort_keys=True)
    assert "P/E was 9.5;" not in serialized_log
    for unsafe_key in (
        "evidence_state",
        "market_report",
        "sentiment_report",
        "news_report",
        "fundamentals_report",
        "investment_debate_state",
        "investment_plan",
        "trader_investment_decision",
        "risk_debate_state",
        "trading_decision",
        "final_trade_decision",
    ):
        assert unsafe_key not in state_log


@pytest.mark.unit
@pytest.mark.parametrize("envelope", [False, True])
def test_selector_uses_closed_context_for_direct_and_include_raw_results(envelope):
    context = _context()
    selection = _selection(context)
    result = (
        {"raw": object(), "parsed": selection, "parsing_error": None}
        if envelope
        else selection
    )
    llm = _StructuredLLM(result)
    state = {
        "validated_decision_context": context.model_dump(mode="json"),
        "market_report": "SENTINEL MARKET PROSE",
        "risk_debate_state": {"history": "SENTINEL RISK PROSE"},
    }

    update = create_direction_selector(llm)(state)

    assert update["direction_selection"] == selection.model_dump(mode="json")
    assert len(llm.prompts) == 1
    assert "SENTINEL" not in llm.prompts[0]
    assert context.context_id in llm.prompts[0]


@pytest.mark.unit
def test_semantic_gate_rejection_does_not_retry_selector_or_publish():
    context = _context()
    llm = _StructuredLLM(_selection(context))
    selection_update = create_direction_selector(llm)(
        {"validated_decision_context": context.model_dump(mode="json")}
    )
    swapped = {**selection_update["direction_selection"], "rating": PortfolioRating.SELL.value}
    policy = MagicMock()
    policy.gate.return_value = DecisionGateResultV2(
        permitted=False, integrity_status=EvidenceIntegrityStatus.INSUFFICIENT,
        diagnostics=("rating does not match assertions",),
    )

    update = create_decision_gate_node(policy)(
        {"validated_decision_context": context.model_dump(mode="json"), "direction_selection": swapped}
    )

    assert len(llm.prompts) == 1
    assert update["decision_gate"]["permitted"] is False
    gated_context = policy.gate.call_args.args[0]
    assert isinstance(gated_context.facts[0].normalized_value, Decimal)
    assert "trading_decision" not in update
    assert "final_trade_decision" not in update


@pytest.mark.unit
def test_permitted_gate_publishes_only_deterministic_contract_and_render():
    context = _context()
    decision = TradingDecisionContract(
        decision_id="decision:NVDA", context_id=context.context_id,
        registry_digest=context.registry_digest,
        calculation_registry_digest=context.calculation_registry_digest,
        instrument=context.instrument, as_of_date=context.as_of_date, horizon=context.horizon,
        rating=PortfolioRating.BUY, facts=context.facts, assertions=context.assertions,
        integrity_status=EvidenceIntegrityStatus.DECISION_READY,
    )
    policy = MagicMock()
    policy.gate.return_value = DecisionGateResultV2(
        permitted=True, integrity_status=EvidenceIntegrityStatus.DECISION_READY,
        diagnostics=(), decision=decision,
    )

    update = create_decision_gate_node(policy)(
        {
            "validated_decision_context": context.model_dump(mode="json"),
            "direction_selection": _selection(context).model_dump(mode="json"),
        }
    )

    assert update["trading_decision"] == decision.model_dump(mode="json")
    assert update["final_trade_decision"].startswith("**Rating**: Buy")
    assert "SENTINEL" not in update["final_trade_decision"]


@pytest.mark.unit
def test_shadow_mode_cannot_bypass_decision_gate_or_publish():
    update = create_decision_gate_node(MagicMock())(
        {"evidence_gate_mode": "shadow", "validated_decision_context": None}
    )

    assert update["decision_gate"]["permitted"] is False
    assert "trading_decision" not in update
    assert "final_trade_decision" not in update


@pytest.mark.unit
def test_shadow_mode_blocks_a_policy_permitted_direction():
    policy, context, selection, _ = _permitted_policy_case()

    update = create_decision_gate_node(
        policy,
        evidence_gate_mode="shadow",
    )(
        {
            "validated_decision_context": context.model_dump(mode="json"),
            "direction_selection": selection.model_dump(mode="json"),
        }
    )

    assert update["decision_gate"]["permitted"] is False
    assert "shadow" in " ".join(update["decision_gate"]["diagnostics"]).casefold()
    assert "analysis_outcome" in update
    outcome = AnalysisOutcome.model_validate(update["analysis_outcome_contract"])
    assert update["analysis_outcome"] == render_analysis_outcome(outcome)
    assert "trading_decision" not in update
    assert "final_trade_decision" not in update


@pytest.mark.unit
def test_json_safe_nullable_initial_state_and_topology_signature_contract():
    state = Propagator().create_initial_state("NVDA", "2026-01-10")
    round_trip = json.loads(json.dumps(state))
    assert state["strategy_rule_applications"] == []
    assert state["decision_gate_v2"] == {}
    assert state["analysis_outcome_contract"] is None
    assert round_trip["analysis_outcome_contract"] is None
    for field in (
        "validated_decision_context", "direction_selection", "direction_selector_diagnostics",
        "trading_decision", "final_trade_decision",
    ):
        assert state[field] is None
        assert round_trip[field] is None

    llm = MagicMock()
    workflow = GraphSetup(
        llm, llm, dict.fromkeys(("market", "social", "news", "fundamentals"), lambda state: state),
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        evidence_gate_mode="shadow", decision_horizon=HORIZON,
    ).setup_graph(["market"])
    assert ("Portfolio Manager", "Decision Gate") in workflow.edges
    assert ("Decision Gate", "__end__") in workflow.edges

    graph = object.__new__(TradingAgentsGraph)
    graph.selected_analysts = ("market",)
    graph.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 2}
    graph.decision_policy = MagicMock(registry_digest="registry:test")
    graph.decision_horizon = HORIZON
    signature = graph._run_signature("stock")
    assert "evidence_schema=4" in signature
    assert "decision_schema=1" in signature
    assert "registry=registry:test" in signature
    assert "horizon=20:trading_days" in signature

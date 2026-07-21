"""Fail-closed Portfolio Manager backed by the validated claim ledger."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioDecisionRevision,
    PortfolioDecisionSelection,
    render_pm_decision,
)
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    bind_required_structured,
    bind_structured,
    invoke_required_structured,
    invoke_structured_or_freetext,
)
from tradingagents.evidence import (
    AnalysisOutcome,
    AnalysisOutcomeReason,
    DecisionGateResult,
    DraftThesis,
    EvidenceReadiness,
    EvidenceState,
    analysis_diagnostic_codes,
    analysis_outcome_publication,
    decision_ready_material_claims,
    evaluate_decision_gate,
)


def _render_allowed_claim_ledger(evidence: EvidenceState) -> str:
    """Render only individually validated claims as the PM's closed vocabulary."""
    claims = decision_ready_material_claims(evidence)
    if not claims:
        return "- No decision-ready material claims are available."
    return "\n".join(
        (
            "- {claim_id} | analyst={analyst} | sources={sources} | "
            "facts={facts} | statement={statement} | source_quote={quote}"
        ).format(
            claim_id=claim.claim_id,
            analyst=claim.analyst,
            sources=", ".join(sorted(claim.source_refs)),
            facts=", ".join(sorted(claim.fact_ids)) or "legacy-validated-source",
            statement=claim.statement,
            quote=claim.source_quote or claim.statement,
        )
        for claim in sorted(claims, key=lambda claim: claim.claim_id)
    )


def _render_selected_claim_ids(claim_ids: tuple[str, ...]) -> str:
    if not claim_ids:
        return "- (none)"
    return "\n".join(f"- {claim_id}" for claim_id in claim_ids)


def _evidence_bound_decision(
    selection: PortfolioDecisionSelection,
    claim_ids: tuple[str, ...],
    evidence: EvidenceState,
) -> PortfolioDecision:
    """Discard generated factual prose and render only ledger-held premises."""
    claims_by_id = {claim.claim_id: claim for claim in evidence.material_claims}
    assertion_claim_ids = tuple(
        assertion.claim_id for assertion in selection.decision_assertions
    )
    if (
        len(set(assertion_claim_ids)) != len(assertion_claim_ids)
        or set(assertion_claim_ids) != set(selection.material_claim_ids)
    ):
        raise ValueError(
            "decision assertions must bind every selected claim exactly once"
        )
    assertions_by_claim = {
        assertion.claim_id: assertion for assertion in selection.decision_assertions
    }
    facts_by_id = {fact.fact_id: fact for fact in evidence.source_facts}
    for claim_id in claim_ids:
        assertion = assertions_by_claim.get(claim_id)
        if assertion is None:
            raise ValueError(f"selected claim {claim_id!r} has no decision assertion")
        claim = claims_by_id.get(claim_id)
        if claim is None:
            continue
        asserted_fact_ids = set(assertion.fact_ids)
        claim_fact_ids = set(claim.fact_ids)
        if claim_fact_ids and (
            not asserted_fact_ids
            or not asserted_fact_ids.issubset(claim_fact_ids)
            or any(fact_id not in facts_by_id for fact_id in asserted_fact_ids)
        ):
            raise ValueError(
                f"decision assertion for {claim_id!r} is not bound to its Source Facts"
            )
        if not claim_fact_ids and asserted_fact_ids:
            raise ValueError(
                f"legacy claim {claim_id!r} cannot assert unregistered Source Facts"
            )
    selected_claims = tuple(
        claims_by_id[claim_id]
        for claim_id in claim_ids
        if claim_id in claims_by_id
    )
    thesis = (
        "\n".join(
            f"- [{claim.claim_id}] {claim.source_quote}"
            for claim in selected_claims
        )
        if selected_claims
        else "No validated material premise was retained."
    )
    return PortfolioDecision(
        rating=selection.rating,
        executive_summary=(
            f"The {selection.rating.value} rating is based exclusively on the validated "
            "material premises listed below."
        ),
        investment_thesis=thesis,
        material_claim_ids=claim_ids,
        # Targets and holding periods are omitted unless the product gains a
        # dedicated, deterministic portfolio-context evidence model.
        price_target=None,
        time_horizon=None,
    )


def _draft_for_decision(decision: PortfolioDecision) -> tuple[DraftThesis, str]:
    rendered = render_pm_decision(decision)
    return (
        DraftThesis(
            rating=decision.rating.value,
            narrative=rendered,
            material_claim_ids=decision.material_claim_ids,
        ),
        rendered,
    )


def _classified_gate_failure(
    gate: DecisionGateResult,
    diagnostic: str,
) -> DecisionGateResult:
    return gate.model_copy(
        update={
            "permitted": False,
            "readiness": EvidenceReadiness.INSUFFICIENT,
            "diagnostics": (*gate.diagnostics, diagnostic),
        }
    )


def _model_dump(value: Any) -> dict[str, Any] | None:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _selection_ledger_failure(
    selection: PortfolioDecisionSelection,
    evidence: EvidenceState,
    *,
    original_rating: str | None = None,
) -> str | None:
    """Reject PM claim references that are outside the immutable ready ledger."""
    ready_claims = {
        claim.claim_id: claim for claim in decision_ready_material_claims(evidence)
    }
    allowed_claim_ids = set(ready_claims)
    referenced_claim_ids = (
        *selection.material_claim_ids,
        *(assertion.claim_id for assertion in selection.decision_assertions),
    )
    if any(claim_id not in allowed_claim_ids for claim_id in referenced_claim_ids):
        return "selection references a claim outside the decision-ready ledger"
    assertion_claim_ids = tuple(
        assertion.claim_id for assertion in selection.decision_assertions
    )
    if (
        len(set(assertion_claim_ids)) != len(assertion_claim_ids)
        or set(assertion_claim_ids) != set(selection.material_claim_ids)
    ):
        return "selection assertions do not bind every selected claim exactly once"
    registered_fact_ids = {fact.fact_id for fact in evidence.source_facts}
    for assertion in selection.decision_assertions:
        asserted_fact_ids = set(assertion.fact_ids)
        claim_fact_ids = set(ready_claims[assertion.claim_id].fact_ids)
        if (
            not asserted_fact_ids
            or not asserted_fact_ids.issubset(claim_fact_ids)
            or not asserted_fact_ids.issubset(registered_fact_ids)
        ):
            return "selection assertion is not bound to immutable Source Facts"
    if original_rating is not None and selection.rating.value != original_rating:
        return "selection retry changed the immutable rating"
    return None


def _revision_subset_failure(
    revision: PortfolioDecisionRevision,
    original_claim_ids: tuple[str, ...],
) -> str | None:
    """Ensure a revision is removal-only before it can render a draft."""
    original_claim_id_set = set(original_claim_ids)
    if any(
        claim_id not in original_claim_id_set
        for claim_id in revision.retained_material_claim_ids
    ):
        return "revision adds a claim outside the original selection"
    return None


def _base_prompt(state: Mapping[str, Any], evidence: EvidenceState) -> str:
    instrument_context = get_instrument_context_from_state(state)
    history = state["risk_debate_state"]["history"]
    past_context = state.get("past_context", "")
    lessons_line = (
        f"- Untrusted prior-decision context (not evidence):\n{past_context}\n"
        if past_context
        else ""
    )
    return f"""As the Portfolio Manager, select a final rating and the exact validated claims that determine it.

{instrument_context}

**Rating Scale** (use exactly one): Buy / Overweight / Hold / Underweight / Sell.

**Untrusted working context:**
- Research Manager plan: {state['investment_plan']}
- Trader proposal: {state['trader_investment_plan']}
{lessons_line}- Risk debate: {history}

The working context may help you choose among validated claims, but it is not
evidence and must never become a factual premise by itself.

**Decision-ready material claim ledger (closed vocabulary):**
{_render_allowed_claim_ledger(evidence)}

Return only a PortfolioDecisionSelection. Every material_claim_ids entry must
be copied exactly from this ledger, and every factual premise used for the
rating must appear in that list. Include one decision_assertions entry per
selected claim, copying that claim's exact fact IDs from the ledger. Do not
infer facts from debate prose.

{get_language_instruction()}"""


def _blocked_result(
    draft: DraftThesis,
    gate: DecisionGateResult,
    audit_fields: Mapping[str, Any],
) -> dict[str, Any]:
    outcome = AnalysisOutcome(
        readiness=gate.readiness,
        reason=AnalysisOutcomeReason.PORTFOLIO_GATE_BLOCKED,
        diagnostic_codes=analysis_diagnostic_codes(gate.diagnostics),
    )
    return {
        **audit_fields,
        "draft_thesis": draft.model_dump(mode="json"),
        "decision_gate": gate.model_dump(mode="json"),
        **analysis_outcome_publication(outcome),
    }


def _risk_state_with_decision(state: Mapping[str, Any], decision: str) -> dict[str, Any]:
    risk_state = state["risk_debate_state"]
    return {
        "judge_decision": decision,
        "history": risk_state["history"],
        "aggressive_history": risk_state["aggressive_history"],
        "conservative_history": risk_state["conservative_history"],
        "neutral_history": risk_state["neutral_history"],
        "latest_speaker": "Judge",
        "current_aggressive_response": risk_state["current_aggressive_response"],
        "current_conservative_response": risk_state["current_conservative_response"],
        "current_neutral_response": risk_state["current_neutral_response"],
        "count": risk_state["count"],
    }


def create_portfolio_manager(llm, evidence_gate_mode: str = "enforce"):
    if evidence_gate_mode not in {"enforce", "shadow"}:
        raise ValueError(
            "evidence_gate_mode must be one of: enforce, shadow; "
            f"got {evidence_gate_mode!r}"
        )
    if evidence_gate_mode == "enforce":
        selection_llm = bind_required_structured(
            llm,
            PortfolioDecisionSelection,
            "Portfolio Manager",
        )
        revision_llm = bind_required_structured(
            llm,
            PortfolioDecisionRevision,
            "Portfolio Manager Revision",
        )
        legacy_structured_llm = None
    else:
        selection_llm = None
        revision_llm = None
        legacy_structured_llm = bind_structured(
            llm,
            PortfolioDecision,
            "Portfolio Manager",
        )

    def portfolio_manager_node(state) -> dict[str, Any]:
        evidence = EvidenceState.model_validate(state.get("evidence_state", {}))
        prompt = _base_prompt(state, evidence)

        if evidence_gate_mode != "enforce":
            captured_decision: PortfolioDecision | None = None

            def capture_and_render(decision: PortfolioDecision) -> str:
                nonlocal captured_decision
                captured_decision = decision
                return render_pm_decision(decision)

            rendered = invoke_structured_or_freetext(
                legacy_structured_llm,
                llm,
                prompt,
                capture_and_render,
                "Portfolio Manager",
            )
            draft = DraftThesis(
                rating=(
                    captured_decision.rating.value
                    if captured_decision is not None
                    else "Unvalidated"
                ),
                narrative=rendered,
                material_claim_ids=(
                    captured_decision.material_claim_ids
                    if captured_decision is not None
                    else ()
                ),
            )
            gate = _classified_gate_failure(
                evaluate_decision_gate(draft, evidence),
                "Shadow evidence mode is diagnostic-only and cannot publish direction.",
            )
            outcome = AnalysisOutcome(
                readiness=gate.readiness,
                reason=AnalysisOutcomeReason.SHADOW_MODE_BLOCKED,
                diagnostic_codes=analysis_diagnostic_codes(gate.diagnostics),
            )
            return {
                "decision_gate": gate.model_dump(mode="json"),
                **analysis_outcome_publication(outcome),
                "evidence_gate_mode": "shadow",
                "shadow_diagnostics": {
                    "status": "non_directional",
                    "model_output_kind": (
                        "structured" if captured_decision is not None else "free_text"
                    ),
                },
            }

        initial_result = invoke_required_structured(
            selection_llm,
            prompt,
            "Portfolio Manager",
        )
        raw_initial = _model_dump(initial_result.value)
        selection_audit_fields: dict[str, Any] = {
            "pm_original_selection": raw_initial,
        }
        try:
            if initial_result.value is None:
                raise ValueError(initial_result.reason or "unknown structured failure")
            selection = PortfolioDecisionSelection.model_validate(
                initial_result.value,
                from_attributes=True,
            )
            original_selection_rating = selection.rating.value
            selection_failure = _selection_ledger_failure(selection, evidence)
            if selection_failure is not None:
                selection_retry_prompt = (
                    prompt
                    + "\n\nYour prior selection violated the closed claim vocabulary. "
                    "Return exactly one corrected PortfolioDecisionSelection using only "
                    "the immutable decision-ready claim ledger above. Keep the rating "
                    f"unchanged as {selection.rating.value}. Do not add facts, prose, "
                    "values, or claims outside that ledger."
                )
                selection_retry_result = invoke_required_structured(
                    selection_llm,
                    selection_retry_prompt,
                    "Portfolio Manager Selection Retry",
                )
                selection_audit_fields["pm_selection_retry"] = _model_dump(
                    selection_retry_result.value
                )
                if selection_retry_result.value is None:
                    raise ValueError(
                        selection_retry_result.reason or "selection retry unavailable"
                    )
                selection = PortfolioDecisionSelection.model_validate(
                    selection_retry_result.value,
                    from_attributes=True,
                )
                selection_failure = _selection_ledger_failure(
                    selection,
                    evidence,
                    original_rating=original_selection_rating,
                )
                if selection_failure is not None:
                    raise ValueError(selection_failure)
            decision = _evidence_bound_decision(
                selection,
                selection.material_claim_ids,
                evidence,
            )
            original_draft, original_rendered = _draft_for_decision(decision)
            original_gate = evaluate_decision_gate(original_draft, evidence)
        except (ValidationError, ValueError, TypeError) as exc:
            original_draft = DraftThesis(
                rating="Unvalidated",
                narrative="",
                material_claim_ids=(),
            )
            original_gate = _classified_gate_failure(
                evaluate_decision_gate(original_draft, evidence),
                "Portfolio Manager required structured output was unavailable or "
                f"invalid ({initial_result.reason or type(exc).__name__}).",
            )
            return _blocked_result(
                original_draft,
                original_gate,
                {
                    **selection_audit_fields,
                    "original_draft_thesis": original_draft.model_dump(mode="json"),
                    "original_decision_gate": original_gate.model_dump(mode="json"),
                    "evidence_gate_mode": "enforce",
                },
            )

        audit_fields: dict[str, Any] = {
            **selection_audit_fields,
            "original_draft_thesis": original_draft.model_dump(mode="json"),
            "original_decision_gate": original_gate.model_dump(mode="json"),
            "evidence_gate_mode": "enforce",
        }
        draft = original_draft
        gate = original_gate
        rendered = original_rendered

        if not original_gate.permitted:
            revision_prompt = (
                prompt
                + "\n\nThe deterministic gate rejected the original selection:\n- "
                + "\n- ".join(original_gate.diagnostics)
                + "\n\nReturn one PortfolioDecisionRevision containing only the subset "
                "of these original IDs that should be retained:\n"
                + _render_selected_claim_ids(original_draft.material_claim_ids)
                + "\nThe rating is immutable. You cannot add prose, fields, values, or IDs."
            )
            revision_result = invoke_required_structured(
                revision_llm,
                revision_prompt,
                "Portfolio Manager Revision",
            )
            audit_fields["pm_revision"] = _model_dump(revision_result.value)
            if revision_result.value is None:
                gate = _classified_gate_failure(
                    original_gate,
                    "Portfolio Manager removal-only revision was unavailable "
                    f"({revision_result.reason}).",
                )
            else:
                try:
                    revision = PortfolioDecisionRevision.model_validate(
                        revision_result.value,
                        from_attributes=True,
                    )
                    revision_failure = _revision_subset_failure(
                        revision,
                        original_draft.material_claim_ids,
                    )
                    if revision_failure is not None:
                        revision_retry_prompt = (
                            prompt
                            + "\n\nReturn exactly one corrected PortfolioDecisionRevision. "
                            "Its retained_material_claim_ids must be a removal-only subset "
                            "of the immutable original IDs below. Keep the rating unchanged "
                            f"as {selection.rating.value}; do not add facts, prose, fields, "
                            "values, ratings, or IDs.\n"
                            + _render_selected_claim_ids(
                                original_draft.material_claim_ids
                            )
                        )
                        revision_retry_result = invoke_required_structured(
                            revision_llm,
                            revision_retry_prompt,
                            "Portfolio Manager Revision Retry",
                        )
                        audit_fields["pm_revision_retry"] = _model_dump(
                            revision_retry_result.value
                        )
                        if revision_retry_result.value is None:
                            raise ValueError(
                                revision_retry_result.reason
                                or "revision retry unavailable"
                            )
                        revision = PortfolioDecisionRevision.model_validate(
                            revision_retry_result.value,
                            from_attributes=True,
                        )
                        revision_failure = _revision_subset_failure(
                            revision,
                            original_draft.material_claim_ids,
                        )
                        if revision_failure is not None:
                            raise ValueError(revision_failure)
                    decision = _evidence_bound_decision(
                        selection,
                        revision.retained_material_claim_ids,
                        evidence,
                    )
                    draft, rendered = _draft_for_decision(decision)
                    gate = evaluate_decision_gate(
                        draft,
                        evidence,
                        original_draft=original_draft,
                    )
                except (ValueError, TypeError) as exc:
                    gate = _classified_gate_failure(
                        original_gate,
                        "Portfolio Manager removal-only revision was invalid "
                        f"({type(exc).__name__}).",
                    )
            audit_fields["revised_draft_thesis"] = draft.model_dump(mode="json")
            audit_fields["revised_decision_gate"] = gate.model_dump(mode="json")

        if not gate.permitted:
            return _blocked_result(draft, gate, audit_fields)

        final_decision = render_pm_decision(
            decision,
            confidence=gate.confidence,
            evidence_coverage=gate.evidence_coverage,
        )
        return {
            **audit_fields,
            "risk_debate_state": _risk_state_with_decision(state, final_decision),
            "final_trade_decision": final_decision,
            "draft_thesis": draft.model_dump(mode="json"),
            "decision_gate": gate.model_dump(mode="json"),
        }

    return portfolio_manager_node

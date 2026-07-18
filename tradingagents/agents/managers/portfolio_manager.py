"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call.  The result is rendered
back to markdown for storage in ``final_trade_decision`` so memory log,
CLI display, and saved reports continue to consume the same shape they do
today.  When a provider does not expose structured output, the agent falls
back gracefully to free-text generation.
"""

from __future__ import annotations

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.evidence import (
    AnalysisOutcome,
    DraftThesis,
    EvidenceState,
    evaluate_decision_gate,
    render_analysis_outcome,
)


def create_portfolio_manager(llm, evidence_gate_mode: str = "enforce"):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.{get_language_instruction()}"""

        def invoke_draft(draft_prompt):
            captured_decision = None

            def capture_and_render(decision):
                nonlocal captured_decision
                captured_decision = decision
                return render_pm_decision(decision)

            rendered = invoke_structured_or_freetext(
                structured_llm,
                llm,
                draft_prompt,
                capture_and_render,
                "Portfolio Manager",
            )
            return captured_decision, rendered

        decision, rendered_decision = invoke_draft(prompt)
        evidence = EvidenceState.model_validate(state.get("evidence_state", {}))
        if decision is None:
            draft = DraftThesis(
                rating="Unvalidated",
                narrative=rendered_decision,
                material_claim_ids=(),
            )
        else:
            draft = DraftThesis(
                rating=decision.rating.value,
                narrative=rendered_decision,
                material_claim_ids=decision.material_claim_ids,
            )
        gate = evaluate_decision_gate(draft, evidence)

        if not gate.permitted and evidence_gate_mode == "enforce":
            revision_prompt = (
                prompt
                + "\n\n---\n\nThe evidence gate rejected the draft for these reasons:\n- "
                + "\n- ".join(gate.diagnostics)
                + "\nRevise the draft exactly once. Preserve the rating, remove unsupported "
                "premises and unsupported numeric precision, and do not add material claims."
            )
            revised_decision, revised_markdown = invoke_draft(revision_prompt)
            if revised_decision is None:
                revised_draft = DraftThesis(
                    rating=draft.rating,
                    narrative=revised_markdown,
                    material_claim_ids=(),
                )
            else:
                revised_draft = DraftThesis(
                    rating=revised_decision.rating.value,
                    narrative=revised_markdown,
                    material_claim_ids=revised_decision.material_claim_ids,
                )
            gate = evaluate_decision_gate(
                revised_draft,
                evidence,
                original_draft=draft,
            )
            decision = revised_decision
            rendered_decision = revised_markdown
            draft = revised_draft

        if not gate.permitted and evidence_gate_mode == "enforce":
            outcome = AnalysisOutcome(
                readiness=gate.readiness,
                summary=(
                    "Analysis completed without a Trading Decision because the "
                    "draft remained unsupported after one constrained revision."
                ),
                diagnostics=gate.diagnostics,
                evidence_coverage=gate.evidence_coverage,
            )
            return {
                "draft_thesis": draft.model_dump(mode="json"),
                "decision_gate": gate.model_dump(mode="json"),
                "analysis_outcome": render_analysis_outcome(outcome),
            }

        if decision is not None and gate.permitted:
            rendered_decision = render_pm_decision(
                decision,
                confidence=gate.confidence,
                evidence_coverage=gate.evidence_coverage,
            )
        if evidence_gate_mode == "shadow":
            rendered_decision = (
                "> **Evidence Gate:** UNENFORCED (shadow mode)\n"
                "> This Trading Decision was produced with the explicit legacy "
                "override and was not fail-closed by the evidence gates.\n\n"
                + rendered_decision
            )
        final_trade_decision = rendered_decision

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
            "draft_thesis": draft.model_dump(mode="json"),
            "decision_gate": gate.model_dump(mode="json"),
        }

    return portfolio_manager_node

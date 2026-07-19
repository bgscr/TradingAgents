"""Graph nodes and routing helpers for deterministic evidence gates."""

from __future__ import annotations

from tradingagents.evidence import (
    AnalysisOutcome,
    EvidenceState,
    evaluate_admission_gate,
    render_analysis_outcome,
)


def route_after_admission(state: dict) -> str:
    """Route admitted evidence to debate and all other states to a terminal outcome."""
    gate = state.get("admission_gate", {})
    return "admitted" if gate.get("admitted") is True else "blocked"


def create_admission_gate_node(
    minimum_history_rows: int = 1,
    *,
    emit_blocked_outcome: bool = True,
):
    """Create a node that evaluates shared evidence before thesis synthesis."""

    def admission_gate_node(state: dict) -> dict:
        evidence = EvidenceState.model_validate(state.get("evidence_state", {}))
        gate = evaluate_admission_gate(evidence, minimum_history_rows)
        update = {"admission_gate": gate.model_dump(mode="json")}
        if not gate.admitted and emit_blocked_outcome:
            outcome = AnalysisOutcome(
                readiness=gate.readiness,
                summary=(
                    "Analysis stopped before thesis synthesis because required "
                    "evidence was not decision-ready."
                ),
                diagnostics=gate.diagnostics,
                evidence_coverage=gate.coverage,
            )
            update["analysis_outcome"] = render_analysis_outcome(outcome)
        return update

    return admission_gate_node

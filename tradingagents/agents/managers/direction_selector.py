"""Closed-context direction selection and deterministic final decision gating."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tradingagents.agents.utils.structured import (
    bind_required_structured,
    invoke_required_structured,
)
from tradingagents.decision_policy import (
    DecisionGateResultV2,
    DecisionPolicyEngine,
    DirectionSelection,
    EvidenceIntegrityStatus,
    TradingDecisionContract,
    ValidatedDecisionContext,
)
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    analysis_diagnostic_codes,
    analysis_outcome_publication,
)


class DirectionSelectorDiagnostics(BaseModel):
    """JSON-safe, payload-free diagnostics for a failed selector invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["blocked"] = "blocked"
    reason: str = Field(min_length=1)
    attempts: int = Field(ge=0)


def _context_from_state(value: Any) -> ValidatedDecisionContext:
    """Validate checkpoint-safe context with Pydantic's JSON union semantics."""

    if isinstance(value, ValidatedDecisionContext):
        return value
    return ValidatedDecisionContext.model_validate_json(json.dumps(value))


def render_direction_selector_prompt(context: ValidatedDecisionContext) -> str:
    """Render the selector prompt from the closed context and nothing else."""

    return (
        "Select one rating using only the validated decision context below. "
        "Return exactly one DirectionSelection. Copy context_id exactly, and include "
        "every assertion_id from the context. Do not add reasoning or prose.\n\n"
        "Validated Decision Context:\n"
        + context.model_dump_json(indent=2)
    )


def create_direction_selector(llm: Any):
    """Create the sole model adapter at the final direction-selection seam."""

    structured_llm = bind_required_structured(
        llm,
        DirectionSelection,
        "Portfolio Manager Direction Selector",
    )

    def direction_selector_node(state: Mapping[str, Any]) -> dict[str, Any]:
        try:
            context = _context_from_state(state.get("validated_decision_context"))
        except (ValidationError, TypeError, ValueError):
            diagnostics = DirectionSelectorDiagnostics(
                reason="validated_decision_context_invalid",
                attempts=0,
            )
            return {
                "direction_selector_diagnostics": diagnostics.model_dump(mode="json")
            }

        result = invoke_required_structured(
            structured_llm,
            render_direction_selector_prompt(context),
            "Portfolio Manager Direction Selector",
            validator=lambda value: DirectionSelection.model_validate(
                value,
                from_attributes=True,
            ),
        )
        if result.value is None:
            diagnostics = DirectionSelectorDiagnostics(
                reason=result.reason or "direction_selection_unavailable",
                attempts=result.attempts,
            )
            return {
                "direction_selector_diagnostics": diagnostics.model_dump(mode="json")
            }

        selection = DirectionSelection.model_validate(
            result.value,
            from_attributes=True,
        )
        return {"direction_selection": selection.model_dump(mode="json")}

    return direction_selector_node


def render_trading_decision(decision: TradingDecisionContract) -> str:
    """Render a backward-compatible decision using only the gated contract."""

    assertion_lines = [
        (
            f"- [{assertion.assertion_id}] rule={assertion.rule_id}@"
            f"{assertion.rule_version}; facts={', '.join(assertion.fact_ids)}"
        )
        for assertion in decision.assertions
    ]
    return "\n".join(
        [
            f"**Rating**: {decision.rating.value}",
            "",
            "**Executive Summary**: "
            f"The {decision.rating.value} rating is authorized by "
            f"{len(decision.assertions)} validated rule-backed assertion(s).",
            "",
            "**Investment Thesis**:",
            *assertion_lines,
            "",
            f"**Decision ID**: {decision.decision_id}",
            f"**Evidence Integrity**: {decision.integrity_status.value}",
        ]
    )


def _readiness(status: EvidenceIntegrityStatus) -> EvidenceReadiness:
    return EvidenceReadiness(status.value)


def _blocked_gate_result(diagnostic: str) -> DecisionGateResultV2:
    return DecisionGateResultV2(
        permitted=False,
        integrity_status=EvidenceIntegrityStatus.INSUFFICIENT,
        diagnostics=(diagnostic,),
    )


def _blocked_outcome(gate: DecisionGateResultV2) -> dict[str, Any]:
    diagnostic_codes = analysis_diagnostic_codes(gate.diagnostics)
    reason = (
        AnalysisOutcomeReason.SHADOW_MODE_BLOCKED
        if AnalysisDiagnosticCode.SHADOW_MODE in diagnostic_codes
        else AnalysisOutcomeReason.DECISION_GATE_BLOCKED
    )
    return analysis_outcome_publication(
        AnalysisOutcome(
            readiness=_readiness(gate.integrity_status),
            reason=reason,
            diagnostic_codes=diagnostic_codes,
        )
    )


def _risk_state_with_decision(
    state: Mapping[str, Any], decision: str
) -> dict[str, Any] | None:
    risk_state = state.get("risk_debate_state")
    if not isinstance(risk_state, Mapping):
        return None
    return {
        **dict(risk_state),
        "judge_decision": decision,
        "latest_speaker": "Judge",
    }


def create_decision_gate_node(
    decision_policy: DecisionPolicyEngine,
    evidence_gate_mode: str = "enforce",
):
    """Create the only node authorized to publish a Trading Decision."""

    if evidence_gate_mode not in {"enforce", "shadow"}:
        raise ValueError(
            "evidence_gate_mode must be one of: enforce, shadow; "
            f"got {evidence_gate_mode!r}"
        )

    def decision_gate_node(state: Mapping[str, Any]) -> dict[str, Any]:
        if (
            evidence_gate_mode == "shadow"
            or state.get("evidence_gate_mode") == "shadow"
        ):
            gate = _blocked_gate_result(
                "shadow evidence mode is diagnostic-only and cannot publish direction"
            )
        else:
            try:
                context = _context_from_state(state.get("validated_decision_context"))
            except (ValidationError, TypeError, ValueError):
                gate = _blocked_gate_result("validated decision context is unavailable")
            else:
                try:
                    selection = DirectionSelection.model_validate(
                        state.get("direction_selection")
                    )
                except (ValidationError, TypeError, ValueError):
                    gate = _blocked_gate_result("direction selection is unavailable")
                else:
                    try:
                        gate = decision_policy.gate(context, selection)
                    except Exception:  # noqa: BLE001 - trust seam fails closed
                        gate = _blocked_gate_result("decision policy gate failed")

        gate_payload = gate.model_dump(mode="json")
        update: dict[str, Any] = {
            "decision_gate": gate_payload,
            "decision_gate_v2": gate_payload,
        }
        if not gate.permitted or gate.decision is None:
            update.update(_blocked_outcome(gate))
            return update

        rendered = render_trading_decision(gate.decision)
        update.update(
            {
                "trading_decision": gate.decision.model_dump(mode="json"),
                "final_trade_decision": rendered,
            }
        )
        risk_state = _risk_state_with_decision(state, rendered)
        if risk_state is not None:
            update["risk_debate_state"] = risk_state
        return update

    return decision_gate_node

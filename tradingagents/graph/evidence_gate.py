"""Graph nodes and routing helpers for deterministic evidence gates."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from pydantic import ValidationError

from tradingagents.decision_policy import (
    DecisionContextBuilt,
    DecisionHorizon,
    DecisionPolicyEngine,
    EvidenceIntegrityStatus,
    RuleApplicationRequest,
)
from tradingagents.evidence import (
    AdmissionGateResult,
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    EvidenceState,
    analysis_outcome_publication,
    evaluate_admission_gate,
    evaluate_preflight_gate,
)


def route_after_preflight(state: dict) -> str:
    """Route only authoritative baseline evidence into model-mediated analysis."""
    result = state.get("evidence_preflight", {})
    return "admitted" if result.get("passed") is True else "blocked"


def _readiness(status: EvidenceIntegrityStatus) -> EvidenceReadiness:
    return EvidenceReadiness(status.value)


def _analysis_outcome(
    *,
    readiness: EvidenceReadiness,
    reason: AnalysisOutcomeReason,
    diagnostic_codes: tuple[AnalysisDiagnosticCode, ...],
) -> dict[str, Any]:
    return analysis_outcome_publication(
        AnalysisOutcome(
            readiness=readiness,
            reason=reason,
            diagnostic_codes=diagnostic_codes,
        )
    )


def _evidence_from_state(value: Any) -> EvidenceState:
    """Validate checkpoint-safe evidence with Pydantic's JSON union semantics."""

    if isinstance(value, EvidenceState):
        return value
    return EvidenceState.model_validate_json(json.dumps(value or {}))


def create_preflight_gate_node(
    decision_policy: DecisionPolicyEngine | None = None,
    decision_horizon: DecisionHorizon | None = None,
    minimum_history_rows: int = 1,
):
    """Create the deterministic gate that runs before every analyst/model node."""

    policy = decision_policy if decision_policy is not None else DecisionPolicyEngine()

    def preflight_gate_node(state: dict) -> dict:
        evidence = _evidence_from_state(state.get("evidence_state", {}))
        effective_minimum_history_rows = minimum_history_rows
        applicable_history_rows = None
        if (
            decision_horizon is not None
            and evidence.instrument_identity is not None
            and evidence.instrument_identity.is_authoritative
        ):
            applicable_history_rows = policy.preflight_minimum_history_rows(
                evidence.instrument_identity.instrument_kind,
                decision_horizon,
            )
            if applicable_history_rows is not None:
                effective_minimum_history_rows = max(
                    effective_minimum_history_rows,
                    applicable_history_rows,
                )
        result = evaluate_preflight_gate(
            evidence,
            minimum_history_rows=effective_minimum_history_rows,
        )
        configuration_blockers = list(policy.configuration_blockers)
        if decision_horizon is None:
            configuration_blockers.append("decision_horizon_not_configured")
        elif (
            evidence.instrument_identity is not None
            and evidence.instrument_identity.is_authoritative
            and applicable_history_rows is None
        ):
            configuration_blockers.append(
                "no_applicable_registered_strategy_rule"
            )
        blockers = tuple(sorted({*result.blockers, *configuration_blockers}))
        if blockers != result.blockers:
            result = result.model_copy(
                update={
                    "passed": False,
                    "readiness": EvidenceReadiness.INSUFFICIENT,
                    "blockers": blockers,
                    "diagnostic_codes": tuple(
                        sorted(
                            {
                            *result.diagnostic_codes,
                            AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID,
                            },
                            key=lambda code: code.value,
                        )
                    ),
                }
            )
        update = {"evidence_preflight": result.model_dump(mode="json")}
        if not result.passed:
            update.update(
                _analysis_outcome(
                    readiness=result.readiness,
                    reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
                    diagnostic_codes=result.diagnostic_codes,
                )
            )
        return update

    return preflight_gate_node


def route_after_admission(state: dict) -> str:
    """Route admitted evidence to debate and all other states to a terminal outcome."""
    gate = state.get("admission_gate", {})
    return "admitted" if gate.get("admitted") is True else "blocked"


def create_admission_gate_node(
    decision_policy: DecisionPolicyEngine | None = None,
    decision_horizon: DecisionHorizon | None = None,
    minimum_history_rows: int = 1,
):
    """Create a node that evaluates shared evidence before thesis synthesis."""

    policy = decision_policy if decision_policy is not None else DecisionPolicyEngine()

    def blocked_gate(
        gate: AdmissionGateResult,
        diagnostics: tuple[str, ...],
        diagnostic_codes: tuple[AnalysisDiagnosticCode, ...],
        readiness: EvidenceReadiness = EvidenceReadiness.INSUFFICIENT,
    ) -> AdmissionGateResult:
        return gate.model_copy(
            update={
                "admitted": False,
                "readiness": readiness,
                "diagnostics": tuple(sorted({*gate.diagnostics, *diagnostics})),
                "diagnostic_codes": tuple(
                    sorted(
                        {*gate.diagnostic_codes, *diagnostic_codes},
                        key=lambda code: code.value,
                    )
                ),
            }
        )

    def admission_gate_node(state: dict) -> dict:
        evidence = _evidence_from_state(state.get("evidence_state", {}))
        gate = evaluate_admission_gate(evidence, minimum_history_rows)
        context = None
        binding = None
        if gate.admitted:
            if decision_horizon is None:
                gate = blocked_gate(
                    gate,
                    ("decision_horizon_not_configured",),
                    (AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID,),
                )
            else:
                raw_applications: Any = state.get("strategy_rule_applications", ())
                discovered_applications = not raw_applications
                try:
                    if discovered_applications:
                        applications = policy.candidate_applications(
                            evidence,
                            horizon=decision_horizon,
                        )
                    else:
                        applications = tuple(
                            RuleApplicationRequest.model_validate(application)
                            for application in raw_applications
                        )
                except (ValidationError, TypeError, ValueError):
                    gate = blocked_gate(
                        gate,
                        (
                            "strategy_rule_application_discovery_failed"
                            if discovered_applications
                            else "strategy_rule_application_invalid",
                        ),
                        (AnalysisDiagnosticCode.STRATEGY_RULE_INVALID,),
                    )
                else:
                    try:
                        as_of_date = date.fromisoformat(str(state.get("trade_date", "")))
                    except ValueError:
                        gate = blocked_gate(
                            gate,
                            ("decision_as_of_date_invalid",),
                            (AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID,),
                        )
                    else:
                        built = policy.build_context(
                            evidence,
                            applications,
                            horizon=decision_horizon,
                            as_of_date=as_of_date,
                            tolerate_unsatisfied_applications=(
                                discovered_applications
                            ),
                        )
                        if isinstance(built, DecisionContextBuilt):
                            run_id = state.get("run_id")
                            if not isinstance(run_id, str) or not run_id.strip():
                                gate = blocked_gate(
                                    gate,
                                    ("run_identity_unavailable",),
                                    (
                                        AnalysisDiagnosticCode.DETERMINISTIC_GATE_REJECTED,
                                    ),
                                )
                            else:
                                try:
                                    binding = policy.admit_evidence(
                                        evidence,
                                        built.context,
                                        run_id=run_id,
                                    )
                                except (TypeError, ValueError):
                                    gate = blocked_gate(
                                        gate,
                                        ("trusted_evidence_admission_failed",),
                                        (
                                            AnalysisDiagnosticCode.DETERMINISTIC_GATE_REJECTED,
                                        ),
                                    )
                                else:
                                    context = built.context
                        else:
                            gate = blocked_gate(
                                gate,
                                built.blocker_codes,
                                built.diagnostic_codes,
                                _readiness(built.integrity_status),
                            )

        update = {"admission_gate": gate.model_dump(mode="json")}
        if context is not None:
            update["validated_decision_context"] = context.model_dump(mode="json")
        if binding is not None:
            update["admitted_evidence_binding"] = binding.model_dump(mode="json")
        if not gate.admitted:
            update.update(
                _analysis_outcome(
                    readiness=gate.readiness,
                    reason=AnalysisOutcomeReason.ADMISSION_BLOCKED,
                    diagnostic_codes=gate.diagnostic_codes,
                )
            )
        return update

    return admission_gate_node

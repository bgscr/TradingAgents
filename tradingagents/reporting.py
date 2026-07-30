"""Deterministic report publication for validated terminal outcomes."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingagents.dataflows.financial_dispatch import FinancialDispatchAuditProjection
from tradingagents.decision_audit import write_immutable_decision_audit
from tradingagents.decision_policy import TradingDecisionContract
from tradingagents.evidence import (
    AnalysisOutcome,
    EvidenceState,
    SourceAcquisitionUnavailable,
    render_analysis_outcome,
)
from tradingagents.terminal_contract import (
    CanonicalRunIdentity,
    DecisionCoverage,
    TerminalContract,
    TerminalOutcomeKind,
)


def _write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _remove_unvalidated_outputs(root: Path) -> None:
    """Remove stale/model-authored files that are not terminal contracts."""

    for relative in ("1_analysts", "2_research", "3_trading", "4_risk"):
        path = root / relative
        if path.is_dir():
            shutil.rmtree(path)
    for relative in (
        "market_report.md",
        "sentiment_report.md",
        "news_report.md",
        "fundamentals_report.md",
        "investment_plan.md",
        "trader_investment_plan.md",
        "final_trade_decision.md",
        "5_portfolio/decision.md",
        "5_portfolio/analysis_outcome.md",
    ):
        (root / relative).unlink(missing_ok=True)


def _format_scalar(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _format_coverage(coverage: DecisionCoverage) -> list[str]:
    rows = (
        ("Source Availability Coverage", coverage.source_availability),
        ("Validated Fact Coverage", coverage.validated_facts),
        ("Decision Assertion Coverage", coverage.decision_assertions),
    )
    return [
        (
            f"- **{label}:** {measure.ratio:.1%} "
            f"({measure.covered_count}/{measure.total_count})"
        )
        for label, measure in rows
    ]


def _render_fact(index: int, fact) -> list[str]:
    value = _format_scalar(fact.normalized_value)
    rendered_value = f"{value} {fact.unit}".strip()
    lines = [
        f"### Fact {index}: `{fact.canonical_field}`",
        "",
        f"- **Canonical value:** {rendered_value}",
        f"- **Instrument:** `{fact.instrument_symbol}`",
        f"- **Effective date:** {fact.effective_date}",
        f"- **Fact ID:** `{fact.fact_id}`",
        f"- **Artifact:** `artifact=sha256:{fact.artifact_sha256}`",
        (
            "- **Source span:** "
            f"{fact.source_span_start}:{fact.source_span_end}"
        ),
    ]
    lineage = fact.calculation_lineage
    if lineage is None:
        lines.append("- **Calculation lineage:** direct source observation")
    else:
        lines.extend(
            [
                (
                    "- **Calculation:** "
                    f"`{lineage.calculation_id}@{lineage.calculation_version}`"
                ),
                f"- **Input snapshot:** `{lineage.input_snapshot_id}`",
                (
                    "- **Effective range:** "
                    f"{lineage.effective_range_start} to {lineage.effective_range_end}"
                ),
                f"- **Observations used:** {lineage.observations_used}",
                f"- **Adjustment basis:** {lineage.adjustment_basis}",
                f"- **Implementation:** `{lineage.implementation_version}`",
                f"- **Result digest:** `{lineage.result_digest}`",
            ]
        )
    return lines


def _render_assertion(index: int, assertion) -> list[str]:
    predicate = f"`{assertion.predicate_id}` ({assertion.comparator}"
    if assertion.threshold is not None:
        predicate += f" {_format_scalar(assertion.threshold)}"
    predicate += ")"
    return [
        f"### Assertion {index}: `{assertion.assertion_id}`",
        "",
        f"- **Strategy Rule:** `{assertion.rule_id}@{assertion.rule_version}`",
        f"- **Target rating:** {assertion.target_rating.value}",
        f"- **Polarity:** {assertion.polarity.value}",
        f"- **Predicate:** {predicate}",
        (
            "- **Horizon:** "
            f"{assertion.horizon.count} {assertion.horizon.unit.value}"
        ),
        "- **Source Fact IDs:** " + ", ".join(f"`{item}`" for item in assertion.fact_ids),
        f"- **Evaluation digest:** `{assertion.evaluation_digest}`",
    ]


def _render_acquisition_outcomes(evidence: EvidenceState) -> list[str]:
    lines = ["## Acquisition Outcomes", ""]
    outcomes = sorted(
        evidence.acquisition_outcomes,
        key=lambda outcome: (
            outcome.provider,
            outcome.capability,
            outcome.attempt,
            outcome.retrieved_at,
            outcome.outcome,
            outcome.source_ref,
        ),
    )
    if not outcomes:
        lines.extend(["No acquisition outcomes were recorded.", ""])
    else:
        for index, outcome in enumerate(outcomes, 1):
            if isinstance(outcome, SourceAcquisitionUnavailable):
                reason = outcome.reason.value
                retry_after = (
                    f"{outcome.retry_after_seconds:g} seconds"
                    if outcome.retry_after_seconds is not None
                    else "not provided"
                )
            else:
                reason = "not applicable"
                retry_after = "not provided"
            lines.extend(
                [
                    f"### Acquisition {index}",
                    "",
                    f"- **Provider:** `{outcome.provider}`",
                    f"- **Capability:** `{outcome.capability}`",
                    f"- **Attempt:** {outcome.attempt}",
                    f"- **Retrieved at:** {outcome.retrieved_at}",
                    f"- **Outcome:** {outcome.outcome}",
                    f"- **Unavailable reason:** {reason}",
                    f"- **Retryable:** {_format_scalar(outcome.retryable)}",
                    f"- **Retry-After:** {retry_after}",
                    "",
                ]
            )
    attempts = evidence.physical_attempt_events
    if attempts:
        lines.extend(
            [
                "## Physical Provider Attempts",
                "",
                f"- **Total physical-attempt count:** {evidence.physical_attempt_count}",
                "",
            ]
        )
        for event in attempts:
            cooldown = event.cooldown_until or "unchanged"
            retry_after = (
                f"{event.retry_after_seconds:g} seconds"
                if event.retry_after_seconds is not None
                else "not provided"
            )
            status_code = (
                str(event.status_code)
                if event.status_code is not None
                else "not provided"
            )
            error_code = event.error_code or "not provided"
            lines.extend(
                [
                    f"### Physical attempt {event.attempt_index}",
                    "",
                    *(
                        [f"- **Attempt event ID:** `{event.attempt_event_id}`"]
                        if event.attempt_event_id is not None
                        else []
                    ),
                    f"- **Request identity:** `{event.request_key}`",
                    f"- **Sequence identity:** `{event.sequence_id}`",
                    (
                        "- **Upstream Service Identity:** "
                        f"`{event.upstream_service_id}` ({event.upstream_service_name})"
                    ),
                    f"- **Capacity scope:** `{event.capacity_scope}`",
                    f"- **Attempted at:** {event.attempted_at}",
                    f"- **Pacing/permit event:** {event.pacing_event}",
                    f"- **Pacing wait:** {event.pacing_wait_seconds:g} seconds",
                    f"- **Typed outcome:** {event.outcome}",
                    f"- **Retryable:** {_format_scalar(event.retryable)}",
                    f"- **Status code:** {status_code}",
                    f"- **Error code:** `{error_code}`",
                    f"- **Retry-After:** {retry_after}",
                    f"- **Cooldown changed:** {_format_scalar(event.cooldown_changed)}",
                    f"- **Cooldown until:** {cooldown}",
                    (
                        "- **Final physical-attempt count:** "
                        f"{event.final_physical_attempt_count}"
                    ),
                    "",
                ]
            )
    return lines


def _render_market_snapshot_binding(evidence: EvidenceState) -> list[str]:
    snapshot = evidence.market_snapshot
    if snapshot is None:
        return []
    lines = [
        "## Authoritative Market Snapshot",
        "",
        f"- **Snapshot ID:** `{snapshot.snapshot_id}`",
        f"- **Snapshot identity version:** {snapshot.snapshot_id_version}",
    ]
    if snapshot.pin_membership_digest is not None:
        lines.append(
            "- **Canonical manifest digest:** "
            f"`{snapshot.pin_membership_digest}`"
        )
    if snapshot.snapshot_manifest_json is not None:
        lines.extend(
            [
                "- **Canonical manifest:**",
                "",
                "```json",
                snapshot.snapshot_manifest_json,
                "```",
            ]
        )
    lines.append("")
    return lines


def _render_financial_dispatch_operations(
    financial_dispatch: FinancialDispatchAuditProjection | None,
) -> list[str]:
    if financial_dispatch is None:
        return []
    lines = [
        "## Financial dispatch operations",
        "",
        f"- **Canonical request count:** {financial_dispatch.request_count}",
        (
            "- **Acquisition attempt count:** "
            f"{financial_dispatch.acquisition_attempt_count}"
        ),
        (
            "- **Duplicate-suppressed call count:** "
            f"{financial_dispatch.duplicate_suppressed_count}"
        ),
        "",
    ]
    for request in financial_dispatch.requests:
        artifact = (
            f"`artifact=sha256:{request.artifact_sha256}`"
            if request.artifact_sha256 is not None
            else "none"
        )
        lines.extend(
            [
                f"### `{request.tool_name}` request",
                "",
                f"- **Request reference:** `{request.request_ref}`",
                f"- **Statement type:** {request.statement_type.value}",
                f"- **Frequency:** {request.frequency.value}",
                f"- **As of:** {request.as_of_date.isoformat()}",
                (
                    "- **Acquisition attempts:** "
                    f"{request.acquisition_attempt_count}"
                ),
                (
                    "- **Duplicate-suppressed calls:** "
                    f"{request.duplicate_suppressed_count}"
                ),
                f"- **Artifact:** {artifact}",
            ]
        )
        for index, outcome in enumerate(request.outcomes, 1):
            reason = outcome.reason.value if outcome.reason is not None else "available"
            error_code = outcome.error_code or "none"
            lines.extend(
                [
                    f"- **Outcome {index}:** {outcome.outcome}",
                    f"  - Provider: `{outcome.provider}`",
                    f"  - Attempt: {outcome.attempt}",
                    f"  - Reason: `{reason}`",
                    f"  - Error code: `{error_code}`",
                ]
            )
        lines.append("")
    return lines


def render_decision_report(
    decision: TradingDecisionContract,
    terminal: TerminalContract,
    evidence: EvidenceState | None = None,
    financial_dispatch: FinancialDispatchAuditProjection | None = None,
) -> str:
    """Render only canonical facts and rule semantics from the gated decision."""

    lines = [
        "# Validated Trading Decision",
        "",
        f"- **Rating:** {decision.rating.value}",
        f"- **Decision ID:** `{decision.decision_id}`",
        f"- **Context ID:** `{decision.context_id}`",
        (
            "- **Instrument:** "
            f"`{decision.instrument.symbol}` on {decision.instrument.venue} "
            f"({decision.instrument.instrument_kind.value}, "
            f"{decision.instrument.currency})"
        ),
        f"- **As of:** {decision.as_of_date.isoformat()}",
        (
            "- **Decision horizon:** "
            f"{decision.horizon.count} {decision.horizon.unit.value}"
        ),
        f"- **Evidence Integrity Status:** {decision.integrity_status.value}",
        f"- **Strategy Rule Registry:** `{decision.registry_digest}`",
        f"- **Calculation Registry:** `{decision.calculation_registry_digest}`",
        "",
        "## Coverage",
        "",
        *_format_coverage(terminal.coverage),
        "",
        "No predictive-confidence score is reported because no out-of-sample "
        "calibration is available.",
        "",
    ]
    if evidence is not None:
        lines.extend(_render_market_snapshot_binding(evidence))
        lines.extend(_render_acquisition_outcomes(evidence))
    lines.extend(_render_financial_dispatch_operations(financial_dispatch))
    lines.extend(["## Canonical Source Facts", ""])
    for index, fact in enumerate(sorted(decision.facts, key=lambda item: item.fact_id), 1):
        lines.extend(_render_fact(index, fact))
        lines.append("")
    lines.extend(["## Decision Assertions", ""])
    for index, assertion in enumerate(
        sorted(decision.assertions, key=lambda item: item.assertion_id),
        1,
    ):
        lines.extend(_render_assertion(index, assertion))
        lines.append("")
    if decision.integrity_status.value == "degraded":
        lines.extend(
            [
                "## Material Degradation",
                "",
                "Optional evidence was unavailable. The validated facts and every "
                "directional assertion above nevertheless passed the Decision Gate.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_analysis_outcome_report(
    outcome: AnalysisOutcome,
    terminal: TerminalContract,
    evidence: EvidenceState | None = None,
    financial_dispatch: FinancialDispatchAuditProjection | None = None,
) -> str:
    outcome = AnalysisOutcome.model_validate(outcome)
    lines = [
        "# Non-Directional Analysis Outcome",
        "",
        f"- **Evidence Integrity Status:** {terminal.evidence_integrity_status.value}",
        "- **Terminal Outcome Kind:** analysis_outcome",
        "",
        "## Coverage",
        "",
        *_format_coverage(terminal.coverage),
        "",
    ]
    if evidence is not None:
        lines.extend(_render_market_snapshot_binding(evidence))
        lines.extend(_render_acquisition_outcomes(evidence))
    lines.extend(_render_financial_dispatch_operations(financial_dispatch))
    lines.extend([render_analysis_outcome(outcome), ""])
    return "\n".join(lines)


def _report_header(
    ticker: str,
    terminal: TerminalContract,
    identity: CanonicalRunIdentity,
    created_at: str,
) -> str:
    return "\n".join(
        [
            f"# Trading Analysis Report: {ticker}",
            "",
            f"- **Run ID:** `{identity.run_id}`",
            f"- **Audit SHA-256:** `{identity.audit_digest}`",
            f"- **Audit created:** {created_at}",
            f"- **Lifecycle Status:** {terminal.lifecycle_status.value}",
            f"- **Terminal Outcome Kind:** {terminal.terminal_outcome_kind.value}",
            (
                "- **Evidence Integrity Status:** "
                f"{terminal.evidence_integrity_status.value}"
            ),
            f"- **Configuration Digest:** `{identity.configuration_digest}`",
            "",
        ]
    )


def write_report_tree(final_state: dict, ticker: str, save_path) -> Path:
    """Publish one validated terminal report and its immutable decision audit."""

    save_path = Path(save_path)
    save_path.mkdir(parents=True, exist_ok=True)
    write_immutable_decision_audit(final_state, save_path)
    terminal = TerminalContract.model_validate(final_state["terminal_contract"])
    identity = CanonicalRunIdentity.model_validate(final_state["run_identity"])
    evidence = EvidenceState.model_validate(final_state["evidence_state"])
    raw_financial_dispatch = final_state.get("financial_dispatch_audit_projection")
    financial_dispatch = (
        None
        if raw_financial_dispatch is None
        else FinancialDispatchAuditProjection.model_validate(raw_financial_dispatch)
    )
    if identity.audit_digest is None:
        raise ValueError("canonical export identity is missing its audit digest")

    _remove_unvalidated_outputs(save_path)
    if terminal.terminal_outcome_kind is TerminalOutcomeKind.ANALYSIS_OUTCOME:
        outcome = AnalysisOutcome.model_validate(
            final_state["analysis_outcome_contract"]
        )
        body = render_analysis_outcome_report(
            outcome,
            terminal,
            evidence,
            financial_dispatch,
        )
        _write_markdown(save_path / "5_portfolio" / "analysis_outcome.md", body)
    elif terminal.terminal_outcome_kind is TerminalOutcomeKind.TRADING_DECISION:
        decision = TradingDecisionContract.model_validate(final_state["trading_decision"])
        body = render_decision_report(
            decision,
            terminal,
            evidence,
            financial_dispatch,
        )
        _write_markdown(save_path / "5_portfolio" / "decision.md", body)
    else:
        raise ValueError("operational failures cannot be published as decision reports")

    header = _report_header(
        ticker,
        terminal,
        identity,
        str(final_state["decision_audit_created_at"]),
    )
    complete_path = save_path / "complete_report.md"
    complete_path.write_text(header + body, encoding="utf-8")
    return complete_path

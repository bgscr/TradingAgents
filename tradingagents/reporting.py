"""Deterministic report publication for validated terminal outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from tradingagents.capability_routing import (
    MainlandCapabilityRoutingCheckpointCompatibility,
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingRunProjection,
    MainlandCapabilityRoutingShadowFailure,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchAuditProjection,
    FinancialDispatchAuditProjectionV2,
    FinancialDispatchAuditProjectionV3,
    FinancialDispatchAuditProjectionV6,
)
from tradingagents.decision_audit import write_immutable_decision_audit
from tradingagents.decision_policy import TradingDecisionContract, ValidatedDecisionContext
from tradingagents.evidence import (
    AnalysisOutcome,
    EvidenceState,
    InstrumentKind,
    SourceAcquisitionUnavailable,
    render_analysis_outcome,
)
from tradingagents.multiyear_trend import multiyear_trend_markdown
from tradingagents.report_exports import preflight_report_paths, publish_report_export
from tradingagents.report_presentation import (
    advisory_markdown,
    advisory_sections,
    decision_scope_markdown,
    evidence_diagnostic_markdown,
    is_chinese,
    localize_report,
    localized_analysis_outcome,
)
from tradingagents.report_storage import ReportStore
from tradingagents.terminal_contract import (
    CanonicalRunIdentity,
    DecisionCoverage,
    TerminalContract,
    TerminalOutcomeKind,
    validated_trading_decision_from_state,
)


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
            if outcome.provider == 'eastmoney_powershell':
                lines.extend(['### PowerShell 新闻传输', '',
                    '本次选择固定、无 profile 的 PowerShell Invoke-WebRequest；仅 available 表示取得合格文本，未使用 Requests 回退。失败不等于没有新闻。',
                    '收到时间不是历史发布时间证明；通过原结构、股票和截止日期校验后用于辅助分析，单页历史覆盖未知，评级规则不变。',
                    *(['未采用原因：`' + outcome.error_code + '`。'] if isinstance(outcome, SourceAcquisitionUnavailable)
                      and outcome.error_code and outcome.error_code.startswith('powershell_news_') else []), ''])
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
        f"- **History store access:** `{snapshot.history_store_status}`",
    ]
    # Only emit allowlisted runtime tokens, never arbitrary diagnostic text.
    diagnostics = set((snapshot.history_store_diagnostic or "").split(";"))
    for status in ("refreshed", "current_only_refreshed", "current_only_cached"):
        if f"runtime_history_store_status={status}" in diagnostics:
            lines.append(f"- **History runtime status:** `{status}`")
            if status.startswith("current_only_"):
                lines.append(
                    "- Current-analysis reuse only; `strict_history_qualified=false`."
                )
    if snapshot.history_store_status == "stored":
        lines.append("- Stored access does not establish strict point-in-time eligibility.")
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
    *,
    include_candidate_identities: bool = True,
) -> list[str]:
    if financial_dispatch is None:
        return []
    if isinstance(financial_dispatch, FinancialDispatchAuditProjectionV2):
        lines = [
            "## Financial dispatch operations",
            "",
            (
                "- **Capability Routing Plan:** "
                f"`{financial_dispatch.capability_routing_plan_signature}`"
            ),
            (
                "- **Financial manifest version:** "
                f"{financial_dispatch.financial_manifest_version}"
            ),
            f"- **Canonical request count:** {financial_dispatch.request_count}",
            (
                "- **Logical provider count:** "
                f"{financial_dispatch.logical_provider_count}"
            ),
            (
                "- **Logical candidate count:** "
                f"{financial_dispatch.logical_candidate_count}"
            ),
            (
                "- **Physical request count:** "
                f"{financial_dispatch.acquisition_attempt_count}"
            ),
            (
                "- **Duplicate-suppressed call count:** "
                f"{financial_dispatch.duplicate_suppressed_count}"
            ),
            "",
        ]
        if isinstance(financial_dispatch, FinancialDispatchAuditProjectionV3):
            lines[4:4] = [
                (
                    "- **Financial indicator manifest version:** "
                    f"{financial_dispatch.financial_indicator_manifest_version}"
                ),
                "- **Financial completeness row version:** financial-completeness-row-v4",
                "- **Financial aggregate completeness version:** financial-aggregate-completeness-v3",
                "- **Financial manifest audit version:** financial-manifest-audit-v3",
                (
                    "- **Indicator field-semantics version:** "
                    f"`{financial_dispatch.indicator_field_semantics_version}`"
                ),
                (
                    "- **Indicator metadata-compatibility version:** "
                    f"`{financial_dispatch.indicator_metadata_compatibility_version}`"
                ),
            ]
        if isinstance(financial_dispatch, FinancialDispatchAuditProjectionV6):
            lines[4:4] = [
                (
                    "- **Financial indicator manifest version:** "
                    f"{financial_dispatch.financial_indicator_manifest_version}"
                ),
                (
                    "- **Composite listing temporal proof:** "
                    f"`{financial_dispatch.listing_temporal_proof_identity}`"
                ),
                (
                    "- **Listing effective artifact/row:** "
                    f"`{financial_dispatch.effective_artifact_identity}` / "
                    f"`{financial_dispatch.effective_row_identity}`"
                ),
                (
                    "- **CNINFO security-lookup artifact:** "
                    f"`{financial_dispatch.security_lookup_artifact_identity}`"
                ),
                (
                    "- **Listing publication artifact/announcement:** "
                    f"`{financial_dispatch.publication_artifact_identity}` / "
                    f"`{financial_dispatch.publication_announcement_identity}`"
                ),
                (
                    "- **Listing document artifact/request:** "
                    f"`{financial_dispatch.document_artifact_identity}` / "
                    f"`{financial_dispatch.document_request_identity}`"
                    if financial_dispatch.document_artifact_identity is not None
                    else "- **Listing document artifact/request:** not required"
                ),
            ]
        binding = financial_dispatch.financial_eligibility_binding
        if binding is not None:
            lines[2:2] = [
                (
                    "- **Eligibility binding contract:** "
                    f"`{binding.contract_version}`"
                ),
                (
                    "- **Eligibility policy version:** "
                    f"`{binding.eligibility_policy_version}`"
                ),
                (
                    "- **Reporting calendar/deadline contract:** "
                    f"`{binding.reporting_calendar_deadline_contract_identity}`"
                ),
                (
                    "- **Target-construction contract:** "
                    f"`{binding.target_construction_contract_identity}`"
                ),
                (
                    "- **Completeness-policy identity:** "
                    f"`{binding.completeness_policy_identity}`"
                ),
                (
                    "- **Candidate-selection contract:** "
                    f"`{binding.candidate_selection_contract_identity}`"
                ),
                (
                    "- **Checkpoint projection:** "
                    f"`{binding.checkpoint_projection_version}`"
                ),
                (
                    "- **Audit projection:** "
                    f"`{binding.audit_projection_version}`"
                ),
                (
                    "- **Report projection:** "
                    f"`{binding.report_projection_version}`"
                ),
                (
                    "- **Availability-scope contract:** "
                    f"`{binding.availability_scope}`"
                ),
                (
                    "- **Rollout authority disposition:** "
                    f"`{binding.rollout_authority_disposition}`"
                ),
                f"- **Eligibility policy signature:** `{binding.policy_signature}`",
            ]
        for request in financial_dispatch.manifest_requests:
            lines.extend(
                [
                    f"### `{request.tool_name}` qualified request",
                    "",
                    f"- **Request reference:** `{request.request_ref}`",
                    (
                        "- **Logical providers / candidates:** "
                        f"{request.logical_provider_count} / "
                        f"{request.logical_candidate_count}"
                    ),
                    (
                        "- **Physical requests:** "
                        f"{request.physical_request_count}"
                    ),
                ]
            )
            aggregate = request.aggregate_family_assessment
            if aggregate is not None:
                lines.extend(
                    [
                        "",
                        "#### Family-level financial evidence",
                        "",
                        (
                            "- **Eligibility policy version:** "
                            f"`{aggregate.eligibility_policy_version}`"
                        ),
                        (
                            "- **Requested analysis scope:** "
                            f"`{aggregate.requested_analysis_scope.value}`"
                        ),
                        f"- **Company type:** `{aggregate.company_type.value}`",
                        f"- **Aggregate disposition:** `{aggregate.disposition.value}`",
                        (
                            "- **Aggregate typed reasons:** "
                            + (
                                ", ".join(
                                    f"`{reason.value}`"
                                    for reason in aggregate.typed_reasons
                                )
                                or "none"
                            )
                        ),
                        (
                            "- **Complete families:** "
                            + (
                                ", ".join(
                                    f"`{family.value}`"
                                    for family in aggregate.complete_families
                                )
                                or "none"
                            )
                        ),
                        f"- **Requirements identity:** `{aggregate.requirements_identity}`",
                    ]
                )
                for assessment in request.family_assessments:
                    lines.extend(
                        [
                            "",
                            f"##### `{assessment.family.value}` family",
                            "",
                            f"- **Complete:** {_format_scalar(assessment.complete)}",
                            f"- **Disposition:** `{assessment.disposition.value}`",
                            (
                                "- **Requirement:** "
                                f"`{assessment.requirement.status.value}`"
                            ),
                            (
                                "- **Target periods:** "
                                + ", ".join(
                                    period.isoformat()
                                    for period in assessment.target_periods
                                )
                            ),
                            (
                                "- **Selected periods:** "
                                + (
                                    ", ".join(
                                        period.isoformat()
                                        for period in assessment.selected_periods
                                    )
                                    or "none"
                                )
                            ),
                            (
                                "- **Current-only periods:** "
                                + (
                                    ", ".join(
                                        period.isoformat()
                                        for period in assessment.current_only_periods
                                    )
                                    or "none"
                                )
                            ),
                            (
                                "- **Rejected periods:** "
                                + (
                                    ", ".join(
                                        period.isoformat()
                                        for period in assessment.rejected_periods
                                    )
                                    or "none"
                                )
                            ),
                            (
                                "- **Conflicted periods:** "
                                + (
                                    ", ".join(
                                        period.isoformat()
                                        for period in assessment.conflicted_periods
                                    )
                                    or "none"
                                )
                            ),
                            (
                                "- **Missing periods:** "
                                + (
                                    ", ".join(
                                        period.isoformat()
                                        for period in assessment.missing_periods
                                    )
                                    or "none"
                                )
                            ),
                            (
                                "- **Typed reasons:** "
                                + (
                                    ", ".join(
                                        f"`{reason.value}`"
                                        for reason in assessment.typed_reasons
                                    )
                                    or "none"
                                )
                            ),
                            (
                                "- **Provider artifacts / selected lineage / conflicts:** "
                                f"{len(assessment.artifact_identities)} / "
                                f"{len(assessment.selection_identities)} / "
                                f"{len(assessment.conflict_identities)}"
                            ),
                        ]
                    )
                    for authority in assessment.requirement.authority_bindings:
                        lines.append(
                            "- **Requirement authority:** "
                            f"`{authority.authority.value}` / "
                            f"`{authority.authority_id}` / "
                            f"required={_format_scalar(authority.required)}"
                        )
                    for binding_label, bindings in (
                        (
                            "Missing material-premise binding",
                            assessment.missing_material_premise_bindings,
                        ),
                        (
                            "Conflicted material-premise binding",
                            assessment.conflicted_material_premise_bindings,
                        ),
                    ):
                        for binding in bindings:
                            lines.append(
                                f"- **{binding_label}:** `{binding.premise_id}` / "
                                + ", ".join(
                                    period.isoformat()
                                    for period in binding.affected_periods
                                )
                            )
                    for coverage in assessment.coverage:
                        lines.append(
                            "- **Period coverage:** "
                            f"{coverage.period.isoformat()} / "
                            f"`{coverage.disposition.value}` / "
                            "critical "
                            f"{_format_scalar(coverage.critical_coverage)} / "
                            f"{_format_scalar(coverage.critical_threshold)} / "
                            "core "
                            f"{_format_scalar(coverage.core_coverage)} / "
                            f"{_format_scalar(coverage.core_threshold)}"
                        )
            for manifest in request.manifests:
                lines.extend(
                    [
                        (
                            f"- **{manifest.statement_or_ratio_family} manifest:** "
                            f"`{manifest.manifest_identity}`"
                        ),
                        f"  - Disposition: `{manifest.disposition.value}`",
                        (
                            "  - Final rendered artifact: "
                            f"`{manifest.final_rendered_artifact_identity}`"
                        ),
                        (
                            "  - Retained provider artifacts: "
                            f"{len(manifest.artifact_identities)}"
                        ),
                        (
                            "  - Physical attempt events: "
                            f"{len(manifest.physical_attempt_events)}"
                        ),
                    ]
                )
                if manifest.contract_version == "financial-manifest-audit-v3":
                    lines.extend(
                        [
                            "  - Audit contract: `financial-manifest-audit-v3`",
                            "  - Indicator field semantics: "
                            f"`{manifest.indicator_field_semantics_version}`",
                            "  - Indicator metadata compatibility: "
                            f"`{manifest.indicator_metadata_compatibility_version}`",
                            "  - Family disposition: "
                            + (
                                "selected"
                                if manifest.selected_families
                                else "unresolved"
                                if manifest.unresolved_families
                                else "unselected"
                            ),
                        ]
                    )
                    for assessment in manifest.metadata_compatibility_assessments:
                        lines.append(
                            "  - Metadata assessment: "
                            f"`{assessment.assessment_identity}` / "
                            f"`{assessment.family.value}` / "
                            f"`{assessment.provider_id}` / "
                            f"`{assessment.compatibility_state}` / "
                            "current-only="
                            f"{_format_scalar(assessment.current_only_metadata_eligible)} / "
                            "strict-PIT="
                            f"{_format_scalar(assessment.strict_pit_metadata_eligible)}"
                        )
                    for rejection in manifest.metadata_incompatible_rejections:
                        lines.append(
                            "  - Metadata rejection: "
                            f"`{rejection.assessment_identity}` / "
                            + ", ".join(
                                f"`{reason.reason}`"
                                for reason in rejection.typed_reasons
                            )
                        )
                if manifest.requested_analysis_scope is not None:
                    lines.extend(
                        [
                            "  - Requested analysis scope: "
                            f"`{manifest.requested_analysis_scope.value}`",
                            "  - Availability scope: "
                            f"`{manifest.availability_scope.value if manifest.availability_scope is not None else 'none'}`",
                            "  - Availability basis: "
                            f"`{manifest.availability_basis.value if manifest.availability_basis is not None else 'none'}`",
                            "  - Eligibility policy version: "
                            f"`{manifest.eligibility_policy_version or 'none'}`",
                            "  - Availability reason: "
                            f"`{manifest.availability_reason.value if manifest.availability_reason is not None else 'none'}`",
                        ]
                    )
                targets = manifest.publication_target_construction
                if targets is not None:
                    lines.extend(
                        [
                            "  - Annual publication frontier: "
                            + (
                                targets.annual_publication_frontier.isoformat()
                                if targets.annual_publication_frontier is not None
                                else "none"
                            ),
                            "  - Reporting publication frontier: "
                            + (
                                targets.reporting_publication_frontier.isoformat()
                                if targets.reporting_publication_frontier is not None
                                else "none"
                            ),
                            "  - Annual target periods: "
                            + (
                                ", ".join(
                                    period.isoformat()
                                    for period in targets.annual_target_period_ends
                                )
                                or "none"
                            ),
                            "  - Reporting target periods: "
                            + (
                                ", ".join(
                                    period.isoformat()
                                    for period in targets.reporting_target_period_ends
                                )
                                or "none"
                            ),
                            "  - Target evidence integrity status: "
                            f"`{targets.evidence_integrity_status}`",
                            "  - Target failure reason: "
                            f"`{targets.failure_reason.value if targets.failure_reason is not None else 'none'}`",
                        ]
                    )
                    for excluded in targets.excluded_periods:
                        lines.append(
                            "  - Excluded publication period: "
                            f"{excluded.period_end.isoformat()} / "
                            f"`{excluded.publication_state.value}` / deadline "
                            f"{excluded.filing_deadline.isoformat()} / announcements "
                            + (
                                ", ".join(
                                    value.isoformat()
                                    for value in excluded.valid_announcement_dates
                                )
                                or "none"
                            )
                        )
                    for proof in targets.publication_evidence_bindings:
                        proof_identity = (
                            f"`{proof.candidate_identity}` / "
                            if include_candidate_identities
                            else ""
                        )
                        lines.append(
                            "  - Publication proof binding: "
                            f"{proof.period_end.isoformat()} / "
                            f"{proof.ann_date.isoformat()} / `{proof.provider_id}` / "
                            f"{proof_identity}`{proof.artifact_identity}`"
                        )
                    exception = targets.since_listing_exception
                    if exception is not None:
                        lines.extend(
                            [
                                "  - Since-listing exception: "
                                f"`{exception.reason}` / supported="
                                f"{_format_scalar(exception.supported)}",
                                "  - Since-listing provenance: "
                                f"{exception.listing_date.isoformat()} / "
                                f"`{exception.listing_provenance.provider_id}` / "
                                f"`{exception.listing_provenance.source_ref}` / "
                                f"{exception.listing_provenance.observed_at.isoformat()}",
                            ]
                        )
                for artifact_identity in manifest.artifact_identities:
                    lines.append(
                        "  - Retained provider artifact identity: "
                        f"`{artifact_identity}`"
                    )
                for attempt in manifest.physical_attempt_events:
                    lines.append(
                        "  - Physical attempt event identity: "
                        f"`{attempt.attempt_event_id}`"
                    )
                for selection in manifest.selections:
                    lines.append(
                        "  - Selected lineage: "
                        f"`{selection.provider_id}` / "
                        f"`{selection.period_identity}` / "
                        f"`{selection.artifact_identity}`"
                    )
                for revision_selection in manifest.revision_selections:
                    winner_identity = (
                        revision_selection.selected_candidate_identity
                        if include_candidate_identities
                        else revision_selection.selected_artifact_identity
                    )
                    lines.append(
                        "  - Revision selection: "
                        f"`{revision_selection.selection_key_identity}` / "
                        f"winner `{winner_identity}` / "
                        f"route rank {revision_selection.selected_provider_route_rank} / "
                        f"`{revision_selection.ordering_basis}` / "
                        f"`{revision_selection.chronology_claim}` / "
                        f"conflicted={_format_scalar(revision_selection.conflicted)} / "
                        f"policy `{revision_selection.eligibility_policy_version}` / "
                        "scope "
                        f"`{revision_selection.requested_analysis_scope.value if revision_selection.requested_analysis_scope is not None else 'not_applicable'}`"
                    )
                    for candidate_disposition in (
                        revision_selection.candidate_dispositions
                    ):
                        observation_identity = (
                            candidate_disposition.candidate_identity
                            if include_candidate_identities
                            else candidate_disposition.artifact_identity
                        )
                        lines.append(
                            "    - Revision observation: "
                            f"`{observation_identity}` / "
                            f"`{candidate_disposition.provider_id}` / "
                            f"route rank {candidate_disposition.provider_route_rank} / "
                            f"`{candidate_disposition.disposition}`"
                        )
                for rejection in manifest.rejected_periods:
                    rejection_identity = (
                        rejection.candidate_identity
                        if include_candidate_identities
                        else rejection.artifact_identity
                    )
                    lines.append(
                        "  - Rejected revision: "
                        f"`{rejection_identity}` "
                        f"({', '.join(reason.value for reason in rejection.reasons)})"
                    )
                for outcome in manifest.acquisition_outcomes:
                    lines.append(
                        "  - Acquisition outcome: "
                        f"`{outcome.provider_id}` / `{outcome.outcome}` / "
                        f"`{outcome.reason.value if outcome.reason else 'available'}`"
                    )
                for overlap in manifest.overlaps:
                    overlap_reference = (
                        overlap.overlapping_candidate_identity
                        if include_candidate_identities
                        else overlap.overlap_identity
                    )
                    lines.append(
                        "  - Overlap: "
                        f"`{overlap.disposition}` / "
                        f"`{overlap_reference}`"
                    )
                for conflict in manifest.conflicts:
                    lines.append(
                        "  - Critical conflict: "
                        f"`{conflict.normalized_field}` / "
                        f"`{conflict.conflict_identity}`"
                    )
            lines.append("")
        lines.extend(
            [
                "### Financial completeness",
                "",
                (
                    "| Capability | Family | Period | Provider | Company type | "
                    "Classification provenance | Metadata coverage | Ann date | "
                    "F ann date | F ann date state | Requested analysis scope | "
                    "Availability scope | Availability basis | Eligibility policy | "
                    "Availability reason | Report type | Provider comp type | Update flag | "
                    "Revision observed | Retrieved at | Local revision identity | Provider revision "
                    "ID | Currency | Declared unit | Observed units | Declared critical "
                    "fields | Declared core fields | Mapped normalized fields | Present "
                    "critical fields | Present core fields | Missing critical fields | "
                    "Missing core fields | Critical actual / threshold | Core actual / threshold | "
                    "Disposition | Typed reason | PIT eligible | Artifact identity | "
                    "Provider attempts | Since-listing exception | Listing date | "
                    "Listing provider | Listing source | Projection contract | "
                    "Compatibility state | Field-semantics version | Compatibility version | "
                    "Assessment identity | Metadata typed reason |"
                ),
                (
                    "|---|---|---|---|---|---|---:|---|---|---|---|---|---|---|"
                    "---|---|---|---|---|---|---|---|---|---|---|---:|---:|---|---|---|---|---:|---|---|---|---|---|---|---|---|---|---|"
                ),
            ]
        )
        for row in financial_dispatch.completeness_rows:
            provenance = (
                (
                    row.company_type_resolution_method.value
                    if row.company_type_resolution_method is not None
                    else "not_available"
                )
                + (
                    ":" + ",".join(row.company_type_provenance)
                    if row.company_type_provenance
                    else ""
                )
            )
            reasons = ", ".join(row.typed_reason) if row.typed_reason else "none"
            lines.append(
                "| "
                + " | ".join(
                    (
                        row.capability.value,
                        row.statement_or_ratio_family,
                        row.period.isoformat(),
                        row.provider,
                        row.company_type.value,
                        provenance,
                        format(row.metadata_coverage, "f"),
                        (
                            row.ann_date.isoformat()
                            if row.ann_date is not None
                            else "none"
                        ),
                        (
                            row.f_ann_date.isoformat()
                            if row.f_ann_date is not None
                            else "none"
                        ),
                        row.f_ann_date_state or "not_applicable",
                        (
                            row.requested_analysis_scope.value
                            if row.requested_analysis_scope is not None
                            else "not_applicable"
                        ),
                        (
                            row.availability_scope.value
                            if row.availability_scope is not None
                            else "none"
                        ),
                        (
                            row.availability_basis.value
                            if row.availability_basis is not None
                            else "none"
                        ),
                        row.eligibility_policy_version or "none",
                        (
                            row.availability_reason.value
                            if row.availability_reason is not None
                            else "none"
                        ),
                        row.report_type or "none",
                        row.provider_comp_type or "none",
                        row.update_flag or "none",
                        (
                            row.revision_observed_at.isoformat()
                            if row.revision_observed_at is not None
                            else "none"
                        ),
                        (
                            row.retrieved_at.isoformat()
                            if row.retrieved_at is not None
                            else "none"
                        ),
                        row.local_provider_revision_identity or "none",
                        row.provider_filing_revision_id or "none",
                        row.currency,
                        row.declared_normalized_unit or "none",
                        ", ".join(row.observed_normalized_units) or "none",
                        ", ".join(row.declared_critical_fields) or "none",
                        ", ".join(row.declared_core_fields) or "none",
                        ", ".join(row.mapped_normalized_fields) or "none",
                        ", ".join(row.present_critical_fields) or "none",
                        ", ".join(row.present_core_fields) or "none",
                        ", ".join(row.missing_critical_fields) or "none",
                        ", ".join(row.missing_core_fields) or "none",
                        (
                            f"{format(row.critical_coverage, 'f')} / "
                            f"{format(row.critical_threshold, 'f')}"
                        ),
                        (
                            f"{format(row.core_coverage, 'f')} / "
                            f"{format(row.core_threshold, 'f')}"
                        ),
                        row.disposition,
                        reasons,
                        "yes" if row.pit_eligible else "no",
                        row.artifact_identity or "none",
                        str(row.provider_attempt_count),
                        (
                            "yes"
                            if row.since_listing_exception
                            else "no"
                        ),
                        (
                            row.listing_date.isoformat()
                            if row.listing_date is not None
                            else "none"
                        ),
                        row.listing_provider or "none",
                        row.listing_source_ref or "none",
                        row.contract_version,
                        row.compatibility_state or "not_applicable",
                        row.indicator_field_semantics_version or "not_applicable",
                        (
                            row.indicator_metadata_compatibility_version
                            or "not_applicable"
                        ),
                        (
                            row.metadata_compatibility_assessment_identity
                            or "none"
                        ),
                        ", ".join(row.metadata_typed_reasons) or "none",
                    )
                )
                + " |"
            )
        lines.append("")
        return lines
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


def _render_shadow_financial_dispatch_comparison(
    rollout: MainlandCapabilityRoutingRunProjection | None,
    authoritative: FinancialDispatchAuditProjection | None,
    shadow: FinancialDispatchAuditProjection | None,
    failure: MainlandCapabilityRoutingShadowFailure | None = None,
) -> list[str]:
    """Render non-authoritative shadow coverage and its audit-only operations."""

    if shadow is None and failure is None:
        return []
    if (
        rollout is None
        or rollout.disposition
        is not MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
        or (shadow is not None and not isinstance(shadow, FinancialDispatchAuditProjectionV2))
    ):
        raise ValueError("shadow financial report requires a non-authoritative rollout")
    authoritative_requests = authoritative.request_count if authoritative is not None else 0
    authoritative_attempts = (
        authoritative.acquisition_attempt_count if authoritative is not None else 0
    )
    shadow_request_count = shadow.request_count if shadow is not None else 0
    shadow_provider_count = (
        shadow.logical_provider_count if shadow is not None else 0
    )
    shadow_candidate_count = (
        shadow.logical_candidate_count if shadow is not None else 0
    )
    shadow_attempt_count = (
        shadow.acquisition_attempt_count if shadow is not None else 0
    )
    lines = [
        "## Qualified routing shadow comparison",
        "",
        "Shadow selections are non-authoritative. Legacy-selected financial output "
        "remains authoritative for model-visible facts, evidence gates, terminal "
        "outcomes, signals, reports, and memory writes.",
        "",
        f"- **Rollout mode:** `{rollout.mode.value}`",
        f"- **Authority disposition:** `{rollout.disposition.value}`",
        f"- **Plan version:** {rollout.plan_version}",
        f"- **Plan signature:** `{rollout.plan_signature}`",
        f"- **Enabled Tushare capabilities:** {len(rollout.enabled_tushare_capabilities)}",
        f"- **Legacy authoritative requests:** {authoritative_requests}",
        f"- **Legacy authoritative physical requests:** {authoritative_attempts}",
        f"- **Shadow logical requests:** {shadow_request_count}",
        f"- **Shadow logical providers:** {shadow_provider_count}",
        f"- **Shadow candidate coverage:** {shadow_candidate_count}",
        f"- **Shadow physical requests:** {shadow_attempt_count}",
        "",
    ]
    if failure is not None:
        failure_lines = [
            (
                "- **Shadow failure diagnostic contract:** "
                f"`{failure.contract_version}`"
            ),
            (
                "- **Shadow failure diagnostic code:** "
                f"`{failure.diagnostic_code}`"
            ),
            f"- **Sanitized shadow failures:** {failure.failure_count}",
        ]
        localized_failures = failure.localized_failures()
        if failure.contract_version != "1.0":
            retained_count = (
                failure.retained_failure_count
                if failure.contract_version == "2.1"
                and failure.retained_failure_count is not None
                else len(localized_failures)
            )
            dropped_count = (
                failure.dropped_failure_count
                if failure.contract_version == "2.1"
                and failure.dropped_failure_count is not None
                else failure.failure_count - len(localized_failures)
            )
            failure_lines.extend(
                (
                    (
                        "- **Retained localized failures:** "
                        f"{retained_count}"
                    ),
                    (
                        "- **Dropped localized failures:** "
                        f"{dropped_count}"
                    ),
                )
            )
        for index, localized in enumerate(localized_failures, start=1):
            prefix = f"- **Failure {index}"
            failure_lines.extend(
                (
                    f"{prefix} stage:** `{localized.stage.value}`",
                    f"{prefix} kind:** `{localized.failure_kind.value}`",
                )
            )
            if localized.typed_acquisition_reason is not None:
                failure_lines.append(
                    f"{prefix} acquisition reason:** "
                    f"`{localized.typed_acquisition_reason.value}`"
                )
            if localized.temporal_rejection_reasons:
                reasons = ", ".join(
                    f"`{reason.value}`"
                    for reason in localized.temporal_rejection_reasons
                )
                failure_lines.append(
                    f"{prefix} temporal rejection reasons:** {reasons}"
                )
            if (
                localized.exception_family is not None
                and localized.exception_class is not None
            ):
                failure_lines.append(
                    f"{prefix} exception:** "
                    f"`{localized.exception_family.value}."
                    f"{localized.exception_class.value}`"
                )
            if localized.typed_reason is not None:
                failure_lines.append(
                    f"{prefix} typed reason:** `{localized.typed_reason.value}`"
                )
            if localized.canonical_request_reference is not None:
                failure_lines.append(
                    f"{prefix} canonical request:** "
                    f"`{localized.canonical_request_reference}`"
                )
            if localized.selection_kind is not None:
                failure_lines.append(
                    f"{prefix} selection kind:** `{localized.selection_kind.value}`"
                )
            failure_lines.extend(
                (
                    f"{prefix} context constructed:** "
                    f"{str(localized.context_constructed).lower()}",
                    f"{prefix} dispatcher constructed:** "
                    f"{str(localized.dispatcher_constructed).lower()}",
                )
            )
        lines[-1:-1] = failure_lines
    if shadow is not None:
        shadow_operations = _render_financial_dispatch_operations(
            shadow,
            include_candidate_identities=False,
        )
        if shadow_operations:
            shadow_operations[0] = (
                "## Qualified routing shadow financial operations "
                "(non-authoritative)"
            )
            shadow_operations[2:2] = [
                "These audit-only shadow rows create no authoritative Source Facts, "
                "Decision Gate changes, Trading Decisions, signals, or memory writes.",
                "",
            ]
            lines.extend(shadow_operations)
    return lines


def _render_checkpoint_compatibility(
    compatibility: MainlandCapabilityRoutingCheckpointCompatibility | None,
) -> list[str]:
    if compatibility is None:
        return []
    lines = [
        "## Checkpoint / new-run compatibility",
        "",
        f"- **Compatibility action:** `{compatibility.action.value}`",
        (
            "- **Typed compatibility reason:** `"
            + (
                compatibility.reason.value
                if compatibility.reason is not None
                else "none"
            )
            + "`"
        ),
        (
            "- **Provider I/O during compatibility check:** "
            f"{_format_scalar(compatibility.provider_io_performed)}"
        ),
        (
            "- **Checkpoint rewrite permitted:** "
            f"{_format_scalar(compatibility.checkpoint_rewrite_permitted)}"
        ),
        (
            "- **Historical state preserved:** "
            f"{_format_scalar(compatibility.historical_state_preserved)}"
        ),
    ]
    if compatibility.previous_run_id is not None:
        lines.append(f"- **Previous run ID:** `{compatibility.previous_run_id}`")
    if compatibility.new_run_id is not None:
        lines.append(f"- **New run ID:** `{compatibility.new_run_id}`")
    lines.append("")
    return lines


def render_decision_report(
    decision: TradingDecisionContract,
    terminal: TerminalContract,
    evidence: EvidenceState | None = None,
    financial_dispatch: FinancialDispatchAuditProjection | None = None,
    *,
    capability_routing_rollout: MainlandCapabilityRoutingRunProjection | None = None,
    checkpoint_compatibility: (
        MainlandCapabilityRoutingCheckpointCompatibility | None
    ) = None,
    shadow_financial_dispatch: FinancialDispatchAuditProjection | None = None,
    shadow_failure: MainlandCapabilityRoutingShadowFailure | None = None,
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
    lines.extend(_render_checkpoint_compatibility(checkpoint_compatibility))
    lines.extend(
        _render_shadow_financial_dispatch_comparison(
            capability_routing_rollout,
            financial_dispatch,
            shadow_financial_dispatch,
            shadow_failure,
        )
    )
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
    *,
    capability_routing_rollout: MainlandCapabilityRoutingRunProjection | None = None,
    checkpoint_compatibility: (
        MainlandCapabilityRoutingCheckpointCompatibility | None
    ) = None,
    shadow_financial_dispatch: FinancialDispatchAuditProjection | None = None,
    shadow_failure: MainlandCapabilityRoutingShadowFailure | None = None,
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
    lines.extend(_render_checkpoint_compatibility(checkpoint_compatibility))
    lines.extend(
        _render_shadow_financial_dispatch_comparison(
            capability_routing_rollout,
            financial_dispatch,
            shadow_financial_dispatch,
            shadow_failure,
        )
    )
    lines.extend([render_analysis_outcome(outcome), ""])
    return "\n".join(lines)


def budget_data_gap_markdown(state: dict, language="English") -> str:
    """Render fixed operational gaps separately from the validated decision."""
    from tradingagents.budget_data import ALPHA_ENRICHMENT_TOOLS
    from tradingagents.evidence_diagnostics import NEWS_DIAGNOSTIC_LABELS

    receipt = state.get("analysis_budget") or {}
    allowed = {"user_disabled", "not_configured", "authentication_failed", "permission_denied", "rate_limited"}
    core_gaps = {
        ("get_fundamentals.business", "akshare", "optional_business_omitted"),
        ("get_fundamentals.financial_summary", "akshare", "optional_financial_summary_omitted"),
        ("get_news.coverage", "akshare", "news_window_coverage_unknown"),
        ("get_news", "akshare", "news_unavailable"),
    }
    rows = [item for item in receipt.get("data_gaps", [])
            if (item.get("tool") in ALPHA_ENRICHMENT_TOOLS
                and item.get("provider") == "alpha_vantage" and item.get("reason") in allowed)
            or (item.get("tool"), item.get("provider"), item.get("reason")) in core_gaps]
    if not rows:
        return ""
    heading = "## 预算运行数据缺口" if is_chinese(language) else "## Budget run data gaps"
    warning = ("运行诊断，不是来源事实；未自动换源，不代表没有新闻或内幕交易。"
               if is_chinese(language) else
               "Operational diagnostics, not source facts. No automatic fallback; these gaps do not mean no news or insider transactions.")
    lines = [heading, "", warning, ""]
    for item in rows:
        coverage_gap = item['reason'] == 'news_window_coverage_unknown'
        label = 'DATA_GAP' if coverage_gap else 'DATA_UNAVAILABLE'
        lines.append(f"- {label}: `{item['tool']}`; `{item['provider']}`; `{item['reason']}`.")
        if coverage_gap:
            lines.append(NEWS_DIAGNOSTIC_LABELS[item['reason']][int(is_chinese(language))] + ".")
    return "\n".join([*lines, ""])


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


@dataclass(frozen=True, slots=True)
class SupplementalStatementRequest:
    """Explicit trusted retained input; never discovered from graph state or cache.

    The caller supplies the authorized original bytes and independently trusted
    digest. Report symbol and cutoff are bound from the existing run, not here.
    """

    raw_payload: bytes
    expected_sha256: str
    report_period: str
    row_index: int


def _supplemental_report_text(final_state, ticker, request, output_language="Chinese"):
    if request is None:
        return ""
    from tradingagents.supplemental_observation import (
        observe_retained_payload,
        render_supplemental,
    )

    def symbol(value):
        if type(value) is not str or re.fullmatch(r"[0-9]{6}\.(?:SH|SS|SZ)", value) is None:
            raise ValueError("supplemental_report_binding_invalid")
        return value[:-3] + ".SH" if value.endswith(".SS") else value

    try:
        if type(request) is not SupplementalStatementRequest:
            raise ValueError("supplemental_request_type_invalid")
        terminal = TerminalContract.model_validate(final_state["terminal_contract"])
        if terminal.terminal_outcome_kind not in {
            TerminalOutcomeKind.ANALYSIS_OUTCOME, TerminalOutcomeKind.TRADING_DECISION,
        }:
            raise ValueError("supplemental_terminal_invalid")
        evidence = EvidenceState.model_validate(final_state["evidence_state"])
        identity = evidence.instrument_identity
        if identity is None or not identity.is_authoritative:
            raise ValueError("supplemental_identity_missing")
        bound_symbol = symbol(identity.symbol)
        expected_venue = {"SH": "XSHG", "SZ": "XSHE"}[bound_symbol[-2:]]
        if (identity.instrument_kind is not InstrumentKind.EQUITY
                or identity.venue != expected_venue or identity.currency != "CNY"):
            raise ValueError("supplemental_equity_identity_invalid")
        def identity_matches(instrument):
            return (symbol(instrument.symbol) == bound_symbol
                    and instrument.instrument_kind is InstrumentKind.EQUITY
                    and instrument.venue == expected_venue and instrument.currency == "CNY")
        if symbol(ticker) != bound_symbol or symbol(final_state["company_of_interest"]) != bound_symbol:
            raise ValueError("supplemental_symbol_mismatch")
        raw_cutoff = final_state["trade_date"]
        if type(raw_cutoff) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_cutoff) is None:
            raise ValueError("supplemental_cutoff_invalid")
        cutoff = date.fromisoformat(raw_cutoff)
        raw_context = final_state.get("validated_decision_context")
        if raw_context is not None:
            context = ValidatedDecisionContext.model_validate(raw_context)
            if not identity_matches(context.instrument) or context.as_of_date != cutoff:
                raise ValueError("supplemental_context_mismatch")
        if final_state.get("trading_decision") is not None or terminal.terminal_outcome_kind is TerminalOutcomeKind.TRADING_DECISION:
            decision = validated_trading_decision_from_state(final_state)
            if decision is None or not identity_matches(decision.instrument) or decision.as_of_date != cutoff:
                raise ValueError("supplemental_decision_mismatch")
        observation = observe_retained_payload(
            request.raw_payload, expected_sha256=request.expected_sha256,
            ticker=bound_symbol, report_period=request.report_period,
            as_of=cutoff.isoformat(), row_index=request.row_index,
        )
        chinese = is_chinese(output_language)
        footer = ("附录由报告导出完整性摘要覆盖；这不是决策审计的资格认证。" if chinese else
                  "The appendix is covered by the report export integrity digest; this is not qualification by the decision audit.")
        return "\n\n---\n\n" + render_supplemental(observation, chinese=chinese) + "\n" + footer + "\n"
    except (ValueError, TypeError, KeyError, AttributeError):
        # No arbitrary state, payload, validation detail or source value escapes.
        raise ValueError("supplemental_report_binding_invalid") from None


def write_report_tree(final_state: dict, ticker: str, save_path, *, output_language="English",
                      supplemental_statement: SupplementalStatementRequest | None = None,
                      company_announcement=None, announcement_timeout_seconds=25, announcement_remaining=None,
                      news_source_selection=None) -> Path:
    """Publish the validated decision and separately labeled advisory stages."""

    from tradingagents.portfolio_research_run import active_selection
    if active_selection() is not None or final_state.get("portfolio_research_terminal"):
        from tradingagents.portfolio_research_terminal import publish
        return publish(final_state,ticker,save_path,output_language)
    supplemental_text = _supplemental_report_text(final_state, ticker, supplemental_statement, output_language)
    from tradingagents.news_projection import render_source_selection_report
    if news_source_selection is not None:
        render_source_selection_report(news_source_selection, final_state.get('company_of_interest'), final_state.get('trade_date'))
    news_selection_text = render_source_selection_report(news_source_selection, ticker, final_state.get('trade_date'))
    with ReportStore(save_path) as publication:
        preflight_report_paths(publication)
        return _write_report_tree(final_state, ticker, publication, output_language, supplemental_text,
                                  company_announcement, announcement_timeout_seconds, announcement_remaining, news_selection_text)


def _write_report_tree(final_state, ticker, publication, output_language, supplemental_text="",
                       company_announcement=None, announcement_timeout_seconds=25, announcement_remaining=None, news_selection_text=""):
    save_path = publication.root
    write_immutable_decision_audit(final_state, save_path, publication=publication)
    terminal = TerminalContract.model_validate(final_state["terminal_contract"])
    identity = CanonicalRunIdentity.model_validate(final_state["run_identity"])
    evidence = EvidenceState.model_validate(final_state["evidence_state"])
    raw_financial_dispatch = final_state.get("financial_dispatch_audit_projection")
    financial_dispatch = (
        None
        if raw_financial_dispatch is None
        else TypeAdapter(FinancialDispatchAuditProjection).validate_python(
            raw_financial_dispatch
        )
    )
    raw_shadow_financial_dispatch = final_state.get(
        "financial_dispatch_shadow_audit_projection"
    )
    shadow_financial_dispatch = (
        None
        if raw_shadow_financial_dispatch is None
        else TypeAdapter(FinancialDispatchAuditProjection).validate_python(
            raw_shadow_financial_dispatch
        )
    )
    raw_rollout = final_state.get("capability_routing_rollout")
    capability_routing_rollout = (
        None
        if raw_rollout is None
        else MainlandCapabilityRoutingRunProjection.model_validate(raw_rollout)
    )
    raw_checkpoint_compatibility = final_state.get(
        "capability_routing_checkpoint_compatibility"
    )
    checkpoint_compatibility = (
        None
        if raw_checkpoint_compatibility is None
        else MainlandCapabilityRoutingCheckpointCompatibility.model_validate(
            raw_checkpoint_compatibility
        )
    )
    raw_shadow_failure = final_state.get("financial_dispatch_shadow_failure")
    shadow_failure = (
        None
        if raw_shadow_failure is None
        else MainlandCapabilityRoutingShadowFailure.model_validate(
            raw_shadow_failure
        )
    )
    if identity.audit_digest is None:
        raise ValueError("canonical export identity is missing its audit digest")

    decision = None
    if terminal.terminal_outcome_kind is TerminalOutcomeKind.ANALYSIS_OUTCOME:
        outcome = AnalysisOutcome.model_validate(
            final_state["analysis_outcome_contract"]
        )
        body = render_analysis_outcome_report(
            outcome,
            terminal,
            evidence,
            financial_dispatch,
            capability_routing_rollout=capability_routing_rollout,
            checkpoint_compatibility=checkpoint_compatibility,
            shadow_financial_dispatch=shadow_financial_dispatch,
            shadow_failure=shadow_failure,
        )
        body = body.replace(
            render_analysis_outcome(outcome),
            localized_analysis_outcome(outcome, output_language),
        )
        terminal_path = "5_portfolio/analysis_outcome.md"
    elif terminal.terminal_outcome_kind is TerminalOutcomeKind.TRADING_DECISION:
        decision = TradingDecisionContract.model_validate(final_state["trading_decision"])
        body = render_decision_report(
            decision,
            terminal,
            evidence,
            financial_dispatch,
            capability_routing_rollout=capability_routing_rollout,
            checkpoint_compatibility=checkpoint_compatibility,
            shadow_financial_dispatch=shadow_financial_dispatch,
            shadow_failure=shadow_failure,
        )
        terminal_path = "5_portfolio/decision.md"
    else:
        raise ValueError("operational failures cannot be published as decision reports")

    body = decision_scope_markdown(final_state, decision, evidence, output_language) + "\n" + localize_report(body, output_language)
    body += "\n" + evidence_diagnostic_markdown(evidence, output_language)
    body += "\n" + budget_data_gap_markdown(final_state, output_language)
    documents = {terminal_path: body}
    trend = multiyear_trend_markdown(
        evidence, symbol=ticker, as_of=final_state.get("trade_date"), language=output_language,
    )
    if trend:
        documents["1_analysts/multiyear_trend.md"] = trend
    from tradingagents.moneyflow import moneyflow_markdown

    moneyflow = moneyflow_markdown(final_state, language=output_language)
    if moneyflow:
        documents["1_analysts/moneyflow.md"] = moneyflow
    from tradingagents.margin_balance import margin_balance_markdown

    margin_balance = margin_balance_markdown(final_state, language=output_language)
    if margin_balance:
        documents["1_analysts/margin_balance.md"] = margin_balance
    from tradingagents.powershell_fundflow import fundflow_markdown
    eastmoney_flow = fundflow_markdown(final_state)
    if eastmoney_flow:
        documents["1_analysts/eastmoney_fundflow.md"] = eastmoney_flow

    header = _report_header(
        ticker,
        terminal,
        identity,
        str(final_state["decision_audit_created_at"]),
    )
    advisory = []
    stages = []
    synthesis_text = ""
    if ((synthesis := final_state.get("personal_research_conclusion")) is not None
            and isinstance(final_state.get("personal_report_sources"), tuple)):
        from tradingagents.personal_research_conclusion import (
            PersonalResearchConclusion,
            render_personal_research_conclusion,
        )

        if isinstance(synthesis, PersonalResearchConclusion) and synthesis.matches(final_state):
            relative = "2_research/conclusion.md"
            text = render_personal_research_conclusion(synthesis, output_language)
            documents[relative] = text
            stages.append(relative)
            synthesis_text = "\n\n---\n\n" + text
    rule_summary = ""
    # Equality only deduplicates the old text. The new system summary is rendered
    # from the gated contract, never attributed to the author of a matching string.
    canonical_decision_text = None
    if decision is not None:
        from tradingagents.agents.managers.direction_selector import render_trading_decision

        canonical_decision_text = render_trading_decision(decision)
    target = _current_full_target.get()
    targeted = target is not None and target == (ticker, str(final_state.get("trade_date"))) and ticker == final_state.get("company_of_interest") and is_chinese(output_language)
    verified = section(publication, ticker, str(final_state["trade_date"]), getattr(financial_dispatch, "requests", ())) if targeted else None
    for relative, title, chinese_title, content in advisory_sections(final_state):
        if relative == "4_risk/manager.md" and content == canonical_decision_text:
            rating = decision.rating.value
            if is_chinese(output_language):
                label = {"Buy": "买入", "Hold": "持有", "Sell": "卖出"}.get(rating, rating)
                text = ("# 规则决定摘要（系统生成）\n\n"
                        f"规则评级：{label}。由本次通过门禁的决定合同生成，"
                        "不是独立的风险经理模型建议或多分析员共识。\n\n"
                        f"决定编号：`{decision.decision_id}`。完整规则依据见报告前部。\n")
            else:
                text = ("# Rule decision summary (system-generated)\n\n"
                        f"Rule-based rating: {rating}. Generated from the gated decision contract, "
                        "not a separate risk-manager model recommendation or analyst consensus.\n\n"
                        f"Decision ID: `{decision.decision_id}`. See the rule basis above.\n")
            documents[relative] = text
            rule_summary = "\n\n---\n\n" + text
            continue
        text = advisory_markdown(
            chinese_title if is_chinese(output_language) else title, content, output_language,
            decision=decision, evidence=evidence,
            flow_windows_unverified=final_state.get("moneyflow_observation") is not None,
        )
        documents[relative] = text
        if targeted:
            text = checked_advisory(relative, text, verified)
        documents[relative] = text
        stages.append(relative)
        advisory.append(text)
    appendix = ""
    if advisory:
        heading = "辅助分析阶段正文" if is_chinese(output_language) else "Advisory stage reports"
        appendix = "\n\n---\n\n# " + heading + "\n\n" + "\n\n".join(advisory)
    trend_section = "\n\n---\n\n" + trend if trend else ""
    if moneyflow:
        trend_section += "\n\n---\n\n" + moneyflow
    if margin_balance:
        trend_section += "\n\n---\n\n" + margin_balance
    from tradingagents.announcement_report import announcement_appendix

    announcement = announcement_appendix(final_state, ticker, company_announcement,
                                        language=output_language, timeout=announcement_timeout_seconds,
                                        remaining=announcement_remaining)
    documents["complete_report.md"] = localize_report(header, output_language) + news_selection_text + body + synthesis_text + trend_section + ('\n\n' + eastmoney_flow if eastmoney_flow else '') + rule_summary + appendix + supplemental_text + announcement
    return publish_report_export(publication, identity, output_language, documents, stages)



from contextvars import ContextVar
_current_full_target = ContextVar("current_full_financial_target", default=None)
"""Offline candidate for a dedicated, deterministic financial-report section."""
from decimal import Decimal

TABLES = {"income": ("利润表", "归母净利润", "n_income_attr_p"),
          "cash": ("现金流量表", "现金及现金等价物净增加额", "n_incr_cash_cash_equ"),
          "balance": ("资产负债表", "资产总额", "total_assets")}

def render(ticker, cutoff, facts, latest_references=()):
    lines = [f"# {ticker} 财务报告期核验", "", f"分析截止日 {cutoff}。按本次已核验 CURRENT 观测展示。", ""]
    rejected = 0
    for ref in latest_references:
        table = ref.get("table")
        expected = facts.get(table) if isinstance(table,str) else None
        if not expected or ref != {"table":table, **expected}:
            rejected += 1
    for key,(title,label,field) in TABLES.items():
        fact = facts.get(key)
        if not fact or fact["field"] != field:
            lines.append(f"- {title}：本次工具未提供可核验的{label}。")
            continue
        amount = Decimal(fact["value"]) / Decimal(100000000)
        lines.append(f"- {title}：{fact['period']}，公告日 {fact['announcement']}，{fact['basis']}；{label} {amount:f} 亿元。附件 SHA-256：{fact['source']}。")
    lines += ["", "季度利润与现金流按报告期累计展示，未推导单季金额。"]
    if rejected:
        lines.append(f"有 {rejected} 条最新期间引用未通过核验，已从本节剔除。")
    return "\n".join(lines)+"\n"

import zlib,csv,gzip,io,re
from datetime import date
from hashlib import sha256
from tradingagents.financial_observations import CURRENT_STATEMENT_COLUMNS,CURRENT_OBSERVATION_MARKER,STATEMENT_TITLES,_valid_row
from tradingagents.dataflows.financial_dispatch import FinancialDispatchAuditRequest


TOOLS={"income":("get_income_statement","income_statement","n_income_attr_p","net_income"),"cash":("get_cashflow","cash_flow","n_incr_cash_cash_equ","net_change_in_cash"),"balance":("get_balance_sheet","balance_sheet","total_assets","total_assets")}

def extract(store,ticker,cutoff,request,spec):
    tool,statement,field,normalized=spec
    if not isinstance(request,FinancialDispatchAuditRequest) or request.tool_name!=tool or request.statement_type.value!=statement or request.frequency.value!="quarterly" or request.as_of_date!=date.fromisoformat(cutoff):return None
    digest=request.artifact_sha256
    available=[o for o in request.outcomes if o.outcome=="available"]
    if not digest or len(available)!=1 or (available[0].provider,available[0].artifact_sha256)!=("tushare",digest):return None
    with gzip.GzipFile(fileobj=io.BytesIO(store.read_bytes(f"evidence_artifacts/sha256/{digest[:2]}/{digest}.utf8.gz"))) as f:
        raw=f.read(4_000_001)
    if len(raw)>4_000_000 or sha256(raw).hexdigest()!=digest:return None
    lines=raw.decode().splitlines()
    header=",".join(CURRENT_STATEMENT_COLUMNS)
    if not lines or lines[0]!=f"# {STATEMENT_TITLES[tool]} data for {ticker} (quarterly)" or lines.count(header)!=1:return None
    i=lines.index(header)
    pre=lines[:i]
    if any(not l.startswith("# ") for l in pre) or any(pre.count(tag)!=1 for tag in (CURRENT_OBSERVATION_MARKER,"# CURRENT-only; source=tushare; not strict PIT evidence.")):return None
    if tool!="get_balance_sheet" and pre.count("# Income/cashflow quarterly rows retain cumulative report_type=1 values, not standalone-quarter deltas.")!=1:return None
    if not any(l.startswith("# degraded=false;") for l in pre) or any(l.startswith(("# missing","# stale","# degraded=true")) for l in pre):return None
    parsed=list(csv.DictReader(lines[i:],strict=True))
    if any(None in r or None in r.values() or not _valid_row(r,"quarterly") for r in parsed) or len(set(lines[i+1:]))!=len(parsed):return None
    eligible=[r for r in parsed if max(r["period_end"],r["ann_date"],r["f_ann_date"])<=cutoff]
    last=max((r["period_end"] for r in eligible),default=None)
    found=[r for r in eligible if r["period_end"]==last and r["provider_field"]==field and r["field"]==normalized and r["value_status"]=="reported"]
    if len(found)!=1:return None
    r=found[0]
    return dict(period=last,announcement=r["ann_date"],basis="期末余额" if tool=="get_balance_sheet" else "报告期累计",value=r["value"],source=digest,field=field,view="quarterly")

def section(store,ticker,cutoff,requests):
    facts={}
    for key,spec in TOOLS.items():
        matches=[r for r in requests if r.tool_name==spec[0] and r.frequency.value=="quarterly"]
        if len(matches)==1:
            try:
                fact=extract(store,ticker,cutoff,matches[0],spec)
            except (zlib.error,ValueError,OSError,EOFError,csv.Error,KeyError):
                fact=None
            if fact:facts[key]=fact
    return render(ticker,cutoff,facts)

def checked_advisory(relative,text,verified):
    if relative=="1_analysts/fundamentals.md":return verified
    if relative.startswith(("2_research/","3_trading/","4_risk/")):return "此阶段无逐项核验的自由正文，已省略。经核验的结构化决定与市场数据见主报告。"
    return text



"""Capability-specific mainland financial routing and per-period selection.

This module is intentionally a selection boundary, not a provider transport.
Ticket 04-09 adapters persist their immutable artifacts before returning the
payload-free results consumed here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1,
    FINANCIAL_RATIO_DECLARATION_V1,
    FINANCIAL_RATIO_DECLARATION_V2,
    FinancialAcquisitionManifest,
    FinancialAcquisitionOutcome,
    FinancialAvailabilityScope,
    FinancialCapability,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialConsolidationScope,
    FinancialCriticalValueConflict,
    FinancialEvidenceDisposition,
    FinancialFamilyAssessmentReason,
    FinancialFamilyPeriodCoverage,
    FinancialFamilyRequirementStatus,
    FinancialIndicatorAggregateAssessment,
    FinancialIndicatorEvidenceRequirements,
    FinancialIndicatorFamilyAssessment,
    FinancialListingProvenance,
    FinancialMaterialPremiseFamilyBinding,
    FinancialPeriodCandidate,
    FinancialPeriodCompletenessAssessment,
    FinancialPeriodDisposition,
    FinancialPeriodOverlapFinding,
    FinancialPeriodRejectionReason,
    FinancialPeriodSelection,
    FinancialProviderArtifactEntry,
    FinancialProviderArtifactIdentity,
    FinancialProviderCandidate,
    FinancialRatioFamily,
    FinancialRatioHistoryCompletenessAssessment,
    FinancialRejectedPeriod,
    FinancialReportingFrequency,
    FinancialRevisionSelection,
    FinancialStatementType,
    assess_financial_history_coverage,
    assess_financial_period_candidate,
    assess_financial_ratio_history_coverage,
    compare_financial_period_candidates,
    create_financial_revision_selection,
    financial_provider_revision_sequence_is_qualified,
    financial_revision_selection_key,
    mark_financial_period_company_type_conflicted,
    mark_financial_period_conflicted,
    reject_financial_period_for_company_type,
    reject_financial_period_for_incompatible_selection_key,
    reject_financial_period_for_strict_pit,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialEligibilityMetadataBinding,
    FinancialListingMetadata,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2,
    MainlandFinancialEligibilityProjectionBinding,
    MainlandFinancialPeriodTargetConstruction,
)
from tradingagents.dataflows.financial_period_targets import (
    construct_mainland_financial_period_targets,
    filter_mainland_financial_acquisition_periods,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestAttemptEvent,
)
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    InstrumentIdentityEvidence,
    InstrumentKind,
)

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_STATEMENT_CAPABILITY = {
    FinancialStatementType.BALANCE_SHEET: MainlandCapability.BALANCE_SHEET,
    FinancialStatementType.INCOME_STATEMENT: MainlandCapability.INCOME_STATEMENT,
    FinancialStatementType.CASH_FLOW: MainlandCapability.CASH_FLOW,
}
_ROUTABLE_DATASET_CAPABILITIES = frozenset(
    {
        MainlandCapability.ADJUSTMENT_FACTORS,
        MainlandCapability.SUSPENSION_STATUS,
        MainlandCapability.NAME_EVENTS,
        MainlandCapability.ISSUER_LIFECYCLE,
    }
)
_ROUTE_PROVIDER_ARTIFACT_PROVIDER = {
    "baostock_qualified_families": "baostock",
}
_QUALIFIED_PUBLICATION_PROVIDERS = {
    FinancialCapability.STATEMENT: frozenset({"akshare_sina", "tushare"}),
    FinancialCapability.RATIO_FAMILY: frozenset(
        {"akshare", "baostock", "tushare"}
    ),
}

_INDICATOR_PROVIDER_ASSESSMENT_CONTRACT = {
    "tushare": (FINANCIAL_RATIO_DECLARATION_V2, "XXX"),
    "baostock": (FINANCIAL_RATIO_DECLARATION_BAOSTOCK_V1, "CNY"),
}


def _v2_candidate_group_key(candidate: FinancialPeriodCandidate) -> tuple[object, ...]:
    identity = candidate.period_identity
    instrument = identity.instrument_identity
    return (
        instrument.symbol,
        instrument.venue,
        instrument.instrument_kind.value,
        instrument.currency,
        identity.company_type.value,
        identity.capability.value,
        identity.statement_type.value if identity.statement_type is not None else "",
        identity.ratio_family.value if identity.ratio_family is not None else "",
        identity.period_end,
        identity.frequency.value,
        identity.consolidation_scope.value,
        identity.currency,
        tuple(
            sorted(
                {
                    field.normalized_unit or ""
                    for field in candidate.fields
                    if field.normalized_value is not None
                }
            )
        ),
    )


def _ordered_v2_response_candidates(
    candidates: Sequence[FinancialPeriodCandidate],
) -> tuple[FinancialPeriodCandidate, ...]:
    """Canonicalize response rows before any first-winner state is mutated."""

    grouped: dict[tuple[object, ...], list[FinancialPeriodCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(_v2_candidate_group_key(candidate), []).append(candidate)
    ordered: list[FinancialPeriodCandidate] = []
    for group_key in sorted(grouped):
        group = grouped[group_key]
        qualified_sequence = financial_provider_revision_sequence_is_qualified(
            group
        )
        if qualified_sequence:
            group.sort(
                key=lambda item: (
                    -int(item.filing_metadata.provider_filing_revision_sequence),
                    item.filing_metadata.local_provider_revision_identity,
                )
            )
        else:
            group.sort(
                key=lambda item: (
                    -(
                        item.filing_metadata.ann_date.toordinal()
                        if item.filing_metadata.ann_date is not None
                        else -1
                    ),
                    -max(
                        item.artifact.observed_at,
                        item.filing_metadata.observed_at,
                    ).timestamp(),
                    -item.filing_metadata.retrieved_at.timestamp(),
                    item.filing_metadata.local_provider_revision_identity,
                )
            )
        ordered.extend(group)
    return tuple(ordered)


def _qualified_v2_revision_candidate_identities(
    candidates: Sequence[FinancialPeriodCandidate],
) -> frozenset[str]:
    grouped: dict[tuple[object, ...], list[FinancialPeriodCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(_v2_candidate_group_key(candidate), []).append(candidate)
    return frozenset(
        candidate.candidate_identity
        for group in grouped.values()
        if financial_provider_revision_sequence_is_qualified(group)
        for candidate in group
    )


def _v2_revision_selections(
    *,
    provider_candidates: Mapping[str, FinancialProviderCandidate],
    selected_candidates: Sequence[FinancialPeriodCandidate],
    conflicted_candidate_identities: set[str],
    route: Sequence[str],
    requested_analysis_scope: FinancialAvailabilityScope | None,
) -> tuple[FinancialRevisionSelection, ...]:
    route_ranks = {
        _ROUTE_PROVIDER_ARTIFACT_PROVIDER.get(provider_id, provider_id): rank
        for rank, provider_id in enumerate(route)
    }
    eligible_or_conflicted = tuple(
        item
        for item in provider_candidates.values()
        if item.assessment.disposition
        in {
            FinancialPeriodDisposition.ACCEPTED,
            FinancialPeriodDisposition.CURRENT_ONLY,
        }
        or item.candidate.candidate_identity in conflicted_candidate_identities
    )
    by_key: dict[str, list[FinancialProviderCandidate]] = {}
    for item in eligible_or_conflicted:
        by_key.setdefault(financial_revision_selection_key(item), []).append(item)
    revision_selections = []
    for selected in selected_candidates:
        selected_binding = provider_candidates[selected.candidate_identity]
        key = financial_revision_selection_key(selected_binding)
        group = tuple(by_key[key])
        revision_selections.append(
            create_financial_revision_selection(
                provider_candidates=group,
                selected_candidate_identity=selected.candidate_identity,
                provider_route_ranks=route_ranks,
                conflicted=any(
                    item.candidate.candidate_identity
                    in conflicted_candidate_identities
                    for item in group
                ),
                requested_analysis_scope=requested_analysis_scope,
                eligibility_policy_version=MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2,
            )
        )
    return tuple(
        sorted(revision_selections, key=lambda item: item.selection_identity)
    )


def _qualified_publication_provider_ids(
    route: Sequence[str],
    *,
    capability: FinancialCapability,
) -> tuple[str, ...]:
    qualified = _QUALIFIED_PUBLICATION_PROVIDERS[capability]
    artifact_providers = tuple(
        _ROUTE_PROVIDER_ARTIFACT_PROVIDER.get(provider_id, provider_id)
        for provider_id in route
    )
    return tuple(
        provider_id for provider_id in artifact_providers if provider_id in qualified
    )


def _rendered_sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class FinancialProviderResponseKind(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class FinancialCapabilityProviderResponseKind(str, Enum):
    AVAILABLE = "available"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class FinancialSubrequestAcquisitionOutcome(BaseModel):
    """One exact endpoint/family-scoped outcome retained by Ticket 10 routing."""

    model_config = _CLOSED_MODEL_CONFIG

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    subrequest_key: str = Field(min_length=1, max_length=256)
    outcome: Literal["available", "unavailable"]
    artifact_identity: str | None = Field(
        default=None,
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$",
    )
    reason: AcquisitionUnavailableReason | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> FinancialSubrequestAcquisitionOutcome:
        if self.outcome == "available":
            if self.artifact_identity is None or self.reason is not None:
                raise ValueError("available subrequest requires only an artifact")
        elif self.artifact_identity is not None or self.reason is None:
            raise ValueError("unavailable subrequest requires only a typed reason")
        return self

    def manifest_outcome(
        self,
        capability: FinancialCapability,
    ) -> FinancialAcquisitionOutcome:
        if self.outcome == "available":
            assert self.artifact_identity is not None
            return FinancialAcquisitionOutcome.available(
                provider_id=self.provider_id,
                capability=capability,
                artifact_identity=self.artifact_identity,
            )
        assert self.reason is not None
        return FinancialAcquisitionOutcome.unavailable(
            provider_id=self.provider_id,
            capability=capability,
            reason=self.reason,
        )


class FinancialAdapterRowRejection(BaseModel):
    """Provider-neutral safe projection of one adapter row disposition."""

    model_config = _CLOSED_MODEL_CONFIG

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    row_identity: str = Field(min_length=1, max_length=256)
    artifact_identity: str = Field(
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    candidate_identities: tuple[str, ...] = ()
    ratio_family: FinancialRatioFamily | None = None
    reasons: tuple[FinancialPeriodRejectionReason, ...]

    @model_validator(mode="after")
    def _validate_rejection(self) -> FinancialAdapterRowRejection:
        if self.candidate_identities != tuple(
            sorted(set(self.candidate_identities))
        ):
            raise ValueError("adapter rejection candidate identities must be canonical")
        canonical_reasons = tuple(
            reason for reason in FinancialPeriodRejectionReason if reason in self.reasons
        )
        if not canonical_reasons or self.reasons != canonical_reasons:
            raise ValueError("adapter rejection reasons must be canonical")
        return self


class FinancialProviderPeriodResponse(BaseModel):
    """Payload-free common projection of a Ticket 04-09 adapter result."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-provider-period-response-v1"] = (
        "financial-provider-period-response-v1"
    )
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: FinancialProviderResponseKind
    response_artifact: FinancialProviderArtifactIdentity | None = None
    retained_artifacts: tuple[FinancialProviderArtifactIdentity, ...] = ()
    candidates: tuple[FinancialPeriodCandidate, ...] = ()
    reason: AcquisitionUnavailableReason | None = None
    subrequest_keys: tuple[str, ...] = ()
    physical_attempt_ids: tuple[str, ...] = ()
    physical_attempt_events: tuple[ProviderSubrequestAttemptEvent, ...] = ()
    degraded_current_profile: bool = False
    subrequest_outcomes: tuple[FinancialSubrequestAcquisitionOutcome, ...] = ()
    adapter_row_rejections: tuple[FinancialAdapterRowRejection, ...] = ()

    @classmethod
    def available(
        cls,
        *,
        provider_id: str,
        response_artifact: FinancialProviderArtifactIdentity,
        retained_artifacts: Sequence[FinancialProviderArtifactIdentity],
        candidates: Sequence[FinancialPeriodCandidate],
        subrequest_keys: Sequence[str],
        physical_attempt_ids: Sequence[str],
        physical_attempt_events: Sequence[ProviderSubrequestAttemptEvent] = (),
        degraded_current_profile: bool = False,
        subrequest_outcomes: Sequence[FinancialSubrequestAcquisitionOutcome] = (),
        adapter_row_rejections: Sequence[FinancialAdapterRowRejection] = (),
    ) -> FinancialProviderPeriodResponse:
        return cls(
            provider_id=provider_id,
            kind=FinancialProviderResponseKind.AVAILABLE,
            response_artifact=response_artifact,
            retained_artifacts=tuple(retained_artifacts),
            candidates=tuple(candidates),
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
            physical_attempt_events=tuple(physical_attempt_events),
            degraded_current_profile=degraded_current_profile,
            subrequest_outcomes=tuple(subrequest_outcomes),
            adapter_row_rejections=tuple(adapter_row_rejections),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        provider_id: str,
        reason: AcquisitionUnavailableReason | str,
        subrequest_keys: Sequence[str] = (),
        physical_attempt_ids: Sequence[str] = (),
        physical_attempt_events: Sequence[ProviderSubrequestAttemptEvent] = (),
        subrequest_outcomes: Sequence[FinancialSubrequestAcquisitionOutcome] = (),
    ) -> FinancialProviderPeriodResponse:
        return cls(
            provider_id=provider_id,
            kind=FinancialProviderResponseKind.UNAVAILABLE,
            reason=AcquisitionUnavailableReason(reason),
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
            physical_attempt_events=tuple(physical_attempt_events),
            subrequest_outcomes=tuple(subrequest_outcomes),
        )

    @model_validator(mode="after")
    def _validate_response(self) -> FinancialProviderPeriodResponse:
        if len(self.subrequest_keys) != len(set(self.subrequest_keys)):
            raise ValueError("provider response subrequest keys must be unique")
        if len(self.physical_attempt_ids) != len(set(self.physical_attempt_ids)):
            raise ValueError("provider response physical attempts must be unique")
        attempt_event_ids = tuple(
            item.attempt_event_id for item in self.physical_attempt_events
        )
        if self.physical_attempt_events and (
            len(attempt_event_ids) != len(set(attempt_event_ids))
            or set(attempt_event_ids) != set(self.physical_attempt_ids)
            or any(
                item.request_key not in set(self.subrequest_keys)
                for item in self.physical_attempt_events
            )
        ):
            raise ValueError("provider response physical attempt events are invalid")
        if any(
            item.provider_id != self.provider_id
            or item.subrequest_key not in set(self.subrequest_keys)
            for item in self.subrequest_outcomes
        ):
            raise ValueError("provider response subrequest outcome binding is invalid")
        artifact_ids = tuple(item.artifact_identity for item in self.retained_artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("provider response artifacts must be unique")
        if any(
            item.provider_id != self.provider_id
            or item.artifact_identity not in set(artifact_ids)
            for item in self.adapter_row_rejections
        ):
            raise ValueError("provider response adapter rejection binding is invalid")
        if any(
            item.artifact_identity is not None
            and item.artifact_identity not in set(artifact_ids)
            for item in self.subrequest_outcomes
        ):
            raise ValueError("provider response subrequest artifact was not retained")
        if self.kind is FinancialProviderResponseKind.AVAILABLE:
            if self.response_artifact is None or self.reason is not None:
                raise ValueError("available provider response requires only an artifact")
            if self.response_artifact.artifact_identity not in set(artifact_ids):
                raise ValueError("provider response artifact must be retained")
            if any(
                candidate.artifact.artifact_identity not in set(artifact_ids)
                for candidate in self.candidates
            ):
                raise ValueError("provider response candidate artifact must be retained")
            artifact_provider_id = _ROUTE_PROVIDER_ARTIFACT_PROVIDER.get(
                self.provider_id,
                self.provider_id,
            )
            if any(
                candidate.artifact.dataset.provider_id != artifact_provider_id
                for candidate in self.candidates
            ):
                raise ValueError("provider response candidate has another provider")
            if self.degraded_current_profile and (
                self.provider_id != "yfinance" or self.candidates
            ):
                raise ValueError(
                    "degraded current profile must be a candidate-free Yahoo response"
                )
        elif (
            self.response_artifact is not None
            or self.retained_artifacts
            or self.candidates
            or self.reason is None
            or self.degraded_current_profile
            or self.adapter_row_rejections
        ):
            raise ValueError("unavailable provider response cannot carry artifacts")
        return self


class FinancialProviderGapRequest(BaseModel):
    """Exact unresolved target projection supplied to one sequential provider."""

    model_config = _CLOSED_MODEL_CONFIG

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    instrument_identity: InstrumentIdentityEvidence
    statement_type: FinancialStatementType
    as_of_date: date
    missing_annual_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    conflicted_annual_period_ends: tuple[date, ...] = ()
    conflicted_reporting_period_ends: tuple[date, ...] = ()


FinancialPeriodSource = Callable[
    [FinancialProviderGapRequest],
    FinancialProviderPeriodResponse,
]


class FinancialIndicatorProviderGapRequest(BaseModel):
    """Exact unresolved ratio family/period projection for one provider."""

    model_config = _CLOSED_MODEL_CONFIG

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date
    company_type: FinancialCompanyType
    company_type_resolution: FinancialCompanyTypeResolution | None = None
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    missing_periods_by_family: tuple[
        tuple[FinancialRatioFamily, tuple[date, ...]],
        ...,
    ]

    @model_validator(mode="after")
    def _validate_gaps(self) -> FinancialIndicatorProviderGapRequest:
        if (
            self.company_type_resolution is not None
            and self.company_type_resolution.company_type is not self.company_type
        ):
            raise ValueError("indicator gap company-type resolution is inconsistent")
        family_order = tuple(FinancialRatioFamily)
        actual = tuple(family for family, _ in self.missing_periods_by_family)
        if actual != tuple(family for family in family_order if family in actual):
            raise ValueError("indicator gap families must be unique and canonical")
        if any(
            periods != tuple(sorted(set(periods), reverse=True)) or not periods
            for _, periods in self.missing_periods_by_family
        ):
            raise ValueError("indicator gap periods must be non-empty and canonical")
        return self


FinancialIndicatorSource = Callable[
    [FinancialIndicatorProviderGapRequest],
    FinancialProviderPeriodResponse,
]


class FinancialStatementRoutingRequest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-statement-routing-request-v1",
        "financial-statement-routing-request-v2",
    ] = (
        "financial-statement-routing-request-v1"
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    instrument_identity: InstrumentIdentityEvidence
    statement_type: FinancialStatementType
    as_of_date: date
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    eligible_annual_period_ends: tuple[date, ...]
    eligible_reporting_period_ends: tuple[date, ...]
    listing_date: date | None = None
    listing_provenance: FinancialListingProvenance | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> FinancialStatementRoutingRequest:
        if self.statement_type not in _STATEMENT_CAPABILITY:
            raise ValueError("statement routing requires one full statement")
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("statement routing requires a resolved company type")
        if (self.listing_date is None) != (self.listing_provenance is None):
            raise ValueError("listing exception requires authoritative date and provenance")
        v2 = self.contract_version == "financial-statement-routing-request-v2"
        if v2 != (
            self.eligibility_policy_version
            == MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
        ):
            raise ValueError("statement routing request policy identity is inconsistent")
        if self.financial_eligibility_binding is not None and (
            not v2
            or self.financial_eligibility_binding.eligibility_policy_version
            != self.eligibility_policy_version
        ):
            raise ValueError("statement routing eligibility binding is inconsistent")
        _validate_mainland_financial_identity(
            self.instrument_identity,
            currency=self.currency,
        )
        return self


class FinancialIndicatorRoutingRequest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-indicator-routing-request-v1",
        "financial-indicator-routing-request-v2",
        "financial-indicator-routing-request-v3",
        "financial-indicator-routing-request-v4",
        "financial-indicator-routing-request-v5",
    ] = (
        "financial-indicator-routing-request-v1"
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    requested_analysis_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date
    company_type: FinancialCompanyType
    company_type_resolution: FinancialCompanyTypeResolution | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    ratio_families: tuple[FinancialRatioFamily, ...]
    eligible_reporting_period_ends: tuple[date, ...]
    remaining_evidence_decision_ready: bool = False
    family_evidence_requirements: FinancialIndicatorEvidenceRequirements | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    listing_date: date | None = Field(default=None, exclude_if=lambda value: value is None)
    listing_provenance: FinancialListingProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_request(self) -> FinancialIndicatorRoutingRequest:
        canonical = tuple(
            family for family in FinancialRatioFamily if family in self.ratio_families
        )
        family_bound = self.contract_version in {
            "financial-indicator-routing-request-v4",
            "financial-indicator-routing-request-v5",
        }
        provenance_bound = (
            self.contract_version == "financial-indicator-routing-request-v5"
        )
        if not canonical or len(self.ratio_families) != len(canonical):
            raise ValueError("indicator families must be non-empty, unique, and canonical")
        if self.ratio_families != canonical:
            if not family_bound:
                raise ValueError(
                    "indicator families must be non-empty, unique, and canonical"
                )
            object.__setattr__(self, "ratio_families", canonical)
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("indicator routing requires a resolved company type")
        if provenance_bound != (self.company_type_resolution is not None):
            raise ValueError("indicator company-type provenance is inconsistent")
        if (
            self.company_type_resolution is not None
            and self.company_type_resolution.company_type is not self.company_type
        ):
            raise ValueError("indicator company-type resolution is inconsistent")
        if (self.listing_date is None) != (self.listing_provenance is None):
            raise ValueError("listing exception requires authoritative date and provenance")
        policy_bound = self.contract_version in {
            "financial-indicator-routing-request-v2",
            "financial-indicator-routing-request-v3",
            "financial-indicator-routing-request-v4",
            "financial-indicator-routing-request-v5",
        }
        if policy_bound != (
            self.eligibility_policy_version
            == MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
        ):
            raise ValueError("indicator routing request policy identity is inconsistent")
        if self.financial_eligibility_binding is not None and (
            not policy_bound
            or self.financial_eligibility_binding.eligibility_policy_version
            != self.eligibility_policy_version
        ):
            raise ValueError("indicator routing eligibility binding is inconsistent")
        availability_bound = (
            self.contract_version
            in {
                "financial-indicator-routing-request-v3",
                "financial-indicator-routing-request-v4",
                "financial-indicator-routing-request-v5",
            }
        )
        if availability_bound and self.requested_analysis_scope is None:
            raise ValueError(
                "Ticket 03 routing requires an explicit availability scope"
            )
        if not availability_bound and self.requested_analysis_scope is not None:
            raise ValueError("indicator routing request availability scope is inconsistent")
        if family_bound != (self.family_evidence_requirements is not None):
            raise ValueError("Ticket 04 routing requires family evidence requirements")
        if family_bound:
            assert self.family_evidence_requirements is not None
            if (
                self.family_evidence_requirements.requested_families
                != self.ratio_families
                or self.family_evidence_requirements.eligibility_policy_version
                != self.eligibility_policy_version
                or self.remaining_evidence_decision_ready
            ):
                raise ValueError("indicator family requirement binding is inconsistent")
        _validate_mainland_financial_identity(
            self.instrument_identity,
            currency=self.currency,
        )
        return self


class FinancialCapabilityRoutingRequest(BaseModel):
    """One bounded non-statement capability request at the market-owner seam."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-capability-routing-request-v1"] = (
        "financial-capability-routing-request-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    capability: MainlandCapability
    range_start: date
    as_of_date: date

    @model_validator(mode="after")
    def _validate_request(self) -> FinancialCapabilityRoutingRequest:
        if self.capability not in _ROUTABLE_DATASET_CAPABILITIES:
            raise ValueError("dataset routing requires a non-statement capability")
        if self.range_start > self.as_of_date:
            raise ValueError("capability routing range must not end before it starts")
        _validate_mainland_financial_identity(
            self.instrument_identity,
            currency=self.instrument_identity.currency,
        )
        return self


class FinancialCompanyTypeEvidenceRoutingRequest(BaseModel):
    """Pre-company-type request bound to the active eligibility acquisition."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-company-type-evidence-routing-request-v1"] = (
        "financial-company-type-evidence-routing-request-v1"
    )
    metadata_binding: FinancialEligibilityMetadataBinding


class FinancialCompanyTypeEvidenceProviderResponseKind(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class FinancialCompanyTypeEvidenceSourceProjectionKind(str, Enum):
    """Qualified provider-neutral source projection accepted at the boundary."""

    TUSHARE_STATEMENT = "tushare_statement"
    AKSHARE_SINA_STATEMENT = "akshare_sina_statement"
    YFINANCE_CURRENT_PROFILE = "yfinance_current_profile"


class FinancialCompanyTypeEvidenceProviderResponse(BaseModel):
    """Safe provider-neutral company-type projection from one plan source."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-company-type-evidence-provider-response-v1"
    ] = "financial-company-type-evidence-provider-response-v1"
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    metadata_binding: FinancialEligibilityMetadataBinding
    kind: FinancialCompanyTypeEvidenceProviderResponseKind
    source_projection_kind: FinancialCompanyTypeEvidenceSourceProjectionKind | None = (
        None
    )
    reason: AcquisitionUnavailableReason | None = None
    evidence: FinancialCompanyTypeAcquisitionEvidenceBinding | None = None
    subrequest_keys: tuple[str, ...] = ()
    physical_attempt_ids: tuple[str, ...] = ()

    @classmethod
    def available(
        cls,
        *,
        provider_id: str,
        metadata_binding: FinancialEligibilityMetadataBinding,
        source_projection_kind: FinancialCompanyTypeEvidenceSourceProjectionKind | str,
        evidence: FinancialCompanyTypeAcquisitionEvidenceBinding,
        subrequest_keys: Sequence[str] = (),
        physical_attempt_ids: Sequence[str] = (),
    ) -> FinancialCompanyTypeEvidenceProviderResponse:
        return cls(
            provider_id=provider_id,
            metadata_binding=metadata_binding,
            kind=FinancialCompanyTypeEvidenceProviderResponseKind.AVAILABLE,
            source_projection_kind=FinancialCompanyTypeEvidenceSourceProjectionKind(
                source_projection_kind
            ),
            evidence=evidence,
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        provider_id: str,
        metadata_binding: FinancialEligibilityMetadataBinding,
        reason: AcquisitionUnavailableReason | str,
        subrequest_keys: Sequence[str] = (),
        physical_attempt_ids: Sequence[str] = (),
    ) -> FinancialCompanyTypeEvidenceProviderResponse:
        return cls(
            provider_id=provider_id,
            metadata_binding=metadata_binding,
            kind=FinancialCompanyTypeEvidenceProviderResponseKind.UNAVAILABLE,
            reason=AcquisitionUnavailableReason(reason),
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
        )

    @model_validator(mode="after")
    def _validate_response(self) -> FinancialCompanyTypeEvidenceProviderResponse:
        for values in (self.subrequest_keys, self.physical_attempt_ids):
            if len(values) != len(set(values)) or any(
                not value.strip() for value in values
            ):
                raise ValueError("company-type source identities are invalid")
        if self.kind is FinancialCompanyTypeEvidenceProviderResponseKind.AVAILABLE:
            expected_projection_kind = {
                "tushare": FinancialCompanyTypeEvidenceSourceProjectionKind.TUSHARE_STATEMENT,
                "akshare_sina": FinancialCompanyTypeEvidenceSourceProjectionKind.AKSHARE_SINA_STATEMENT,
                "yfinance": FinancialCompanyTypeEvidenceSourceProjectionKind.YFINANCE_CURRENT_PROFILE,
            }.get(self.provider_id)
            if (
                self.reason is not None
                or self.evidence is None
                or self.evidence.provider_id != self.provider_id
                or self.source_projection_kind is not expected_projection_kind
            ):
                raise ValueError("available company-type response is inconsistent")
            if (
                self.evidence.instrument_identity
                != self.metadata_binding.instrument_identity
                or self.evidence.as_of_date != self.metadata_binding.as_of_date
            ):
                raise ValueError("company-type evidence acquisition binding mismatch")
            if self.provider_id == "yfinance" and (
                self.evidence.provider_declared_type is not None
                or self.evidence.provider_declaration_qualified
            ):
                raise ValueError("Yahoo company-type evidence is classifier-only")
        elif (
            self.reason is None
            or self.evidence is not None
            or self.source_projection_kind is not None
        ):
            raise ValueError("unavailable company-type response carries evidence")
        return self


FinancialCompanyTypeEvidenceSource = Callable[
    [FinancialCompanyTypeEvidenceRoutingRequest],
    FinancialCompanyTypeEvidenceProviderResponse,
]


class FinancialCompanyTypeEvidenceRoutingResult(BaseModel):
    """Terminal sequential traversal of the plan prerequisite route."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-company-type-evidence-routing-result-v1"] = (
        "financial-company-type-evidence-routing-result-v1"
    )
    route: tuple[str, ...]
    provider_calls: tuple[FinancialCompanyTypeEvidenceProviderResponse, ...]
    selected_provider: str | None = None
    selected_evidence: FinancialCompanyTypeAcquisitionEvidenceBinding | None = None
    subrequest_keys: tuple[str, ...] = ()
    physical_attempt_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_result(self) -> FinancialCompanyTypeEvidenceRoutingResult:
        called = tuple(item.provider_id for item in self.provider_calls)
        if called != self.route[: len(called)]:
            raise ValueError("company-type route calls do not follow the plan")
        expected_subrequest_keys = tuple(
            identity
            for item in self.provider_calls
            for identity in item.subrequest_keys
        )
        expected_physical_attempt_ids = tuple(
            identity
            for item in self.provider_calls
            for identity in item.physical_attempt_ids
        )
        if (
            len(expected_subrequest_keys) != len(set(expected_subrequest_keys))
            or len(expected_physical_attempt_ids)
            != len(set(expected_physical_attempt_ids))
        ):
            raise ValueError("company-type route contains duplicate lineage")
        if (
            self.subrequest_keys != expected_subrequest_keys
            or self.physical_attempt_ids != expected_physical_attempt_ids
        ):
            raise ValueError("company-type route lineage does not match provider calls")
        selected = next(
            (
                item
                for item in self.provider_calls
                if item.kind
                is FinancialCompanyTypeEvidenceProviderResponseKind.AVAILABLE
            ),
            None,
        )
        if (selected is None) != (self.selected_provider is None) or (
            selected is None
        ) != (self.selected_evidence is None):
            raise ValueError("company-type route selection is incomplete")
        if selected is not None and (
            selected.provider_id != self.selected_provider
            or selected.evidence != self.selected_evidence
            or selected is not self.provider_calls[-1]
        ):
            raise ValueError("company-type route selection contradicts its source")
        return self


class FinancialCapabilityProviderResponse(BaseModel):
    """Payload-free result from one factor/status/name/lifecycle adapter."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-capability-provider-response-v1"] = (
        "financial-capability-provider-response-v1"
    )
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capability: MainlandCapability
    kind: FinancialCapabilityProviderResponseKind
    reason: AcquisitionUnavailableReason | None = None
    retained_artifact_identities: tuple[str, ...] = ()
    candidate_count: int = Field(default=0, ge=0)
    subrequest_keys: tuple[str, ...] = ()
    physical_attempt_ids: tuple[str, ...] = ()
    provider_history_bundle_identity: str | None = Field(
        default=None,
        pattern=r"^baostock-provider-history-bundle:v1:[0-9a-f]{64}$",
    )
    strict_bundle_component_artifact_identities: tuple[str, ...] = ()
    current_tradeability: Literal["unknown", "tradeable", "suspended"] = "unknown"
    listing_metadata: FinancialListingMetadata | None = None
    derived: bool = False
    degraded: bool = False

    @classmethod
    def available(
        cls,
        *,
        provider_id: str,
        capability: MainlandCapability,
        retained_artifact_identities: Sequence[str],
        candidate_count: int,
        subrequest_keys: Sequence[str],
        physical_attempt_ids: Sequence[str],
        provider_history_bundle_identity: str | None = None,
        strict_bundle_component_artifact_identities: Sequence[str] = (),
        current_tradeability: Literal[
            "unknown",
            "tradeable",
            "suspended",
        ] = "unknown",
        listing_metadata: FinancialListingMetadata | None = None,
        derived: bool = False,
        degraded: bool = False,
    ) -> FinancialCapabilityProviderResponse:
        return cls(
            provider_id=provider_id,
            capability=capability,
            kind=FinancialCapabilityProviderResponseKind.AVAILABLE,
            retained_artifact_identities=tuple(retained_artifact_identities),
            candidate_count=candidate_count,
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
            provider_history_bundle_identity=provider_history_bundle_identity,
            strict_bundle_component_artifact_identities=tuple(
                strict_bundle_component_artifact_identities
            ),
            current_tradeability=current_tradeability,
            listing_metadata=listing_metadata,
            derived=derived,
            degraded=degraded,
        )

    @classmethod
    def rejected(
        cls,
        *,
        provider_id: str,
        capability: MainlandCapability,
        reason: AcquisitionUnavailableReason | str,
        retained_artifact_identities: Sequence[str],
        subrequest_keys: Sequence[str],
        physical_attempt_ids: Sequence[str],
    ) -> FinancialCapabilityProviderResponse:
        return cls(
            provider_id=provider_id,
            capability=capability,
            kind=FinancialCapabilityProviderResponseKind.REJECTED,
            reason=AcquisitionUnavailableReason(reason),
            retained_artifact_identities=tuple(retained_artifact_identities),
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        provider_id: str,
        capability: MainlandCapability,
        reason: AcquisitionUnavailableReason | str,
        subrequest_keys: Sequence[str] = (),
        physical_attempt_ids: Sequence[str] = (),
    ) -> FinancialCapabilityProviderResponse:
        return cls(
            provider_id=provider_id,
            capability=capability,
            kind=FinancialCapabilityProviderResponseKind.UNAVAILABLE,
            reason=AcquisitionUnavailableReason(reason),
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
        )

    @model_validator(mode="after")
    def _validate_response(self) -> FinancialCapabilityProviderResponse:
        if self.capability not in _ROUTABLE_DATASET_CAPABILITIES:
            raise ValueError("provider response has an unsupported capability")
        for label, values in (
            ("artifact", self.retained_artifact_identities),
            ("subrequest", self.subrequest_keys),
            ("attempt", self.physical_attempt_ids),
            (
                "strict bundle component artifact",
                self.strict_bundle_component_artifact_identities,
            ),
        ):
            if len(values) != len(set(values)) or any(
                not value.strip() for value in values
            ):
                raise ValueError(f"capability response {label} identities are invalid")
        if self.kind is FinancialCapabilityProviderResponseKind.AVAILABLE:
            has_usable_value = (
                self.candidate_count > 0 or self.current_tradeability != "unknown"
            )
            if (
                self.reason is not None
                or not self.retained_artifact_identities
                or not has_usable_value
            ):
                raise ValueError(
                    "available capability response requires retained usable data"
                )
        elif self.kind is FinancialCapabilityProviderResponseKind.REJECTED:
            if (
                self.reason is None
                or not self.retained_artifact_identities
                or self.candidate_count
                or self.current_tradeability != "unknown"
                or self.derived
                or self.degraded
                or self.listing_metadata is not None
            ):
                raise ValueError(
                    "rejected capability response must retain only unusable artifacts"
                )
        elif (
            self.reason is None
            or self.retained_artifact_identities
            or self.candidate_count
            or self.current_tradeability != "unknown"
            or self.derived
            or self.degraded
            or self.listing_metadata is not None
        ):
            raise ValueError("unavailable capability response carries usable data")
        bundle_binding_present = (
            self.provider_history_bundle_identity is not None
            or bool(self.strict_bundle_component_artifact_identities)
        )
        if bundle_binding_present and (
            self.kind is not FinancialCapabilityProviderResponseKind.AVAILABLE
            or self.capability is not MainlandCapability.ADJUSTMENT_FACTORS
            or self.provider_id != "baostock"
            or self.provider_history_bundle_identity is None
            or len(self.strict_bundle_component_artifact_identities) != 3
            or not set(self.strict_bundle_component_artifact_identities).issubset(
                self.retained_artifact_identities
            )
        ):
            raise ValueError(
                "strict history requires a complete BaoStock bundle binding"
            )
        if self.current_tradeability != "unknown" and (
            self.capability is not MainlandCapability.SUSPENSION_STATUS
            or self.provider_id != "baostock"
        ):
            raise ValueError("only BaoStock status can establish Current Tradeability")
        if self.listing_metadata is not None and (
            self.kind is not FinancialCapabilityProviderResponseKind.AVAILABLE
            or self.capability is not MainlandCapability.ISSUER_LIFECYCLE
            or self.provider_id != "baostock"
            or self.listing_metadata.listing_provenance.source_ref
            not in self.retained_artifact_identities
        ):
            raise ValueError("listing metadata is not BaoStock lifecycle-bound")
        if self.derived and (
            self.capability is not MainlandCapability.ADJUSTMENT_FACTORS
            or self.provider_id != "yfinance_derived"
            or not self.degraded
        ):
            raise ValueError("derived factor fallback must be degraded Yahoo data")
        return self


FinancialCapabilitySource = Callable[
    [FinancialCapabilityRoutingRequest],
    FinancialCapabilityProviderResponse,
]


class FinancialProviderRouteCall(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    provider_id: str
    missing_annual_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    conflicted_annual_period_ends: tuple[date, ...] = ()
    conflicted_reporting_period_ends: tuple[date, ...] = ()
    subrequest_keys: tuple[str, ...]
    physical_attempt_ids: tuple[str, ...]
    physical_attempt_events: tuple[ProviderSubrequestAttemptEvent, ...] = ()
    subrequest_outcomes: tuple[FinancialSubrequestAcquisitionOutcome, ...] = ()
    adapter_row_rejections: tuple[FinancialAdapterRowRejection, ...] = ()


class FinancialIndicatorProviderRouteCall(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    provider_id: str
    missing_periods_by_family: tuple[
        tuple[FinancialRatioFamily, tuple[date, ...]],
        ...,
    ]
    subrequest_keys: tuple[str, ...]
    physical_attempt_ids: tuple[str, ...]
    physical_attempt_events: tuple[ProviderSubrequestAttemptEvent, ...] = ()
    subrequest_outcomes: tuple[FinancialSubrequestAcquisitionOutcome, ...] = ()
    adapter_row_rejections: tuple[FinancialAdapterRowRejection, ...] = ()


class FinancialStatementRoutingResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-statement-routing-result-v1"] = (
        "financial-statement-routing-result-v1"
    )
    route: tuple[str, ...]
    provider_calls: tuple[FinancialProviderRouteCall, ...]
    manifest: FinancialAcquisitionManifest
    selected_candidates: tuple[FinancialPeriodCandidate, ...]
    missing_annual_period_ends: tuple[date, ...]
    missing_reporting_period_ends: tuple[date, ...]
    conflicted_annual_period_ends: tuple[date, ...]
    conflicted_reporting_period_ends: tuple[date, ...]
    physical_attempt_ids: tuple[str, ...]
    publication_target_construction: (
        MainlandFinancialPeriodTargetConstruction | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _validate_result(self) -> FinancialStatementRoutingResult:
        if self.manifest.contract_version == "financial-manifest-v1" and (
            self.manifest.publication_target_construction is not None
        ):
            raise ValueError("statement v1 manifest carries publication targets")
        if self.manifest.contract_version == "financial-manifest-v2" and (
            self.manifest.publication_target_construction
            != self.publication_target_construction
        ):
            raise ValueError("statement publication targets contradict manifest")
        if (
            self.manifest.contract_version == "financial-manifest-v2"
            and self.manifest.financial_eligibility_binding
            != self.financial_eligibility_binding
        ):
            raise ValueError("statement eligibility binding contradicts manifest")
        selection_ids = {item.candidate_identity for item in self.manifest.selections}
        if selection_ids != {
            item.candidate_identity for item in self.selected_candidates
        }:
            raise ValueError("statement routing candidates contradict manifest selections")
        if len(self.physical_attempt_ids) != len(set(self.physical_attempt_ids)):
            raise ValueError("statement routing attempts must be unique")
        manifest_attempt_ids = tuple(
            item.attempt_event_id
            for item in self.manifest.physical_attempt_events
        )
        if self.manifest.contract_version == "financial-manifest-v2" and (
            set(manifest_attempt_ids) != set(self.physical_attempt_ids)
            or self.manifest.final_rendered_artifact is None
            or self.manifest.final_rendered_artifact.content_sha256
            != _rendered_sha256(render_statement_routing_result(self))
        ):
            raise ValueError("statement routing manifest operational binding is invalid")
        return self


class FinancialIndicatorRoutingResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-indicator-routing-result-v1",
        "financial-indicator-routing-result-v2",
    ] = (
        "financial-indicator-routing-result-v1"
    )
    route: tuple[str, ...]
    provider_calls: tuple[FinancialIndicatorProviderRouteCall, ...]
    family_manifests: tuple[FinancialAcquisitionManifest, ...]
    selected_candidates: tuple[FinancialPeriodCandidate, ...]
    complete_families: tuple[FinancialRatioFamily, ...]
    missing_periods_by_family: tuple[
        tuple[FinancialRatioFamily, tuple[date, ...]],
        ...,
    ]
    retained_artifacts: tuple[FinancialProviderArtifactIdentity, ...]
    current_profile_degraded: bool = False
    current_profile_provider: Literal["yfinance"] | None = None
    physical_attempt_ids: tuple[str, ...]
    physical_attempt_events: tuple[ProviderSubrequestAttemptEvent, ...] = ()
    publication_target_construction: (
        MainlandFinancialPeriodTargetConstruction | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    requested_analysis_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    family_assessments: tuple[FinancialIndicatorFamilyAssessment, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    aggregate_family_assessment: FinancialIndicatorAggregateAssessment | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_result(self) -> FinancialIndicatorRoutingResult:
        family_bound = self.contract_version == "financial-indicator-routing-result-v2"
        if family_bound != (
            bool(self.family_assessments)
            and self.aggregate_family_assessment is not None
        ):
            raise ValueError("indicator family assessment binding is incomplete")
        if family_bound:
            assert self.aggregate_family_assessment is not None
            manifest_families = tuple(
                manifest.requested_ratio_family
                for manifest in self.family_manifests
            )
            if (
                self.aggregate_family_assessment.family_assessments
                != self.family_assessments
                or self.aggregate_family_assessment.complete_families
                != self.complete_families
                or manifest_families
                != self.aggregate_family_assessment.requested_families
                or self.aggregate_family_assessment.requested_analysis_scope
                is not self.requested_analysis_scope
                or self.aggregate_family_assessment.eligibility_policy_version
                != self.eligibility_policy_version
            ):
                raise ValueError("indicator aggregate family assessment is inconsistent")
            for manifest, assessment in zip(
                self.family_manifests,
                self.family_assessments,
                strict=True,
            ):
                if (
                    assessment.target_periods
                    != manifest.aggregate_completeness.target_reporting_period_ends
                    or assessment.candidate_identities
                    != tuple(
                        sorted(
                            item.candidate.candidate_identity
                            for item in manifest.provider_candidates
                        )
                    )
                    or assessment.artifact_identities
                    != tuple(
                        sorted(
                            item.artifact.artifact_identity
                            for item in manifest.artifacts
                        )
                    )
                    or assessment.selection_identities
                    != tuple(
                        sorted(
                            item.selection_identity for item in manifest.selections
                        )
                    )
                    or assessment.conflict_identities
                    != tuple(
                        sorted(
                            item.conflict_identity for item in manifest.conflicts
                        )
                    )
                ):
                    raise ValueError("indicator family manifest binding is inconsistent")
            publication_failed = (
                self.publication_target_construction is not None
                and self.publication_target_construction.failure_reason is not None
            )
            if publication_failed != (
                FinancialFamilyAssessmentReason.PUBLICATION_METADATA_UNPROVEN
                in self.aggregate_family_assessment.typed_reasons
            ):
                raise ValueError("indicator publication failure binding is inconsistent")
        if (self.requested_analysis_scope is None) != (
            self.eligibility_policy_version is None
        ):
            raise ValueError("indicator result analysis scope binding is incomplete")
        if self.financial_eligibility_binding is not None and (
            self.eligibility_policy_version
            != self.financial_eligibility_binding.eligibility_policy_version
        ):
            raise ValueError("indicator result eligibility binding is contradictory")
        manifest_target_constructions = {
            manifest.publication_target_construction
            for manifest in self.family_manifests
            if manifest.contract_version == "financial-manifest-v2"
        }
        if any(
            manifest.contract_version == "financial-manifest-v1"
            and manifest.publication_target_construction is not None
            for manifest in self.family_manifests
        ):
            raise ValueError("indicator v1 manifest carries publication targets")
        if manifest_target_constructions and manifest_target_constructions != {
            self.publication_target_construction
        }:
            raise ValueError("indicator publication targets contradict manifests")
        family_order = tuple(FinancialRatioFamily)
        if self.complete_families != tuple(
            family for family in family_order if family in self.complete_families
        ):
            raise ValueError("complete indicator families must be canonical")
        missing_families = tuple(
            family for family, _ in self.missing_periods_by_family
        )
        if missing_families != tuple(
            family for family in family_order if family in missing_families
        ):
            raise ValueError("missing indicator families must be canonical")
        if len(self.physical_attempt_ids) != len(set(self.physical_attempt_ids)):
            raise ValueError("indicator routing attempts must be unique")
        event_ids = tuple(
            event.attempt_event_id for event in self.physical_attempt_events
        )
        if self.physical_attempt_events and set(event_ids) != set(
            self.physical_attempt_ids
        ):
            raise ValueError("indicator routing attempt events are inconsistent")
        if self.current_profile_degraded != (
            self.current_profile_provider == "yfinance"
        ):
            raise ValueError("indicator degraded profile binding is inconsistent")
        manifest_selection_ids = {
            selection.candidate_identity
            for manifest in self.family_manifests
            for selection in manifest.selections
        }
        if manifest_selection_ids != {
            candidate.candidate_identity for candidate in self.selected_candidates
        }:
            raise ValueError("indicator candidates contradict family manifests")
        v2_manifests = tuple(
            manifest
            for manifest in self.family_manifests
            if manifest.contract_version == "financial-manifest-v2"
        )
        if v2_manifests and {
            (
                manifest.requested_analysis_scope,
                manifest.eligibility_policy_version,
            )
            for manifest in v2_manifests
        } != {
            (
                self.requested_analysis_scope,
                self.eligibility_policy_version,
            )
        }:
            raise ValueError("indicator manifest analysis scope binding is invalid")
        if v2_manifests and any(
            manifest.financial_eligibility_binding
            != self.financial_eligibility_binding
            for manifest in v2_manifests
        ):
            raise ValueError("indicator manifest eligibility binding is invalid")
        if v2_manifests and (
            len(v2_manifests) != len(self.family_manifests)
            or any(
                manifest.final_rendered_artifact is None
                or manifest.final_rendered_artifact.content_sha256
                != _rendered_sha256(render_indicator_routing_result(self))
                or {
                    event.attempt_event_id
                    for event in manifest.physical_attempt_events
                }
                != set(self.physical_attempt_ids)
                for manifest in v2_manifests
            )
        ):
            raise ValueError("indicator manifest operational binding is invalid")
        return self


class FinancialCapabilityAuthorityPolicy(BaseModel):
    """Closed Ticket 10 authority/degradation policy for non-statement routes."""

    model_config = _CLOSED_MODEL_CONFIG

    capability: MainlandCapability
    providers: tuple[str, ...]
    authoritative_provider: str | None = None
    supplemental_providers: tuple[str, ...] = ()
    strict_bundle_provider: str | None = None
    non_strict_providers: tuple[str, ...] = ()
    degraded_providers: tuple[str, ...] = ()


class FinancialCapabilityRoutingResult(BaseModel):
    """Deterministic selection projection for a non-statement dataset."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-capability-routing-result-v1"] = (
        "financial-capability-routing-result-v1"
    )
    capability: MainlandCapability
    route: tuple[str, ...]
    provider_calls: tuple[FinancialCapabilityProviderResponse, ...]
    selected_provider: str | None = None
    authoritative_provider: str | None = None
    strict_bundle_provider: str | None = None
    strict_bundle_established: bool = False
    provider_history_bundle_identity: str | None = Field(
        default=None,
        pattern=r"^baostock-provider-history-bundle:v1:[0-9a-f]{64}$",
    )
    strict_bundle_component_artifact_identities: tuple[str, ...] = ()
    current_tradeability: Literal["unknown", "tradeable", "suspended"] = "unknown"
    listing_metadata: FinancialListingMetadata | None = None
    retained_artifact_identities: tuple[str, ...] = ()
    physical_attempt_ids: tuple[str, ...] = ()
    derived: bool = False
    degraded: bool = False
    status_or_lifecycle_fact_created: Literal[False] = False
    source_facts_created: Literal[False] = False

    @model_validator(mode="after")
    def _validate_result(self) -> FinancialCapabilityRoutingResult:
        if self.capability not in _ROUTABLE_DATASET_CAPABILITIES:
            raise ValueError("capability result has an unsupported capability")
        if self.selected_provider is not None and self.selected_provider not in self.route:
            raise ValueError("selected provider is outside the immutable route")
        if len(self.physical_attempt_ids) != len(set(self.physical_attempt_ids)):
            raise ValueError("capability routing attempts must be unique")
        if len(self.retained_artifact_identities) != len(
            set(self.retained_artifact_identities)
        ):
            raise ValueError("capability routing artifacts must be unique")
        if self.strict_bundle_established and (
            self.capability is not MainlandCapability.ADJUSTMENT_FACTORS
            or self.selected_provider != "baostock"
            or self.strict_bundle_provider != "baostock"
            or self.provider_history_bundle_identity is None
            or len(self.strict_bundle_component_artifact_identities) != 3
            or not set(self.strict_bundle_component_artifact_identities).issubset(
                self.retained_artifact_identities
            )
        ):
            raise ValueError("strict bundle result is not BaoStock-bound")
        if self.strict_bundle_established != (
            self.provider_history_bundle_identity is not None
        ) or (
            not self.strict_bundle_established
            and self.strict_bundle_component_artifact_identities
        ):
            raise ValueError("strict bundle result binding is incomplete")
        if self.current_tradeability != "unknown" and (
            self.capability is not MainlandCapability.SUSPENSION_STATUS
            or self.selected_provider != "baostock"
            or self.authoritative_provider != "baostock"
        ):
            raise ValueError("Current Tradeability result is not BaoStock-bound")
        selected = next(
            (
                item
                for item in self.provider_calls
                if item.kind is FinancialCapabilityProviderResponseKind.AVAILABLE
            ),
            None,
        )
        if self.listing_metadata is not None and (
            self.capability is not MainlandCapability.ISSUER_LIFECYCLE
            or self.selected_provider != "baostock"
            or self.authoritative_provider != "baostock"
            or selected is None
            or selected.listing_metadata != self.listing_metadata
        ):
            raise ValueError("listing result is not BaoStock lifecycle-bound")
        if selected is None and self.listing_metadata is not None:
            raise ValueError("unselected lifecycle route carries listing metadata")
        if self.derived and (
            self.selected_provider != "yfinance_derived" or not self.degraded
        ):
            raise ValueError("derived capability result must be degraded Yahoo data")
        return self


class MainlandFinancialCapabilityRouter:
    """Resolve the immutable plan and salvage statement periods sequentially."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        statement_sources: Mapping[str, FinancialPeriodSource],
        indicator_sources: Mapping[str, FinancialIndicatorSource] | None = None,
        capability_sources: (
            Mapping[
                MainlandCapability,
                Mapping[str, FinancialCapabilitySource],
            ]
            | None
        ) = None,
        company_type_evidence_sources: (
            Mapping[str, FinancialCompanyTypeEvidenceSource] | None
        ) = None,
    ) -> None:
        if routing_plan.mode not in {
            MainlandCapabilityRoutingMode.QUALIFIED_V1,
            MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW,
        }:
            raise ValueError("qualified financial router requires qualified_v1")
        self._routing_plan = routing_plan
        self._statement_sources = dict(statement_sources)
        self._indicator_sources = dict(indicator_sources or {})
        self._capability_sources = {
            capability: dict(sources)
            for capability, sources in (capability_sources or {}).items()
        }
        self._company_type_evidence_sources = dict(
            company_type_evidence_sources or {}
        )
        if any(
            capability not in _ROUTABLE_DATASET_CAPABILITIES
            for capability in self._capability_sources
        ):
            raise ValueError("capability source map contains an unsupported capability")

    @property
    def routing_plan(self) -> MainlandCapabilityRoutingPlan:
        return self._routing_plan

    def route_company_type_evidence(
        self,
        request: FinancialCompanyTypeEvidenceRoutingRequest,
        *,
        interrupted: Callable[[], bool] | None = None,
    ) -> FinancialCompanyTypeEvidenceRoutingResult:
        """Traverse only the prerequisite route declared by the immutable plan."""

        route = self._routing_plan.route_for(
            MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
        )
        provider_calls: list[FinancialCompanyTypeEvidenceProviderResponse] = []
        subrequest_keys: list[str] = []
        physical_attempt_ids: list[str] = []
        selected: FinancialCompanyTypeEvidenceProviderResponse | None = None
        for provider_id in route:
            if interrupted is not None and interrupted():
                break
            source = self._company_type_evidence_sources.get(provider_id)
            response = (
                FinancialCompanyTypeEvidenceProviderResponse.unavailable(
                    provider_id=provider_id,
                    metadata_binding=request.metadata_binding,
                    reason=AcquisitionUnavailableReason.NOT_CONFIGURED,
                )
                if source is None
                else source(request)
            )
            response = FinancialCompanyTypeEvidenceProviderResponse.model_validate(
                response.model_dump(mode="json")
            )
            if response.provider_id != provider_id:
                raise ValueError("company-type source returned another route step")
            if response.metadata_binding != request.metadata_binding:
                raise ValueError("company-type source request binding mismatch")
            provider_calls.append(response)
            subrequest_keys.extend(response.subrequest_keys)
            physical_attempt_ids.extend(response.physical_attempt_ids)
            if (
                response.kind
                is FinancialCompanyTypeEvidenceProviderResponseKind.AVAILABLE
            ):
                selected = response
                break
        return FinancialCompanyTypeEvidenceRoutingResult(
            route=route,
            provider_calls=tuple(provider_calls),
            selected_provider=selected.provider_id if selected is not None else None,
            selected_evidence=selected.evidence if selected is not None else None,
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
        )

    def capability_policy(
        self,
        capability: MainlandCapability,
    ) -> FinancialCapabilityAuthorityPolicy:
        providers = self._routing_plan.route_for(capability)
        if capability is MainlandCapability.ADJUSTMENT_FACTORS:
            return FinancialCapabilityAuthorityPolicy(
                capability=capability,
                providers=providers,
                authoritative_provider="baostock",
                strict_bundle_provider="baostock",
                non_strict_providers=tuple(
                    provider
                    for provider in providers
                    if provider != "baostock"
                ),
                degraded_providers=("yfinance_derived",),
            )
        if capability is MainlandCapability.SUSPENSION_STATUS:
            return FinancialCapabilityAuthorityPolicy(
                capability=capability,
                providers=providers,
                authoritative_provider="baostock",
                supplemental_providers=("tushare",),
            )
        if capability is MainlandCapability.ISSUER_LIFECYCLE:
            return FinancialCapabilityAuthorityPolicy(
                capability=capability,
                providers=providers,
                authoritative_provider="baostock",
            )
        return FinancialCapabilityAuthorityPolicy(
            capability=capability,
            providers=providers,
            degraded_providers=(
                ("yfinance",)
                if capability is MainlandCapability.FINANCIAL_INDICATORS
                else ()
            ),
        )

    def route_capability(
        self,
        request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityRoutingResult:
        """Select one complete dataset sequentially without crossing authority."""

        policy = self.capability_policy(request.capability)
        sources = self._capability_sources.get(request.capability, {})
        provider_calls: list[FinancialCapabilityProviderResponse] = []
        retained_artifacts: list[str] = []
        physical_attempt_ids: list[str] = []
        selected: FinancialCapabilityProviderResponse | None = None
        for provider_id in policy.providers:
            source = sources.get(provider_id)
            response = (
                FinancialCapabilityProviderResponse.unavailable(
                    provider_id=provider_id,
                    capability=request.capability,
                    reason=AcquisitionUnavailableReason.NOT_CONFIGURED,
                )
                if source is None
                else source(request)
            )
            response = FinancialCapabilityProviderResponse.model_validate(
                response.model_dump(mode="json")
            )
            if response.listing_metadata is not None and (
                response.listing_metadata.instrument_identity
                != request.instrument_identity
                or response.listing_metadata.as_of_date != request.as_of_date
            ):
                raise ValueError("lifecycle source request binding mismatch")
            if (
                response.provider_id != provider_id
                or response.capability is not request.capability
            ):
                raise ValueError("capability source returned another route step")
            provider_calls.append(response)
            for artifact_id in response.retained_artifact_identities:
                if artifact_id not in retained_artifacts:
                    retained_artifacts.append(artifact_id)
            for attempt_id in response.physical_attempt_ids:
                if attempt_id not in physical_attempt_ids:
                    physical_attempt_ids.append(attempt_id)
            if response.kind is FinancialCapabilityProviderResponseKind.AVAILABLE:
                selected = response
                break

        return FinancialCapabilityRoutingResult(
            capability=request.capability,
            route=policy.providers,
            provider_calls=tuple(provider_calls),
            selected_provider=(
                selected.provider_id if selected is not None else None
            ),
            authoritative_provider=policy.authoritative_provider,
            strict_bundle_provider=policy.strict_bundle_provider,
            strict_bundle_established=(
                selected.provider_history_bundle_identity is not None
                if selected is not None
                else False
            ),
            provider_history_bundle_identity=(
                selected.provider_history_bundle_identity
                if selected is not None
                else None
            ),
            strict_bundle_component_artifact_identities=(
                selected.strict_bundle_component_artifact_identities
                if selected is not None
                else ()
            ),
            current_tradeability=(
                selected.current_tradeability if selected is not None else "unknown"
            ),
            listing_metadata=(
                selected.listing_metadata if selected is not None else None
            ),
            retained_artifact_identities=tuple(retained_artifacts),
            physical_attempt_ids=tuple(physical_attempt_ids),
            derived=selected.derived if selected is not None else False,
            degraded=selected.degraded if selected is not None else False,
        )

    def route_statement(
        self,
        request: FinancialStatementRoutingRequest,
    ) -> FinancialStatementRoutingResult:
        capability = _STATEMENT_CAPABILITY[request.statement_type]
        route = self._routing_plan.route_for(capability)
        publication_aware = request.eligibility_policy_version is not None
        expected_eligibility_binding = (
            self._routing_plan.financial_eligibility_binding
            if publication_aware
            else None
        )
        eligibility_binding = request.financial_eligibility_binding
        if publication_aware and (
            self._routing_plan.eligibility_policy_version
            != request.eligibility_policy_version
            or self._routing_plan.financial_eligibility_policy is None
            or (
                request.financial_eligibility_binding is not None
                and request.financial_eligibility_binding
                != expected_eligibility_binding
            )
        ):
            raise ValueError("statement target construction policy mismatch")
        acquisition_annual = tuple(
            sorted(set(request.eligible_annual_period_ends), reverse=True)
        )
        acquisition_reporting = tuple(
            sorted(set(request.eligible_reporting_period_ends), reverse=True)
        )
        target_annual = (
            acquisition_annual if publication_aware else acquisition_annual[:5]
        )
        target_reporting = (
            acquisition_reporting if publication_aware else acquisition_reporting[:8]
        )
        target_keys = {
            *(("annual", item) for item in target_annual),
            *(("reporting", item) for item in target_reporting),
        }

        artifacts: dict[str, FinancialProviderArtifactEntry] = {}
        acquisition_outcomes: list[FinancialAcquisitionOutcome] = []
        provider_candidates: dict[str, FinancialProviderCandidate] = {}
        publication_candidates: dict[str, FinancialPeriodCandidate] = {}
        rejected_periods: dict[str, FinancialRejectedPeriod] = {}
        selections: dict[tuple[str, date], FinancialPeriodSelection] = {}
        selected_candidates: dict[tuple[str, date], FinancialPeriodCandidate] = {}
        selected_assessments: dict[
            tuple[str, date], FinancialPeriodCompletenessAssessment
        ] = {}
        overlaps: list[FinancialPeriodOverlapFinding] = []
        conflicts: list[FinancialCriticalValueConflict] = []
        conflicted_keys: set[tuple[str, date]] = set()
        conflicted_candidate_identities: set[str] = set()
        provider_calls: list[FinancialProviderRouteCall] = []
        physical_attempt_ids: list[str] = []
        physical_attempt_events: dict[str, ProviderSubrequestAttemptEvent] = {}
        qualified_publication_providers = _qualified_publication_provider_ids(
            route,
            capability=FinancialCapability.STATEMENT,
        )

        def publication_gap_targets() -> tuple[tuple[date, ...], tuple[date, ...]]:
            if not publication_aware:
                return target_annual, target_reporting
            assert self._routing_plan.financial_eligibility_policy is not None
            common = {
                "run_as_of_date": request.as_of_date,
                "expected_instrument_identity": request.instrument_identity,
                "expected_company_type": request.company_type,
                "expected_consolidation_scope": request.consolidation_scope,
                "candidates": tuple(publication_candidates.values()),
                "qualified_provider_ids": qualified_publication_providers,
                "listing_date": request.listing_date,
                "listing_provenance": request.listing_provenance,
                "policy": self._routing_plan.financial_eligibility_policy,
            }
            return (
                filter_mainland_financial_acquisition_periods(
                    period_ends=acquisition_annual,
                    **common,
                ),
                filter_mainland_financial_acquisition_periods(
                    period_ends=acquisition_reporting,
                    **common,
                ),
            )

        for provider_id in route:
            missing_keys = target_keys - set(selections)
            if not missing_keys:
                break
            gap_request = _statement_gap_request(
                provider_id=provider_id,
                request=request,
                target_annual=target_annual,
                target_reporting=target_reporting,
                selections=selections,
                conflicted_keys=conflicted_keys,
            )
            source = self._statement_sources.get(provider_id)
            if source is None:
                response = FinancialProviderPeriodResponse.unavailable(
                    provider_id=provider_id,
                    reason=AcquisitionUnavailableReason.NOT_CONFIGURED,
                )
            else:
                response = source(gap_request)
                if response.provider_id != provider_id:
                    raise ValueError("statement source returned another provider")
            provider_calls.append(
                FinancialProviderRouteCall(
                    provider_id=provider_id,
                    missing_annual_period_ends=gap_request.missing_annual_period_ends,
                    missing_reporting_period_ends=(
                        gap_request.missing_reporting_period_ends
                    ),
                    conflicted_annual_period_ends=(
                        gap_request.conflicted_annual_period_ends
                    ),
                    conflicted_reporting_period_ends=(
                        gap_request.conflicted_reporting_period_ends
                    ),
                    subrequest_keys=response.subrequest_keys,
                    physical_attempt_ids=response.physical_attempt_ids,
                    physical_attempt_events=response.physical_attempt_events,
                    subrequest_outcomes=response.subrequest_outcomes,
                    adapter_row_rejections=response.adapter_row_rejections,
                )
            )
            for attempt_id in response.physical_attempt_ids:
                if attempt_id not in physical_attempt_ids:
                    physical_attempt_ids.append(attempt_id)
            for event in response.physical_attempt_events:
                physical_attempt_events.setdefault(event.attempt_event_id, event)
            acquisition_outcomes.extend(
                _manifest_acquisition_outcomes(
                    response,
                    capability=FinancialCapability.STATEMENT,
                )
            )
            acquisition_outcomes.extend(
                _adapter_rejection_manifest_outcomes(
                    response,
                    capability=FinancialCapability.STATEMENT,
                )
            )
            if response.kind is FinancialProviderResponseKind.UNAVAILABLE:
                target_annual, target_reporting = publication_gap_targets()
                target_keys = {
                    *(('annual', item) for item in target_annual),
                    *(('reporting', item) for item in target_reporting),
                }
                continue

            assert response.response_artifact is not None
            for artifact in response.retained_artifacts:
                artifacts.setdefault(
                    artifact.artifact_identity,
                    FinancialProviderArtifactEntry(artifact=artifact),
                )
            response_candidates = (
                _ordered_v2_response_candidates(response.candidates)
                if publication_aware
                else response.candidates
            )
            qualified_revision_candidate_identities = (
                _qualified_v2_revision_candidate_identities(response_candidates)
                if publication_aware
                else frozenset()
            )
            for candidate in response_candidates:
                publication_candidates.setdefault(
                    candidate.candidate_identity,
                    candidate,
                )
                key = _statement_candidate_target_key(
                    candidate,
                    target_annual=target_annual,
                    target_reporting=target_reporting,
                )
                expected_frequency = candidate.period_identity.frequency
                if publication_aware:
                    period_end = candidate.period_identity.period_end
                    in_annual = period_end in target_annual
                    in_reporting = period_end in target_reporting
                    if in_annual and not in_reporting:
                        expected_frequency = FinancialReportingFrequency.ANNUAL
                    elif in_reporting and not in_annual:
                        expected_frequency = FinancialReportingFrequency.QUARTERLY
                assessment = assess_financial_period_candidate(
                    candidate,
                    requested_capability=FinancialCapability.STATEMENT,
                    requested_statement_type=request.statement_type,
                    requested_ratio_family=None,
                    expected_instrument_identity=request.instrument_identity,
                    expected_frequency=expected_frequency,
                    expected_currency=request.currency,
                    expected_consolidation_scope=request.consolidation_scope,
                    pit_as_of_date=request.as_of_date,
                )
                if (
                    assessment.usable_for_current_analysis
                    and candidate.period_identity.company_type is not request.company_type
                ):
                    assessment = (
                        reject_financial_period_for_company_type(assessment)
                        if publication_aware
                        else mark_financial_period_company_type_conflicted(assessment)
                    )
                provider_candidates[candidate.candidate_identity] = (
                    FinancialProviderCandidate(
                        candidate=candidate,
                        assessment=assessment,
                    )
                )
                if assessment.disposition in {
                    FinancialPeriodDisposition.REJECTED,
                    FinancialPeriodDisposition.CONFLICTED,
                }:
                    rejected_periods[candidate.candidate_identity] = (
                        FinancialRejectedPeriod.create(
                            candidate=candidate,
                            assessment=assessment,
                        )
                    )
                    if (
                        key is not None
                        and assessment.disposition
                        is FinancialPeriodDisposition.CONFLICTED
                    ):
                        conflicted_keys.add(key)
                    continue
                if key is None:
                    continue
                if key in conflicted_keys:
                    conflicted_candidate_identities.add(candidate.candidate_identity)
                    conflicted_assessment = mark_financial_period_conflicted(assessment)
                    provider_candidates[candidate.candidate_identity] = (
                        FinancialProviderCandidate(
                            candidate=candidate,
                            assessment=conflicted_assessment,
                        )
                    )
                    rejected_periods[candidate.candidate_identity] = (
                        FinancialRejectedPeriod.create(
                            candidate=candidate,
                            assessment=conflicted_assessment,
                        )
                    )
                    continue
                selected = selected_candidates.get(key)
                if selected is None:
                    selections[key] = FinancialPeriodSelection.create(
                        candidate=candidate,
                        assessment=assessment,
                    )
                    selected_candidates[key] = candidate
                    selected_assessments[key] = assessment
                    continue

                if publication_aware:
                    selected_binding = provider_candidates[
                        selected.candidate_identity
                    ]
                    overlapping_binding = provider_candidates[
                        candidate.candidate_identity
                    ]
                    if financial_revision_selection_key(
                        selected_binding
                    ) != financial_revision_selection_key(overlapping_binding):
                        assessment = (
                            reject_financial_period_for_incompatible_selection_key(
                                assessment,
                                selected_candidate=selected_binding,
                                overlapping_candidate=overlapping_binding,
                            )
                        )
                        provider_candidates[candidate.candidate_identity] = (
                            FinancialProviderCandidate(
                                candidate=candidate,
                                assessment=assessment,
                            )
                        )
                        rejected_periods[candidate.candidate_identity] = (
                            FinancialRejectedPeriod.create(
                                candidate=candidate,
                                assessment=assessment,
                            )
                        )
                        continue

                if (
                    publication_aware
                    and selected.artifact.dataset.provider_id
                    == candidate.artifact.dataset.provider_id
                    and selected.candidate_identity
                    in qualified_revision_candidate_identities
                    and candidate.candidate_identity
                    in qualified_revision_candidate_identities
                ):
                    continue

                overlap, candidate_conflicts = compare_financial_period_candidates(
                    selected,
                    candidate,
                    selected_assessment=(
                        selected_assessments[key] if publication_aware else None
                    ),
                    overlapping_assessment=(assessment if publication_aware else None),
                )
                overlaps.append(overlap)
                conflicts.extend(candidate_conflicts)
                if not candidate_conflicts and overlap.disposition in {
                    "equivalent_unselected",
                    "non_critical_difference",
                }:
                    continue
                conflicted_keys.add(key)
                conflicted_candidate_identities.update(
                    (selected.candidate_identity, candidate.candidate_identity)
                )
                if publication_aware:
                    current_assessment = (
                        mark_financial_period_company_type_conflicted(assessment)
                        if overlap.disposition == "company_type_conflict"
                        else mark_financial_period_conflicted(assessment)
                    )
                    provider_candidates[candidate.candidate_identity] = (
                        FinancialProviderCandidate(
                            candidate=candidate,
                            assessment=current_assessment,
                        )
                    )
                    rejected_periods[candidate.candidate_identity] = (
                        FinancialRejectedPeriod.create(
                            candidate=candidate,
                            assessment=current_assessment,
                        )
                    )
                    continue
                previous_assessment = mark_financial_period_conflicted(
                    selected_assessments[key]
                )
                current_assessment = (
                    mark_financial_period_company_type_conflicted(assessment)
                    if overlap.disposition == "company_type_conflict"
                    else mark_financial_period_conflicted(assessment)
                )
                provider_candidates[selected.candidate_identity] = (
                    FinancialProviderCandidate(
                        candidate=selected,
                        assessment=previous_assessment,
                    )
                )
                provider_candidates[candidate.candidate_identity] = (
                    FinancialProviderCandidate(
                        candidate=candidate,
                        assessment=current_assessment,
                    )
                )
                rejected_periods[selected.candidate_identity] = (
                    FinancialRejectedPeriod.create(
                        candidate=selected,
                        assessment=previous_assessment,
                    )
                )
                rejected_periods[candidate.candidate_identity] = (
                    FinancialRejectedPeriod.create(
                        candidate=candidate,
                        assessment=current_assessment,
                    )
                )
                selections.pop(key, None)
                selected_candidates.pop(key, None)
                selected_assessments.pop(key, None)

            target_annual, target_reporting = publication_gap_targets()
            target_keys = {
                *(('annual', item) for item in target_annual),
                *(('reporting', item) for item in target_reporting),
            }

        publication_target_construction = None
        if publication_aware:
            assert self._routing_plan.financial_eligibility_policy is not None
            publication_target_construction = (
                construct_mainland_financial_period_targets(
                    run_as_of_date=request.as_of_date,
                    expected_instrument_identity=request.instrument_identity,
                    expected_company_type=request.company_type,
                    expected_consolidation_scope=request.consolidation_scope,
                    candidates=tuple(publication_candidates.values()),
                    qualified_provider_ids=qualified_publication_providers,
                    acquisition_annual_period_ends=(
                        request.eligible_annual_period_ends
                    ),
                    acquisition_reporting_period_ends=(
                        request.eligible_reporting_period_ends
                    ),
                    listing_date=request.listing_date,
                    listing_provenance=request.listing_provenance,
                    policy=self._routing_plan.financial_eligibility_policy,
                )
            )
            target_annual = (
                publication_target_construction.annual_target_period_ends
            )
            target_reporting = (
                publication_target_construction.reporting_target_period_ends
            )
            target_keys = {
                *(("annual", item) for item in target_annual),
                *(("reporting", item) for item in target_reporting),
            }
            selections = {
                key: value for key, value in selections.items() if key in target_keys
            }
            selected_candidates = {
                key: value
                for key, value in selected_candidates.items()
                if key in target_keys
            }
            selected_assessments = {
                key: value
                for key, value in selected_assessments.items()
                if key in target_keys
            }

        aggregate = assess_financial_history_coverage(
            instrument_identity=request.instrument_identity,
            statement_type=request.statement_type,
            company_type=request.company_type,
            consolidation_scope=request.consolidation_scope,
            currency=request.currency,
            assessments=tuple(selected_assessments.values()),
            eligible_annual_period_ends=target_annual,
            eligible_reporting_period_ends=target_reporting,
            listing_date=request.listing_date,
            listing_provenance=request.listing_provenance,
        )
        revision_selections = (
            _v2_revision_selections(
                provider_candidates=provider_candidates,
                selected_candidates=tuple(selected_candidates.values()),
                conflicted_candidate_identities=conflicted_candidate_identities,
                route=route,
                requested_analysis_scope=None,
            )
            if publication_aware
            else ()
        )
        manifest = FinancialAcquisitionManifest.create(
            capability_routing_plan_signature=self._routing_plan.plan_signature,
            instrument_identity=request.instrument_identity,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=request.statement_type,
            requested_ratio_family=None,
            artifacts=tuple(artifacts.values()),
            acquisition_outcomes=tuple(acquisition_outcomes),
            provider_candidates=tuple(provider_candidates.values()),
            selections=tuple(selections.values()),
            rejected_periods=tuple(rejected_periods.values()),
            overlaps=overlaps,
            conflicts=conflicts,
            aggregate_completeness=aggregate,
            revision_selections=revision_selections,
        )
        result = FinancialStatementRoutingResult(
            route=route,
            provider_calls=tuple(provider_calls),
            manifest=manifest,
            selected_candidates=tuple(
                candidate
                for _, candidate in sorted(
                    selected_candidates.items(),
                    key=lambda item: (item[0][0], item[0][1]),
                )
            ),
            missing_annual_period_ends=aggregate.missing_annual_period_ends,
            missing_reporting_period_ends=aggregate.missing_reporting_period_ends,
            conflicted_annual_period_ends=tuple(
                period
                for period in target_annual
                if ("annual", period) in conflicted_keys
            ),
            conflicted_reporting_period_ends=tuple(
                period
                for period in target_reporting
                if ("reporting", period) in conflicted_keys
            ),
            physical_attempt_ids=tuple(physical_attempt_ids),
            publication_target_construction=publication_target_construction,
            financial_eligibility_binding=eligibility_binding,
        )
        if (
            not physical_attempt_events
            and request.financial_eligibility_binding is None
        ):
            return result
        disposition = financial_statement_evidence_disposition(result)
        finalized_manifest = manifest.finalize(
            physical_attempt_events=tuple(physical_attempt_events.values()),
            disposition=disposition,
            rendered_value=render_statement_routing_result(result),
            publication_target_construction=publication_target_construction,
            eligibility_policy_version=(
                request.eligibility_policy_version
                if eligibility_binding is not None
                else None
            ),
            financial_eligibility_binding=eligibility_binding,
        )
        return result.model_copy(update={"manifest": finalized_manifest})

    def route_indicators(
        self,
        request: FinancialIndicatorRoutingRequest,
    ) -> FinancialIndicatorRoutingResult:
        """Select ratio periods per family and request only unresolved gaps."""

        route = self._routing_plan.route_for(
            MainlandCapability.FINANCIAL_INDICATORS
        )
        publication_aware = request.eligibility_policy_version is not None
        expected_eligibility_binding = (
            self._routing_plan.financial_eligibility_binding
            if publication_aware
            else None
        )
        eligibility_binding = request.financial_eligibility_binding
        availability_policy_version = (
            request.eligibility_policy_version
            if request.requested_analysis_scope is not None
            else None
        )
        if publication_aware and (
            self._routing_plan.eligibility_policy_version
            != request.eligibility_policy_version
            or self._routing_plan.financial_eligibility_policy is None
            or (
                request.financial_eligibility_binding is not None
                and request.financial_eligibility_binding
                != expected_eligibility_binding
            )
        ):
            raise ValueError("indicator target construction policy mismatch")
        acquisition_periods = tuple(
            sorted(set(request.eligible_reporting_period_ends), reverse=True)
        )
        target_periods = (
            acquisition_periods if publication_aware else acquisition_periods[:8]
        )
        selected_candidates: dict[
            tuple[FinancialRatioFamily, date],
            FinancialPeriodCandidate,
        ] = {}
        selected_assessments: dict[
            tuple[FinancialRatioFamily, date],
            FinancialPeriodCompletenessAssessment,
        ] = {}
        provider_candidates: dict[str, FinancialProviderCandidate] = {}
        publication_candidates: dict[str, FinancialPeriodCandidate] = {}
        rejected_periods: dict[str, FinancialRejectedPeriod] = {}
        artifacts: dict[str, FinancialProviderArtifactEntry] = {}
        acquisition_outcomes: list[FinancialAcquisitionOutcome] = []
        overlaps: list[FinancialPeriodOverlapFinding] = []
        conflicts: list[FinancialCriticalValueConflict] = []
        conflicted_keys: set[tuple[FinancialRatioFamily, date]] = set()
        conflicted_candidate_identities: set[str] = set()
        provider_calls: list[FinancialIndicatorProviderRouteCall] = []
        physical_attempt_ids: list[str] = []
        physical_attempt_events: dict[str, ProviderSubrequestAttemptEvent] = {}
        current_profile_provider: Literal["yfinance"] | None = None
        qualified_publication_providers = _qualified_publication_provider_ids(
            route,
            capability=FinancialCapability.RATIO_FAMILY,
        )

        def publication_gap_periods() -> tuple[date, ...]:
            if not publication_aware:
                return target_periods
            assert self._routing_plan.financial_eligibility_policy is not None
            return filter_mainland_financial_acquisition_periods(
                period_ends=acquisition_periods,
                run_as_of_date=request.as_of_date,
                expected_instrument_identity=request.instrument_identity,
                expected_company_type=request.company_type,
                expected_consolidation_scope=request.consolidation_scope,
                candidates=tuple(publication_candidates.values()),
                qualified_provider_ids=qualified_publication_providers,
                listing_date=request.listing_date,
                listing_provenance=request.listing_provenance,
                policy=self._routing_plan.financial_eligibility_policy,
            )

        for provider_id in route:
            missing_by_family = _indicator_missing_periods(
                ratio_families=request.ratio_families,
                target_periods=target_periods,
                selected_candidates=selected_candidates,
            )
            if not missing_by_family:
                break
            gap_request = FinancialIndicatorProviderGapRequest(
                provider_id=provider_id,
                instrument_identity=request.instrument_identity,
                as_of_date=request.as_of_date,
                company_type=request.company_type,
                company_type_resolution=request.company_type_resolution,
                consolidation_scope=request.consolidation_scope,
                currency=request.currency,
                missing_periods_by_family=missing_by_family,
            )
            source = self._indicator_sources.get(provider_id)
            if source is None:
                response = FinancialProviderPeriodResponse.unavailable(
                    provider_id=provider_id,
                    reason=AcquisitionUnavailableReason.NOT_CONFIGURED,
                )
            else:
                response = source(gap_request)
                if response.provider_id != provider_id:
                    raise ValueError("indicator source returned another provider")
            provider_calls.append(
                FinancialIndicatorProviderRouteCall(
                    provider_id=provider_id,
                    missing_periods_by_family=missing_by_family,
                    subrequest_keys=response.subrequest_keys,
                    physical_attempt_ids=response.physical_attempt_ids,
                    physical_attempt_events=response.physical_attempt_events,
                    subrequest_outcomes=response.subrequest_outcomes,
                    adapter_row_rejections=response.adapter_row_rejections,
                )
            )
            for attempt_id in response.physical_attempt_ids:
                if attempt_id not in physical_attempt_ids:
                    physical_attempt_ids.append(attempt_id)
            for event in response.physical_attempt_events:
                physical_attempt_events.setdefault(event.attempt_event_id, event)
            acquisition_outcomes.extend(
                _manifest_acquisition_outcomes(
                    response,
                    capability=FinancialCapability.RATIO_FAMILY,
                )
            )
            acquisition_outcomes.extend(
                _adapter_rejection_manifest_outcomes(
                    response,
                    capability=FinancialCapability.RATIO_FAMILY,
                )
            )
            if response.kind is FinancialProviderResponseKind.UNAVAILABLE:
                target_periods = publication_gap_periods()
                continue

            assert response.response_artifact is not None
            for artifact in response.retained_artifacts:
                artifacts.setdefault(
                    artifact.artifact_identity,
                    FinancialProviderArtifactEntry(artifact=artifact),
                )
            if response.degraded_current_profile:
                current_profile_provider = "yfinance"
            response_candidates = (
                _ordered_v2_response_candidates(response.candidates)
                if publication_aware
                else response.candidates
            )
            qualified_revision_candidate_identities = (
                _qualified_v2_revision_candidate_identities(response_candidates)
                if publication_aware
                else frozenset()
            )
            for candidate in response_candidates:
                publication_candidates.setdefault(
                    candidate.candidate_identity,
                    candidate,
                )
                identity = candidate.period_identity
                ratio_family = identity.ratio_family
                if ratio_family is None or ratio_family not in request.ratio_families:
                    continue
                declaration_version, expected_currency = (
                    _INDICATOR_PROVIDER_ASSESSMENT_CONTRACT.get(
                        candidate.artifact.dataset.provider_id,
                        (FINANCIAL_RATIO_DECLARATION_V1, request.currency),
                    )
                )
                assessment = assess_financial_period_candidate(
                    candidate,
                    requested_capability=FinancialCapability.RATIO_FAMILY,
                    requested_statement_type=None,
                    requested_ratio_family=ratio_family,
                    expected_instrument_identity=request.instrument_identity,
                    expected_frequency=FinancialReportingFrequency.QUARTERLY,
                    expected_currency=expected_currency,
                    expected_consolidation_scope=request.consolidation_scope,
                    pit_as_of_date=request.as_of_date,
                    ratio_declaration_version=declaration_version,
                    eligibility_policy_version=availability_policy_version,
                )
                if request.requested_analysis_scope is (
                    FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
                ):
                    assessment = reject_financial_period_for_strict_pit(
                        assessment
                    )
                if (
                    assessment.usable_for_current_analysis
                    and identity.company_type is not request.company_type
                ):
                    assessment = (
                        reject_financial_period_for_company_type(assessment)
                        if publication_aware
                        else mark_financial_period_company_type_conflicted(
                            assessment
                        )
                    )
                provider_candidates[candidate.candidate_identity] = (
                    FinancialProviderCandidate(
                        candidate=candidate,
                        assessment=assessment,
                    )
                )
                if assessment.disposition in {
                    FinancialPeriodDisposition.REJECTED,
                    FinancialPeriodDisposition.CONFLICTED,
                }:
                    rejected_periods[candidate.candidate_identity] = (
                        FinancialRejectedPeriod.create(
                            candidate=candidate,
                            assessment=assessment,
                        )
                    )
                    continue
                key = (ratio_family, identity.period_end)
                if identity.period_end not in target_periods:
                    continue
                if key in conflicted_keys:
                    conflicted_candidate_identities.add(candidate.candidate_identity)
                    conflicted_assessment = mark_financial_period_conflicted(
                        assessment
                    )
                    provider_candidates[candidate.candidate_identity] = (
                        FinancialProviderCandidate(
                            candidate=candidate,
                            assessment=conflicted_assessment,
                        )
                    )
                    rejected_periods[candidate.candidate_identity] = (
                        FinancialRejectedPeriod.create(
                            candidate=candidate,
                            assessment=conflicted_assessment,
                        )
                    )
                    continue
                selected = selected_candidates.get(key)
                if selected is None:
                    selected_candidates[key] = candidate
                    selected_assessments[key] = assessment
                    continue
                if publication_aware:
                    selected_binding = provider_candidates[
                        selected.candidate_identity
                    ]
                    overlapping_binding = provider_candidates[
                        candidate.candidate_identity
                    ]
                    if financial_revision_selection_key(
                        selected_binding
                    ) != financial_revision_selection_key(overlapping_binding):
                        assessment = (
                            reject_financial_period_for_incompatible_selection_key(
                                assessment,
                                selected_candidate=selected_binding,
                                overlapping_candidate=overlapping_binding,
                            )
                        )
                        provider_candidates[candidate.candidate_identity] = (
                            FinancialProviderCandidate(
                                candidate=candidate,
                                assessment=assessment,
                            )
                        )
                        rejected_periods[candidate.candidate_identity] = (
                            FinancialRejectedPeriod.create(
                                candidate=candidate,
                                assessment=assessment,
                            )
                        )
                        continue
                if (
                    publication_aware
                    and selected.artifact.dataset.provider_id
                    == candidate.artifact.dataset.provider_id
                    and selected.candidate_identity
                    in qualified_revision_candidate_identities
                    and candidate.candidate_identity
                    in qualified_revision_candidate_identities
                ):
                    continue
                overlap, candidate_conflicts = compare_financial_period_candidates(
                    selected,
                    candidate,
                    selected_assessment=(
                        selected_assessments[key] if publication_aware else None
                    ),
                    overlapping_assessment=(assessment if publication_aware else None),
                )
                overlaps.append(overlap)
                conflicts.extend(candidate_conflicts)
                if (
                    not candidate_conflicts
                    and overlap.disposition
                    in {"equivalent_unselected", "non_critical_difference"}
                ):
                    continue
                conflicted_keys.add(key)
                conflicted_candidate_identities.update(
                    (selected.candidate_identity, candidate.candidate_identity)
                )
                if publication_aware:
                    current_assessment = (
                        mark_financial_period_company_type_conflicted(assessment)
                        if overlap.disposition == "company_type_conflict"
                        else mark_financial_period_conflicted(assessment)
                    )
                    provider_candidates[candidate.candidate_identity] = (
                        FinancialProviderCandidate(
                            candidate=candidate,
                            assessment=current_assessment,
                        )
                    )
                    rejected_periods[candidate.candidate_identity] = (
                        FinancialRejectedPeriod.create(
                            candidate=candidate,
                            assessment=current_assessment,
                        )
                    )
                    continue
                previous_assessment = mark_financial_period_conflicted(
                    selected_assessments[key]
                )
                current_assessment = (
                    mark_financial_period_company_type_conflicted(assessment)
                    if overlap.disposition == "company_type_conflict"
                    else mark_financial_period_conflicted(assessment)
                )
                provider_candidates[selected.candidate_identity] = (
                    FinancialProviderCandidate(
                        candidate=selected,
                        assessment=previous_assessment,
                    )
                )
                provider_candidates[candidate.candidate_identity] = (
                    FinancialProviderCandidate(
                        candidate=candidate,
                        assessment=current_assessment,
                    )
                )
                rejected_periods[selected.candidate_identity] = (
                    FinancialRejectedPeriod.create(
                        candidate=selected,
                        assessment=previous_assessment,
                    )
                )
                rejected_periods[candidate.candidate_identity] = (
                    FinancialRejectedPeriod.create(
                        candidate=candidate,
                        assessment=current_assessment,
                    )
                )
                selected_candidates.pop(key, None)
                selected_assessments.pop(key, None)

            target_periods = publication_gap_periods()

        publication_target_construction = None
        if publication_aware:
            assert self._routing_plan.financial_eligibility_policy is not None
            publication_target_construction = (
                construct_mainland_financial_period_targets(
                    run_as_of_date=request.as_of_date,
                    expected_instrument_identity=request.instrument_identity,
                    expected_company_type=request.company_type,
                    expected_consolidation_scope=request.consolidation_scope,
                    candidates=tuple(publication_candidates.values()),
                    qualified_provider_ids=qualified_publication_providers,
                    acquisition_annual_period_ends=(),
                    acquisition_reporting_period_ends=(
                        request.eligible_reporting_period_ends
                    ),
                    listing_date=request.listing_date,
                    listing_provenance=request.listing_provenance,
                    policy=self._routing_plan.financial_eligibility_policy,
                )
            )
            target_periods = (
                publication_target_construction.reporting_target_period_ends
            )
            selected_candidates = {
                key: value
                for key, value in selected_candidates.items()
                if key[1] in target_periods
            }
            selected_assessments = {
                key: value
                for key, value in selected_assessments.items()
                if key[1] in target_periods
            }

        family_completeness_assessments: dict[
            FinancialRatioFamily,
            FinancialRatioHistoryCompletenessAssessment,
        ] = {}
        family_manifests: list[FinancialAcquisitionManifest] = []
        candidate_artifact_ids = {
            item.candidate.artifact.artifact_identity
            for item in provider_candidates.values()
        }
        response_lineage_artifact_ids = {
            artifact_identity
            for artifact_identity in artifacts
            if artifact_identity not in candidate_artifact_ids
        }
        for family in request.ratio_families:
            selected_family_assessments = tuple(
                assessment
                for (candidate_family, _), assessment in selected_assessments.items()
                if candidate_family is family
            )
            selected_family_currencies = {
                assessment.currency for assessment in selected_family_assessments
            }
            aggregate_currency = (
                next(iter(selected_family_currencies))
                if len(selected_family_currencies) == 1
                else request.currency
            )
            completeness = assess_financial_ratio_history_coverage(
                instrument_identity=request.instrument_identity,
                ratio_family=family,
                company_type=request.company_type,
                consolidation_scope=request.consolidation_scope,
                currency=aggregate_currency,
                assessments=selected_family_assessments,
                eligible_reporting_period_ends=target_periods,
            )
            family_completeness_assessments[family] = completeness
            family_candidates = tuple(
                item
                for item in provider_candidates.values()
                if item.candidate.period_identity.ratio_family is family
            )
            family_candidate_ids = {
                item.candidate.candidate_identity for item in family_candidates
            }
            family_artifact_ids = {
                item.candidate.artifact.artifact_identity
                for item in family_candidates
            } | response_lineage_artifact_ids
            family_selections = tuple(
                FinancialPeriodSelection.create(
                    candidate=candidate,
                    assessment=selected_assessments[(family, period)],
                    requested_analysis_scope=request.requested_analysis_scope,
                    eligibility_policy_version=availability_policy_version,
                )
                for (candidate_family, period), candidate in selected_candidates.items()
                if candidate_family is family
            )
            family_rejections = tuple(
                rejection
                for candidate_id, rejection in rejected_periods.items()
                if provider_candidates[candidate_id]
                .candidate.period_identity.ratio_family
                is family
            )
            family_revision_selections = (
                tuple(
                    item
                    for item in _v2_revision_selections(
                        provider_candidates=provider_candidates,
                        selected_candidates=tuple(selected_candidates.values()),
                        conflicted_candidate_identities=(
                            conflicted_candidate_identities
                        ),
                        route=route,
                        requested_analysis_scope=request.requested_analysis_scope,
                    )
                    if item.ratio_family is family
                )
                if publication_aware
                else ()
            )
            family_manifests.append(
                FinancialAcquisitionManifest.create(
                    capability_routing_plan_signature=self._routing_plan.plan_signature,
                    instrument_identity=request.instrument_identity,
                    requested_capability=FinancialCapability.RATIO_FAMILY,
                    requested_statement_type=None,
                    requested_ratio_family=family,
                    artifacts=tuple(
                        item
                        for item in artifacts.values()
                        if item.artifact.artifact_identity in family_artifact_ids
                    ),
                    acquisition_outcomes=tuple(
                        outcome
                        for outcome in acquisition_outcomes
                        if outcome.artifact_identity is None
                        or outcome.artifact_identity in family_artifact_ids
                    ),
                    provider_candidates=family_candidates,
                    selections=family_selections,
                    rejected_periods=family_rejections,
                    overlaps=tuple(
                        overlap
                        for overlap in overlaps
                        if overlap.selected_candidate_identity in family_candidate_ids
                        or overlap.overlapping_candidate_identity
                        in family_candidate_ids
                    ),
                    conflicts=tuple(
                        conflict
                        for conflict in conflicts
                        if conflict.selected_candidate_identity
                        in family_candidate_ids
                        or conflict.conflicting_candidate_identity
                        in family_candidate_ids
                    ),
                    aggregate_completeness=completeness,
                    revision_selections=family_revision_selections,
                )
            )

        family_assessments: tuple[FinancialIndicatorFamilyAssessment, ...] = ()
        aggregate_family_assessment = None
        if request.contract_version in {
            "financial-indicator-routing-request-v4",
            "financial-indicator-routing-request-v5",
        }:
            (
                family_assessments,
                aggregate_family_assessment,
            ) = assess_financial_indicator_family_outcomes(
                request=request,
                family_manifests=family_manifests,
                publication_target_construction=publication_target_construction,
            )
        result = FinancialIndicatorRoutingResult(
            contract_version=(
                "financial-indicator-routing-result-v2"
                if aggregate_family_assessment is not None
                else "financial-indicator-routing-result-v1"
            ),
            route=route,
            provider_calls=tuple(provider_calls),
            family_manifests=tuple(family_manifests),
            selected_candidates=tuple(
                candidate
                for _, candidate in sorted(
                    selected_candidates.items(),
                    key=lambda item: (
                        tuple(FinancialRatioFamily).index(item[0][0]),
                        item[0][1],
                    ),
                )
            ),
            complete_families=tuple(
                family
                for family in request.ratio_families
                if family_completeness_assessments[family].complete
            ),
            missing_periods_by_family=tuple(
                (
                    family,
                    family_completeness_assessments[
                        family
                    ].missing_reporting_period_ends,
                )
                for family in request.ratio_families
                if family_completeness_assessments[
                    family
                ].missing_reporting_period_ends
            ),
            retained_artifacts=tuple(
                entry.artifact
                for entry in sorted(
                    artifacts.values(),
                    key=lambda item: item.artifact.artifact_identity,
                )
            ),
            current_profile_degraded=current_profile_provider is not None,
            current_profile_provider=current_profile_provider,
            physical_attempt_ids=tuple(physical_attempt_ids),
            physical_attempt_events=tuple(physical_attempt_events.values()),
            publication_target_construction=publication_target_construction,
            requested_analysis_scope=request.requested_analysis_scope,
            eligibility_policy_version=availability_policy_version,
            financial_eligibility_binding=eligibility_binding,
            family_assessments=family_assessments,
            aggregate_family_assessment=aggregate_family_assessment,
        )
        if (
            not physical_attempt_events
            and request.financial_eligibility_binding is None
        ):
            return result
        rendered_value = render_indicator_routing_result(result)
        finalized_manifests = tuple(
            manifest.finalize(
                physical_attempt_events=tuple(physical_attempt_events.values()),
                disposition=financial_indicator_evidence_disposition(
                    manifest,
                    result,
                    request,
                ),
                rendered_value=rendered_value,
                publication_target_construction=publication_target_construction,
                requested_analysis_scope=request.requested_analysis_scope,
                eligibility_policy_version=availability_policy_version,
                financial_eligibility_binding=eligibility_binding,
            )
            for manifest in result.family_manifests
        )
        return result.model_copy(update={"family_manifests": finalized_manifests})


def _statement_gap_request(
    *,
    provider_id: str,
    request: FinancialStatementRoutingRequest,
    target_annual: tuple[date, ...],
    target_reporting: tuple[date, ...],
    selections: Mapping[tuple[str, date], FinancialPeriodSelection],
    conflicted_keys: set[tuple[str, date]],
) -> FinancialProviderGapRequest:
    return FinancialProviderGapRequest(
        provider_id=provider_id,
        instrument_identity=request.instrument_identity,
        statement_type=request.statement_type,
        as_of_date=request.as_of_date,
        missing_annual_period_ends=tuple(
            period for period in target_annual if ("annual", period) not in selections
        ),
        missing_reporting_period_ends=tuple(
            period
            for period in target_reporting
            if ("reporting", period) not in selections
        ),
        conflicted_annual_period_ends=tuple(
            period for period in target_annual if ("annual", period) in conflicted_keys
        ),
        conflicted_reporting_period_ends=tuple(
            period
            for period in target_reporting
            if ("reporting", period) in conflicted_keys
        ),
    )


def _validate_mainland_financial_identity(
    identity: InstrumentIdentityEvidence,
    *,
    currency: str,
) -> None:
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.venue not in {"XSHG", "XSHE"}
        or identity.currency.upper() != "CNY"
        or currency.upper() != identity.currency.upper()
    ):
        raise ValueError(
            "qualified mainland financial routing requires a CNY XSHG/XSHE equity identity"
        )


def _statement_candidate_target_key(
    candidate: FinancialPeriodCandidate,
    *,
    target_annual: tuple[date, ...],
    target_reporting: tuple[date, ...],
) -> tuple[str, date] | None:
    identity = candidate.period_identity
    if (
        identity.frequency is FinancialReportingFrequency.ANNUAL
        and identity.period_end in target_annual
    ):
        return ("annual", identity.period_end)
    if (
        identity.frequency is FinancialReportingFrequency.QUARTERLY
        and identity.period_end in target_reporting
    ):
        return ("reporting", identity.period_end)
    return None


def _indicator_missing_periods(
    *,
    ratio_families: tuple[FinancialRatioFamily, ...],
    target_periods: tuple[date, ...],
    selected_candidates: Mapping[
        tuple[FinancialRatioFamily, date],
        FinancialPeriodCandidate,
    ],
) -> tuple[tuple[FinancialRatioFamily, tuple[date, ...]], ...]:
    return tuple(
        (
            family,
            tuple(
                period
                for period in target_periods
                if (family, period) not in selected_candidates
            ),
        )
        for family in ratio_families
        if any((family, period) not in selected_candidates for period in target_periods)
    )


def financial_period_response_from_statement_adapter(
    *,
    provider_id: Literal["tushare", "akshare_sina"],
    result: Any,
) -> FinancialProviderPeriodResponse:
    """Project a Ticket 04/07 payload-free adapter result into Ticket 10."""

    actual_provider = {
        "tushare": "tushare",
        "akshare_sina": "akshare_sina",
    }[provider_id]
    outcome_kind = str(result.outcome.kind.value)
    subrequest_keys = (str(result.subrequest_key),)
    physical_attempt_ids = tuple(
        str(event.attempt_event_id) for event in result.attempt_events
    )
    if outcome_kind != "available":
        unavailable_reason = _subrequest_unavailable_reason(outcome_kind)
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=actual_provider,
            reason=unavailable_reason,
            subrequest_keys=subrequest_keys,
            physical_attempt_ids=physical_attempt_ids,
            physical_attempt_events=tuple(
                event
                for event in result.attempt_events
                if isinstance(event, ProviderSubrequestAttemptEvent)
            ),
            subrequest_outcomes=(
                FinancialSubrequestAcquisitionOutcome(
                    provider_id=actual_provider,
                    subrequest_key=subrequest_keys[0],
                    outcome="unavailable",
                    reason=unavailable_reason,
                ),
            ),
        )
    retained_by_id = {
        artifact.artifact_identity: artifact
        for artifact in (result.artifact, *result.row_artifacts)
        if artifact is not None
    }
    assert result.artifact is not None
    adapter_row_rejections = _project_adapter_row_rejections(
        provider_id=actual_provider,
        row_rejections=result.row_rejections,
    )
    return FinancialProviderPeriodResponse.available(
        provider_id=actual_provider,
        response_artifact=result.artifact,
        retained_artifacts=tuple(retained_by_id.values()),
        candidates=(
            *result.annual_candidates,
            *result.reporting_period_candidates,
        ),
        subrequest_keys=subrequest_keys,
        physical_attempt_ids=physical_attempt_ids,
        physical_attempt_events=tuple(
            event
            for event in result.attempt_events
            if isinstance(event, ProviderSubrequestAttemptEvent)
        ),
        subrequest_outcomes=(
            FinancialSubrequestAcquisitionOutcome(
                provider_id=actual_provider,
                subrequest_key=subrequest_keys[0],
                outcome="available",
                artifact_identity=result.artifact.artifact_identity,
            ),
        ),
        adapter_row_rejections=adapter_row_rejections,
    )


def financial_period_response_from_indicator_adapter(
    *,
    provider_id: Literal[
        "tushare",
        "akshare",
        "baostock_qualified_families",
    ],
    result: Any,
) -> FinancialProviderPeriodResponse:
    """Project Ticket 05/09 indicator results into one routed response."""

    operational_results = tuple(
        getattr(result, "operational_results", (result,))
    )
    if not operational_results:
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=provider_id,
            reason=AcquisitionUnavailableReason.NO_DATA,
        )
    subrequest_keys: list[str] = []
    physical_attempt_ids: list[str] = []
    physical_attempt_events: list[ProviderSubrequestAttemptEvent] = []
    retained: dict[str, FinancialProviderArtifactIdentity] = {}
    candidates: list[FinancialPeriodCandidate] = []
    available_results: list[Any] = []
    subrequest_outcomes: list[FinancialSubrequestAcquisitionOutcome] = []
    adapter_row_rejections: list[FinancialAdapterRowRejection] = []
    for operational in operational_results:
        request = getattr(operational, "request", None)
        subrequest_key = getattr(operational, "subrequest_key", None) or getattr(
            request,
            "subrequest_key",
            None,
        )
        if subrequest_key is None:
            raise ValueError("indicator adapter result lacks a subrequest key")
        subrequest_key = str(subrequest_key)
        if subrequest_key not in subrequest_keys:
            subrequest_keys.append(subrequest_key)
        for event in operational.attempt_events:
            attempt_id = str(event.attempt_event_id)
            if attempt_id not in physical_attempt_ids:
                physical_attempt_ids.append(attempt_id)
                if isinstance(event, ProviderSubrequestAttemptEvent):
                    physical_attempt_events.append(event)
        outcome_kind = str(operational.outcome.kind.value)
        if outcome_kind != "available":
            subrequest_outcomes.append(
                FinancialSubrequestAcquisitionOutcome(
                    provider_id=provider_id,
                    subrequest_key=subrequest_key,
                    outcome="unavailable",
                    reason=_subrequest_unavailable_reason(outcome_kind),
                )
            )
            continue
        available_results.append(operational)
        assert operational.artifact is not None
        subrequest_outcomes.append(
            FinancialSubrequestAcquisitionOutcome(
                provider_id=provider_id,
                subrequest_key=subrequest_key,
                outcome="available",
                artifact_identity=operational.artifact.artifact_identity,
            )
        )
        for artifact in (
            operational.artifact,
            *operational.row_artifacts,
        ):
            if artifact is not None:
                retained.setdefault(artifact.artifact_identity, artifact)
        candidates.extend(operational.candidates)
        adapter_row_rejections.extend(
            _project_adapter_row_rejections(
                provider_id=provider_id,
                row_rejections=operational.row_rejections,
            )
        )
    if not available_results:
        first_kind = str(operational_results[0].outcome.kind.value)
        return FinancialProviderPeriodResponse.unavailable(
            provider_id=provider_id,
            reason=_subrequest_unavailable_reason(first_kind),
            subrequest_keys=subrequest_keys,
            physical_attempt_ids=physical_attempt_ids,
            physical_attempt_events=physical_attempt_events,
            subrequest_outcomes=subrequest_outcomes,
        )
    response_artifact = available_results[0].artifact
    assert response_artifact is not None
    return FinancialProviderPeriodResponse.available(
        provider_id=provider_id,
        response_artifact=response_artifact,
        retained_artifacts=tuple(retained.values()),
        candidates=candidates,
        subrequest_keys=subrequest_keys,
        physical_attempt_ids=physical_attempt_ids,
        physical_attempt_events=physical_attempt_events,
        subrequest_outcomes=subrequest_outcomes,
        adapter_row_rejections=adapter_row_rejections,
    )


def _subrequest_unavailable_reason(value: str) -> AcquisitionUnavailableReason:
    return {
        "permission_denied": AcquisitionUnavailableReason.PERMISSION_DENIED,
        "rate_limited": AcquisitionUnavailableReason.RATE_LIMITED,
        "authentication": AcquisitionUnavailableReason.AUTHENTICATION,
        "empty": AcquisitionUnavailableReason.EMPTY_FRAME,
        "malformed": AcquisitionUnavailableReason.MALFORMED_RESPONSE,
        "timeout": AcquisitionUnavailableReason.TIMEOUT,
        "disconnect": AcquisitionUnavailableReason.DISCONNECT,
        "provider_error": AcquisitionUnavailableReason.PROVIDER_ERROR,
        "upstream_busy": AcquisitionUnavailableReason.UPSTREAM_BUSY,
        "abandoned": AcquisitionUnavailableReason.PROVIDER_ERROR,
    }.get(value, AcquisitionUnavailableReason.PROVIDER_ERROR)


def _project_adapter_row_rejections(
    *,
    provider_id: str,
    row_rejections: Sequence[Any],
) -> tuple[FinancialAdapterRowRejection, ...]:
    projected: list[FinancialAdapterRowRejection] = []
    for rejection in row_rejections:
        artifact_identity = str(rejection.artifact_identity)
        ratio_family = getattr(rejection, "ratio_family", None)
        row_identity = getattr(rejection, "row_identity", None)
        if row_identity is None:
            family_token = (
                ratio_family.value if ratio_family is not None else "statement"
            )
            row_identity = (
                f"{provider_id}-row-rejection:"
                f"{artifact_identity}:{family_token}"
            )
        plural_candidate_identities = getattr(
            rejection,
            "candidate_identities",
            None,
        )
        if plural_candidate_identities is None:
            candidate_identity = getattr(rejection, "candidate_identity", None)
            candidate_identities = (
                (str(candidate_identity),)
                if candidate_identity is not None
                else ()
            )
        else:
            candidate_identities = tuple(
                str(candidate_identity)
                for candidate_identity in plural_candidate_identities
            )
        reasons = tuple(
            reason
            for reason in FinancialPeriodRejectionReason
            if reason in rejection.reasons
        )
        projected.append(
            FinancialAdapterRowRejection(
                provider_id=provider_id,
                row_identity=str(row_identity),
                artifact_identity=artifact_identity,
                candidate_identities=tuple(
                    sorted(set(candidate_identities))
                ),
                ratio_family=ratio_family,
                reasons=reasons,
            )
        )
    return tuple(projected)


def _manifest_acquisition_outcomes(
    response: FinancialProviderPeriodResponse,
    *,
    capability: FinancialCapability,
) -> tuple[FinancialAcquisitionOutcome, ...]:
    if response.subrequest_outcomes:
        return tuple(
            outcome.manifest_outcome(capability)
            for outcome in response.subrequest_outcomes
        )
    if response.kind is FinancialProviderResponseKind.UNAVAILABLE:
        assert response.reason is not None
        return (
            FinancialAcquisitionOutcome.unavailable(
                provider_id=response.provider_id,
                capability=capability,
                reason=response.reason,
            ),
        )
    assert response.response_artifact is not None
    return (
        FinancialAcquisitionOutcome.available(
            provider_id=response.provider_id,
            capability=capability,
            artifact_identity=response.response_artifact.artifact_identity,
        ),
    )


def _adapter_rejection_manifest_outcomes(
    response: FinancialProviderPeriodResponse,
    *,
    capability: FinancialCapability,
) -> tuple[FinancialAcquisitionOutcome, ...]:
    return tuple(
        FinancialAcquisitionOutcome.unavailable(
            provider_id=rejection.provider_id,
            capability=capability,
            reason=reason,
        )
        for rejection in response.adapter_row_rejections
        for reason in rejection.reasons
    )


def assess_financial_indicator_family_outcomes(
    *,
    request: FinancialIndicatorRoutingRequest,
    family_manifests: Sequence[FinancialAcquisitionManifest],
    publication_target_construction: (
        MainlandFinancialPeriodTargetConstruction | None
    ),
) -> tuple[
    tuple[FinancialIndicatorFamilyAssessment, ...],
    FinancialIndicatorAggregateAssessment,
]:
    """Assess every Ticket 04 family without collapsing sibling outcomes."""

    requirements = request.family_evidence_requirements
    if (
        request.contract_version
        not in {
            "financial-indicator-routing-request-v4",
            "financial-indicator-routing-request-v5",
        }
        or requirements is None
        or request.requested_analysis_scope is None
    ):
        raise ValueError("Ticket 04 family assessment requires a v4 request")
    manifest_by_family = {
        manifest.requested_ratio_family: manifest for manifest in family_manifests
    }
    if set(manifest_by_family) != set(request.ratio_families) or None in (
        manifest_by_family
    ):
        raise ValueError("Ticket 04 family manifests are incomplete")
    requirement_by_family = {item.family: item for item in requirements.families}

    family_assessments: list[FinancialIndicatorFamilyAssessment] = []
    strict_current_only_candidates = False
    for family in request.ratio_families:
        manifest = manifest_by_family[family]
        assert manifest is not None
        requirement = requirement_by_family[family]
        completeness = manifest.aggregate_completeness
        target_periods = tuple(completeness.target_reporting_period_ends)
        provider_candidates = {
            item.candidate.candidate_identity: item
            for item in manifest.provider_candidates
        }
        candidate_periods = {
            candidate_id: item.candidate.period_identity.period_end
            for candidate_id, item in provider_candidates.items()
        }
        conflict_candidate_ids = {
            candidate_id
            for conflict in manifest.conflicts
            for candidate_id in (
                conflict.selected_candidate_identity,
                conflict.conflicting_candidate_identity,
            )
        }
        conflicted_periods = tuple(
            sorted(
                {
                    candidate_periods[candidate_id]
                    for candidate_id in conflict_candidate_ids
                    if candidate_id in candidate_periods
                    and candidate_periods[candidate_id] in target_periods
                },
                reverse=True,
            )
        )
        selected_periods = tuple(
            sorted(
                {
                    candidate_periods[selection.candidate_identity]
                    for selection in manifest.selections
                    if selection.candidate_identity not in conflict_candidate_ids
                    and candidate_periods.get(selection.candidate_identity)
                    in target_periods
                },
                reverse=True,
            )
        )
        current_only_periods = tuple(
            sorted(
                {
                    candidate_periods[selection.candidate_identity]
                    for selection in manifest.selections
                    if selection.candidate_identity not in conflict_candidate_ids
                    and candidate_periods.get(selection.candidate_identity)
                    in target_periods
                    and selection.disposition == "current_only"
                },
                reverse=True,
            )
        )
        rejected_periods = tuple(
            sorted(
                {
                    candidate_periods[rejection.candidate_identity]
                    for rejection in manifest.rejected_periods
                    if rejection.candidate_identity not in conflict_candidate_ids
                    and candidate_periods.get(rejection.candidate_identity)
                    in target_periods
                },
                reverse=True,
            )
        )
        missing_periods = tuple(
            period
            for period in target_periods
            if period not in selected_periods and period not in conflicted_periods
        )
        complete = bool(target_periods) and not missing_periods and not (
            conflicted_periods
        ) and set(selected_periods) == set(target_periods)
        if complete:
            disposition = (
                FinancialEvidenceDisposition.DEGRADED
                if current_only_periods
                else FinancialEvidenceDisposition.STRICT_PIT_ELIGIBLE
            )
        elif conflicted_periods:
            disposition = FinancialEvidenceDisposition.CONFLICTED
        elif requirement.status is FinancialFamilyRequirementStatus.REQUIRED:
            disposition = FinancialEvidenceDisposition.INSUFFICIENT
        else:
            disposition = FinancialEvidenceDisposition.DEGRADED

        reason_set: set[FinancialFamilyAssessmentReason] = set()
        if current_only_periods:
            reason_set.add(FinancialFamilyAssessmentReason.CURRENT_ONLY_LINEAGE)
        if conflicted_periods:
            reason_set.add(
                FinancialFamilyAssessmentReason.CRITICAL_FAMILY_PERIOD_CONFLICT
            )
        if not complete and not conflicted_periods:
            reason_set.add(
                FinancialFamilyAssessmentReason.REQUIRED_FAMILY_INCOMPLETE
                if requirement.status is FinancialFamilyRequirementStatus.REQUIRED
                else FinancialFamilyAssessmentReason.OPTIONAL_FAMILY_INCOMPLETE
            )
        missing_bindings = tuple(
            FinancialMaterialPremiseFamilyBinding(
                premise_id=premise_id,
                family=family,
                affected_periods=missing_periods,
            )
            for premise_id in requirement.material_premise_ids
            if missing_periods
        )
        conflicted_bindings = tuple(
            FinancialMaterialPremiseFamilyBinding(
                premise_id=premise_id,
                family=family,
                affected_periods=conflicted_periods,
            )
            for premise_id in requirement.material_premise_ids
            if conflicted_periods
        )
        if missing_bindings:
            reason_set.add(
                FinancialFamilyAssessmentReason.MATERIAL_PREMISE_FAMILY_INCOMPLETE
            )
        if conflicted_bindings:
            reason_set.add(
                FinancialFamilyAssessmentReason.MATERIAL_PREMISE_FAMILY_CONFLICTED
            )
        coverage = tuple(
            sorted(
                (
                    FinancialFamilyPeriodCoverage(
                        candidate_identity=candidate_id,
                        artifact_identity=item.candidate.artifact.artifact_identity,
                        provider_id=item.candidate.artifact.dataset.provider_id,
                        period=item.candidate.period_identity.period_end,
                        disposition=item.assessment.disposition,
                        critical_coverage=item.assessment.critical_coverage,
                        core_coverage=item.assessment.core_coverage,
                        declared_critical_fields=(
                            item.assessment.declared_critical_fields
                        ),
                        declared_core_fields=item.assessment.declared_core_fields,
                        rejection_reasons=item.assessment.rejection_reasons,
                    )
                    for candidate_id, item in provider_candidates.items()
                ),
                key=lambda item: (item.period, item.candidate_identity),
                reverse=True,
            )
        )
        family_assessments.append(
            FinancialIndicatorFamilyAssessment.create(
                requested_analysis_scope=request.requested_analysis_scope,
                company_type=request.company_type,
                family=family,
                target_periods=target_periods,
                candidate_identities=tuple(sorted(provider_candidates)),
                selected_periods=selected_periods,
                current_only_periods=current_only_periods,
                rejected_periods=rejected_periods,
                conflicted_periods=conflicted_periods,
                missing_periods=missing_periods,
                coverage=coverage,
                requirement=requirement,
                missing_material_premise_bindings=missing_bindings,
                conflicted_material_premise_bindings=conflicted_bindings,
                complete=complete,
                disposition=disposition,
                typed_reasons=tuple(
                    reason
                    for reason in FinancialFamilyAssessmentReason
                    if reason in reason_set
                ),
                artifact_identities=tuple(
                    sorted(
                        item.artifact.artifact_identity for item in manifest.artifacts
                    )
                ),
                selection_identities=tuple(
                    sorted(item.selection_identity for item in manifest.selections)
                ),
                conflict_identities=tuple(
                    sorted(item.conflict_identity for item in manifest.conflicts)
                ),
            )
        )
        strict_current_only_candidates = strict_current_only_candidates or (
            request.requested_analysis_scope
            is FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
            and (
                manifest.availability_scope is FinancialAvailabilityScope.CURRENT_ONLY
                or any(
                    FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                    in rejection.reasons
                    for rejection in manifest.rejected_periods
                )
            )
        )

    family_assessment_tuple = tuple(family_assessments)
    complete_families = tuple(
        item.family for item in family_assessment_tuple if item.complete
    )
    missing_bindings = tuple(
        binding
        for item in family_assessment_tuple
        for binding in item.missing_material_premise_bindings
    )
    conflicted_bindings = tuple(
        binding
        for item in family_assessment_tuple
        for binding in item.conflicted_material_premise_bindings
    )
    required_conflicts = tuple(
        item
        for item in family_assessment_tuple
        if item.conflicted_periods
        and item.requirement.status is FinancialFamilyRequirementStatus.REQUIRED
    )
    required_incomplete = tuple(
        item
        for item in family_assessment_tuple
        if not item.complete
        and not item.conflicted_periods
        and item.requirement.status is FinancialFamilyRequirementStatus.REQUIRED
    )
    optional_incomplete = tuple(
        item
        for item in family_assessment_tuple
        if not item.complete
        and item.requirement.status is FinancialFamilyRequirementStatus.OPTIONAL
    )
    publication_failed = (
        publication_target_construction is not None
        and publication_target_construction.failure_reason is not None
    )
    if publication_failed or strict_current_only_candidates or not complete_families:
        aggregate_disposition = FinancialEvidenceDisposition.INSUFFICIENT
    elif required_conflicts or conflicted_bindings:
        aggregate_disposition = FinancialEvidenceDisposition.CONFLICTED
    elif required_incomplete or missing_bindings:
        aggregate_disposition = FinancialEvidenceDisposition.INSUFFICIENT
    elif optional_incomplete or any(
        item.current_only_periods for item in family_assessment_tuple
    ):
        aggregate_disposition = FinancialEvidenceDisposition.DEGRADED
    else:
        aggregate_disposition = FinancialEvidenceDisposition.STRICT_PIT_ELIGIBLE

    aggregate_reasons: set[FinancialFamilyAssessmentReason] = set()
    if publication_failed:
        aggregate_reasons.add(
            FinancialFamilyAssessmentReason.PUBLICATION_METADATA_UNPROVEN
        )
    if strict_current_only_candidates:
        aggregate_reasons.add(
            FinancialFamilyAssessmentReason.STRICT_PIT_CURRENT_ONLY_CANDIDATES
        )
    if not complete_families:
        aggregate_reasons.add(FinancialFamilyAssessmentReason.NO_COMPLETE_FAMILY)
    if required_incomplete:
        aggregate_reasons.add(
            FinancialFamilyAssessmentReason.REQUIRED_FAMILY_INCOMPLETE
        )
    if optional_incomplete:
        aggregate_reasons.add(
            FinancialFamilyAssessmentReason.OPTIONAL_FAMILY_INCOMPLETE
        )
    if missing_bindings:
        aggregate_reasons.add(
            FinancialFamilyAssessmentReason.MATERIAL_PREMISE_FAMILY_INCOMPLETE
        )
    if required_conflicts or any(
        item.conflicted_periods for item in family_assessment_tuple
    ):
        aggregate_reasons.add(
            FinancialFamilyAssessmentReason.CRITICAL_FAMILY_PERIOD_CONFLICT
        )
    if conflicted_bindings:
        aggregate_reasons.add(
            FinancialFamilyAssessmentReason.MATERIAL_PREMISE_FAMILY_CONFLICTED
        )
    if any(item.current_only_periods for item in family_assessment_tuple):
        aggregate_reasons.add(FinancialFamilyAssessmentReason.CURRENT_ONLY_LINEAGE)
    aggregate = FinancialIndicatorAggregateAssessment.create(
        requested_analysis_scope=request.requested_analysis_scope,
        company_type=request.company_type,
        requirements_identity=requirements.requirements_identity,
        requested_families=request.ratio_families,
        family_assessments=family_assessment_tuple,
        complete_families=complete_families,
        missing_material_premise_bindings=missing_bindings,
        conflicted_material_premise_bindings=conflicted_bindings,
        disposition=aggregate_disposition,
        typed_reasons=tuple(
            reason
            for reason in FinancialFamilyAssessmentReason
            if reason in aggregate_reasons
        ),
    )
    return family_assessment_tuple, aggregate


def financial_statement_evidence_disposition(
    result: FinancialStatementRoutingResult,
) -> FinancialEvidenceDisposition:
    if (
        result.publication_target_construction is not None
        and result.publication_target_construction.failure_reason is not None
    ):
        return FinancialEvidenceDisposition.INSUFFICIENT
    if (
        result.conflicted_annual_period_ends
        or result.conflicted_reporting_period_ends
        or result.manifest.conflicts
    ):
        return FinancialEvidenceDisposition.CONFLICTED
    if not result.manifest.aggregate_completeness.complete:
        return FinancialEvidenceDisposition.INSUFFICIENT
    if any(
        selection.disposition == "current_only"
        for selection in result.manifest.selections
    ):
        return FinancialEvidenceDisposition.CURRENT_ONLY
    return FinancialEvidenceDisposition.STRICT_PIT_ELIGIBLE


def financial_indicator_evidence_disposition(
    manifest: FinancialAcquisitionManifest,
    result: FinancialIndicatorRoutingResult,
    request: FinancialIndicatorRoutingRequest,
) -> FinancialEvidenceDisposition:
    if result.aggregate_family_assessment is not None:
        family = manifest.requested_ratio_family
        return next(
            assessment.disposition
            for assessment in result.family_assessments
            if assessment.family is family
        )
    if (
        result.publication_target_construction is not None
        and result.publication_target_construction.failure_reason is not None
    ):
        return FinancialEvidenceDisposition.INSUFFICIENT
    if manifest.conflicts:
        return FinancialEvidenceDisposition.CONFLICTED
    if manifest.aggregate_completeness.complete:
        if any(
            selection.disposition == "current_only"
            for selection in manifest.selections
        ):
            return (
                FinancialEvidenceDisposition.DEGRADED
                if request.requested_analysis_scope is not None
                else FinancialEvidenceDisposition.CURRENT_ONLY
            )
        return FinancialEvidenceDisposition.STRICT_PIT_ELIGIBLE
    if request.remaining_evidence_decision_ready and (
        result.current_profile_degraded or manifest.selections
    ):
        return FinancialEvidenceDisposition.DEGRADED
    return FinancialEvidenceDisposition.INSUFFICIENT


def finalize_statement_routing_result(
    result: FinancialStatementRoutingResult,
    *,
    physical_attempt_events: Sequence[ProviderSubrequestAttemptEvent],
) -> FinancialStatementRoutingResult:
    """Upgrade one qualified statement result to the run-scoped v2 manifest."""

    if result.manifest.contract_version == "financial-manifest-v2":
        return result
    finalized = result.manifest.finalize(
        physical_attempt_events=physical_attempt_events,
        disposition=financial_statement_evidence_disposition(result),
        rendered_value=render_statement_routing_result(result),
        publication_target_construction=result.publication_target_construction,
        eligibility_policy_version=(
            result.financial_eligibility_binding.eligibility_policy_version
            if result.financial_eligibility_binding is not None
            else None
        ),
        financial_eligibility_binding=result.financial_eligibility_binding,
    )
    payload = result.model_dump(mode="python")
    payload["manifest"] = finalized
    return FinancialStatementRoutingResult.model_validate(payload)


def finalize_indicator_routing_result(
    result: FinancialIndicatorRoutingResult,
    *,
    request: FinancialIndicatorRoutingRequest,
    physical_attempt_events: Sequence[ProviderSubrequestAttemptEvent],
) -> FinancialIndicatorRoutingResult:
    """Upgrade all families in one qualified indicator result to v2."""

    if all(
        manifest.contract_version == "financial-manifest-v2"
        for manifest in result.family_manifests
    ):
        return result
    rendered_value = render_indicator_routing_result(result)
    finalized = tuple(
        manifest.finalize(
            physical_attempt_events=physical_attempt_events,
            disposition=financial_indicator_evidence_disposition(
                manifest,
                result,
                request,
            ),
            rendered_value=rendered_value,
            publication_target_construction=(
                result.publication_target_construction
            ),
            requested_analysis_scope=request.requested_analysis_scope,
            eligibility_policy_version=(
                request.eligibility_policy_version
                if request.requested_analysis_scope is not None
                else None
            ),
            financial_eligibility_binding=(
                result.financial_eligibility_binding
            ),
        )
        for manifest in result.family_manifests
    )
    payload = result.model_dump(mode="python")
    payload["family_manifests"] = finalized
    return FinancialIndicatorRoutingResult.model_validate(payload)


def render_statement_routing_result(
    result: FinancialStatementRoutingResult,
) -> str:
    """Render only final selected normalized periods for the model boundary."""

    selection_by_candidate = {
        selection.candidate_identity: selection
        for selection in result.manifest.selections
    }
    if (
        result.publication_target_construction is not None
        and result.publication_target_construction.failure_reason is not None
    ):
        status = "insufficient_evidence"
    elif (
        result.conflicted_annual_period_ends
        or result.conflicted_reporting_period_ends
    ):
        status = "conflicted"
    elif result.missing_annual_period_ends or result.missing_reporting_period_ends:
        status = "insufficient_evidence"
    elif any(
        selection.disposition == "current_only"
        for selection in result.manifest.selections
    ):
        status = "current_only_degraded"
    else:
        status = "complete"
    payload = {
        "contract_version": (
            "financial-selected-periods-v2"
            if result.publication_target_construction is not None
            else "financial-selected-periods-v1"
        ),
        "status": status,
        "statement_type": result.manifest.requested_statement_type.value,
        "periods": [
            {
                "period_end": candidate.period_identity.period_end.isoformat(),
                "frequency": candidate.period_identity.frequency.value,
                "provider": candidate.artifact.dataset.provider_id,
                "selection_disposition": (
                    selection_by_candidate[
                        candidate.candidate_identity
                    ].disposition
                ),
                "strict_pit_eligible": (
                    selection_by_candidate[
                        candidate.candidate_identity
                    ].strict_pit_eligible
                ),
                "filing_metadata": {
                    "ann_date": (
                        candidate.filing_metadata.ann_date.isoformat()
                        if candidate.filing_metadata.ann_date is not None
                        else None
                    ),
                    "f_ann_date": (
                        candidate.filing_metadata.f_ann_date.isoformat()
                        if candidate.filing_metadata.f_ann_date is not None
                        else None
                    ),
                    "report_type": candidate.filing_metadata.report_type,
                    "comp_type": candidate.filing_metadata.comp_type,
                    "update_flag": candidate.filing_metadata.update_flag,
                },
                "values": {
                    field.normalized_field: {
                        "value": field.normalized_value,
                        "unit": field.normalized_unit,
                    }
                    for field in candidate.fields
                    if field.normalized_value is not None
                },
            }
            for candidate in result.selected_candidates
        ],
        "missing_annual_periods": [
            period.isoformat() for period in result.missing_annual_period_ends
        ],
        "missing_reporting_periods": [
            period.isoformat() for period in result.missing_reporting_period_ends
        ],
        "conflicted_annual_periods": [
            period.isoformat() for period in result.conflicted_annual_period_ends
        ],
        "conflicted_reporting_periods": [
            period.isoformat()
            for period in result.conflicted_reporting_period_ends
        ],
    }
    if result.publication_target_construction is not None:
        payload["publication_target_construction"] = (
            result.publication_target_construction.model_dump(mode="json")
        )
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def render_indicator_routing_result(
    result: FinancialIndicatorRoutingResult,
) -> str:
    """Render selected normalized ratio periods and bounded degradation only."""

    selections = {
        selection.candidate_identity: selection
        for manifest in result.family_manifests
        for selection in manifest.selections
    }
    requested_analysis_scope = result.requested_analysis_scope
    availability_reason = next(
        (
            reason
            for manifest in result.family_manifests
            for reason in (
                (
                    manifest.availability_reason,
                    *(
                        selection.availability_reason
                        for selection in manifest.selections
                    ),
                    *(
                        rejection_reason
                        for rejection in manifest.rejected_periods
                        for rejection_reason in rejection.reasons
                        if rejection_reason
                        in {
                            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT,
                            FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED,
                        }
                    ),
                )
            )
            if reason is not None
        ),
        None,
    )
    if result.aggregate_family_assessment is not None:
        status = {
            FinancialEvidenceDisposition.DEGRADED: "degraded_evidence",
            FinancialEvidenceDisposition.INSUFFICIENT: "insufficient_evidence",
            FinancialEvidenceDisposition.CONFLICTED: "conflicted_evidence",
            FinancialEvidenceDisposition.STRICT_PIT_ELIGIBLE: "complete",
            FinancialEvidenceDisposition.CURRENT_ONLY: "current_only_degraded",
        }[result.aggregate_family_assessment.disposition]
    elif (
        (
            result.publication_target_construction is not None
            and result.publication_target_construction.failure_reason is not None
        )
        or (
            requested_analysis_scope
            is FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
            and availability_reason is not None
        )
    ):
        status = "insufficient_evidence"
    elif result.missing_periods_by_family and result.current_profile_degraded:
        status = "insufficient_history_with_degraded_current_profile"
    elif result.missing_periods_by_family:
        status = "incomplete"
    elif any(
        selection.disposition == "current_only"
        for selection in selections.values()
    ):
        status = "current_only_degraded"
    else:
        status = "complete"
    rendered_periods: list[dict[str, object]] = []
    for candidate in result.selected_candidates:
        selection = selections[candidate.candidate_identity]
        rendered_period: dict[str, object] = {
            "ratio_family": candidate.period_identity.ratio_family.value,
            "period_end": candidate.period_identity.period_end.isoformat(),
            "provider": candidate.artifact.dataset.provider_id,
            "selection_disposition": selection.disposition,
            "strict_pit_eligible": selection.strict_pit_eligible,
            "values": {
                field.normalized_field: {
                    "value": field.normalized_value,
                    "unit": field.normalized_unit,
                }
                for field in candidate.fields
                if field.normalized_value is not None
            },
        }
        if selection.contract_version == "financial-period-selection-v2":
            assert selection.availability_scope is not None
            assert selection.availability_basis is not None
            assert selection.ann_date is not None
            rendered_period.update(
                {
                    "availability_scope": selection.availability_scope.value,
                    "availability_basis": selection.availability_basis.value,
                    "availability_reason": (
                        selection.availability_reason.value
                        if selection.availability_reason is not None
                        else None
                    ),
                    "ann_date": selection.ann_date.isoformat(),
                    "f_ann_date": (
                        selection.f_ann_date.isoformat()
                        if selection.f_ann_date is not None
                        else None
                    ),
                    "eligibility_policy_version": (
                        selection.eligibility_policy_version
                    ),
                }
            )
        rendered_periods.append(rendered_period)
    payload = {
        "contract_version": (
            "financial-selected-indicators-v2"
            if result.publication_target_construction is not None
            else "financial-selected-indicators-v1"
        ),
        "status": status,
        "complete_families": [
            family.value for family in result.complete_families
        ],
        "periods": rendered_periods,
        "missing_periods_by_family": {
            family.value: [period.isoformat() for period in periods]
            for family, periods in result.missing_periods_by_family
        },
        "current_profile_provider": result.current_profile_provider,
    }
    if result.aggregate_family_assessment is not None:
        payload.update(
            {
                "family_assessments": [
                    item.model_dump(mode="json")
                    for item in result.family_assessments
                ],
                "aggregate_family_assessment": (
                    result.aggregate_family_assessment.model_dump(mode="json")
                ),
            }
        )
    if requested_analysis_scope is not None:
        payload.update(
            {
                "requested_analysis_scope": requested_analysis_scope.value,
                "rendering_scope": (
                    "current_analysis"
                    if requested_analysis_scope
                    is FinancialAvailabilityScope.CURRENT_ONLY
                    else "strict_pit"
                ),
                "availability_reason": (
                    availability_reason.value
                    if availability_reason is not None
                    else None
                ),
            }
        )
    if result.publication_target_construction is not None:
        payload["publication_target_construction"] = (
            result.publication_target_construction.model_dump(mode="json")
        )
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "FinancialAdapterRowRejection",
    "FinancialCompanyTypeEvidenceProviderResponse",
    "FinancialCompanyTypeEvidenceProviderResponseKind",
    "FinancialCompanyTypeEvidenceRoutingRequest",
    "FinancialCompanyTypeEvidenceRoutingResult",
    "FinancialCompanyTypeEvidenceSource",
    "FinancialCompanyTypeEvidenceSourceProjectionKind",
    "FinancialCapabilityProviderResponseKind",
    "FinancialCapabilityProviderResponse",
    "FinancialCapabilityRoutingRequest",
    "FinancialCapabilityRoutingResult",
    "FinancialProviderGapRequest",
    "FinancialIndicatorProviderGapRequest",
    "FinancialIndicatorRoutingRequest",
    "FinancialIndicatorRoutingResult",
    "FinancialCapabilityAuthorityPolicy",
    "FinancialProviderPeriodResponse",
    "FinancialProviderResponseKind",
    "FinancialSubrequestAcquisitionOutcome",
    "FinancialStatementRoutingRequest",
    "FinancialStatementRoutingResult",
    "MainlandFinancialCapabilityRouter",
    "financial_period_response_from_indicator_adapter",
    "financial_period_response_from_statement_adapter",
    "render_indicator_routing_result",
    "render_statement_routing_result",
]

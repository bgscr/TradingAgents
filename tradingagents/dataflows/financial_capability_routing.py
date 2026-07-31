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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAcquisitionManifest,
    FinancialAcquisitionOutcome,
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialCriticalValueConflict,
    FinancialListingProvenance,
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
    FinancialStatementType,
    assess_financial_history_coverage,
    assess_financial_period_candidate,
    assess_financial_ratio_history_coverage,
    compare_financial_period_candidates,
    mark_financial_period_company_type_conflicted,
    mark_financial_period_conflicted,
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
        subrequest_outcomes: Sequence[FinancialSubrequestAcquisitionOutcome] = (),
    ) -> FinancialProviderPeriodResponse:
        return cls(
            provider_id=provider_id,
            kind=FinancialProviderResponseKind.UNAVAILABLE,
            reason=AcquisitionUnavailableReason(reason),
            subrequest_keys=tuple(subrequest_keys),
            physical_attempt_ids=tuple(physical_attempt_ids),
            subrequest_outcomes=tuple(subrequest_outcomes),
        )

    @model_validator(mode="after")
    def _validate_response(self) -> FinancialProviderPeriodResponse:
        if len(self.subrequest_keys) != len(set(self.subrequest_keys)):
            raise ValueError("provider response subrequest keys must be unique")
        if len(self.physical_attempt_ids) != len(set(self.physical_attempt_ids)):
            raise ValueError("provider response physical attempts must be unique")
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
            if any(
                candidate.artifact.dataset.provider_id != self.provider_id
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
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    missing_periods_by_family: tuple[
        tuple[FinancialRatioFamily, tuple[date, ...]],
        ...,
    ]

    @model_validator(mode="after")
    def _validate_gaps(self) -> FinancialIndicatorProviderGapRequest:
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

    contract_version: Literal["financial-statement-routing-request-v1"] = (
        "financial-statement-routing-request-v1"
    )
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
        _validate_mainland_financial_identity(
            self.instrument_identity,
            currency=self.currency,
        )
        return self


class FinancialIndicatorRoutingRequest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-indicator-routing-request-v1"] = (
        "financial-indicator-routing-request-v1"
    )
    instrument_identity: InstrumentIdentityEvidence
    as_of_date: date
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    ratio_families: tuple[FinancialRatioFamily, ...]
    eligible_reporting_period_ends: tuple[date, ...]

    @model_validator(mode="after")
    def _validate_request(self) -> FinancialIndicatorRoutingRequest:
        canonical = tuple(
            family for family in FinancialRatioFamily if family in self.ratio_families
        )
        if not canonical or self.ratio_families != canonical:
            raise ValueError("indicator families must be non-empty, unique, and canonical")
        if self.company_type is FinancialCompanyType.UNKNOWN:
            raise ValueError("indicator routing requires a resolved company type")
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

    @model_validator(mode="after")
    def _validate_result(self) -> FinancialStatementRoutingResult:
        selection_ids = {item.candidate_identity for item in self.manifest.selections}
        if selection_ids != {
            item.candidate_identity for item in self.selected_candidates
        }:
            raise ValueError("statement routing candidates contradict manifest selections")
        if len(self.physical_attempt_ids) != len(set(self.physical_attempt_ids)):
            raise ValueError("statement routing attempts must be unique")
        return self


class FinancialIndicatorRoutingResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-indicator-routing-result-v1"] = (
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

    @model_validator(mode="after")
    def _validate_result(self) -> FinancialIndicatorRoutingResult:
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
    ) -> None:
        if routing_plan.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1:
            raise ValueError("qualified financial router requires qualified_v1")
        self._routing_plan = routing_plan
        self._statement_sources = dict(statement_sources)
        self._indicator_sources = dict(indicator_sources or {})
        self._capability_sources = {
            capability: dict(sources)
            for capability, sources in (capability_sources or {}).items()
        }
        if any(
            capability not in _ROUTABLE_DATASET_CAPABILITIES
            for capability in self._capability_sources
        ):
            raise ValueError("capability source map contains an unsupported capability")

    @property
    def routing_plan(self) -> MainlandCapabilityRoutingPlan:
        return self._routing_plan

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
        target_annual = tuple(
            sorted(set(request.eligible_annual_period_ends), reverse=True)[:5]
        )
        target_reporting = tuple(
            sorted(set(request.eligible_reporting_period_ends), reverse=True)[:8]
        )
        target_keys = {
            *(("annual", item) for item in target_annual),
            *(("reporting", item) for item in target_reporting),
        }

        artifacts: dict[str, FinancialProviderArtifactEntry] = {}
        acquisition_outcomes: list[FinancialAcquisitionOutcome] = []
        provider_candidates: dict[str, FinancialProviderCandidate] = {}
        rejected_periods: dict[str, FinancialRejectedPeriod] = {}
        selections: dict[tuple[str, date], FinancialPeriodSelection] = {}
        selected_candidates: dict[tuple[str, date], FinancialPeriodCandidate] = {}
        selected_assessments: dict[
            tuple[str, date], FinancialPeriodCompletenessAssessment
        ] = {}
        overlaps: list[FinancialPeriodOverlapFinding] = []
        conflicts: list[FinancialCriticalValueConflict] = []
        conflicted_keys: set[tuple[str, date]] = set()
        provider_calls: list[FinancialProviderRouteCall] = []
        physical_attempt_ids: list[str] = []

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
                    subrequest_outcomes=response.subrequest_outcomes,
                    adapter_row_rejections=response.adapter_row_rejections,
                )
            )
            for attempt_id in response.physical_attempt_ids:
                if attempt_id not in physical_attempt_ids:
                    physical_attempt_ids.append(attempt_id)
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
                continue

            assert response.response_artifact is not None
            for artifact in response.retained_artifacts:
                artifacts.setdefault(
                    artifact.artifact_identity,
                    FinancialProviderArtifactEntry(artifact=artifact),
                )
            for candidate in response.candidates:
                key = _statement_candidate_target_key(
                    candidate,
                    target_annual=target_annual,
                    target_reporting=target_reporting,
                )
                assessment = assess_financial_period_candidate(
                    candidate,
                    requested_capability=FinancialCapability.STATEMENT,
                    requested_statement_type=request.statement_type,
                    requested_ratio_family=None,
                    expected_instrument_identity=request.instrument_identity,
                    expected_frequency=candidate.period_identity.frequency,
                    expected_currency=request.currency,
                    expected_consolidation_scope=request.consolidation_scope,
                    pit_as_of_date=request.as_of_date,
                )
                if (
                    assessment.usable_for_current_analysis
                    and candidate.period_identity.company_type is not request.company_type
                ):
                    assessment = mark_financial_period_company_type_conflicted(assessment)
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

                overlap, candidate_conflicts = compare_financial_period_candidates(
                    selected,
                    candidate,
                )
                overlaps.append(overlap)
                conflicts.extend(candidate_conflicts)
                if not candidate_conflicts and overlap.disposition == "equivalent_unselected":
                    continue
                conflicted_keys.add(key)
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
        )
        return FinancialStatementRoutingResult(
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
        )

    def route_indicators(
        self,
        request: FinancialIndicatorRoutingRequest,
    ) -> FinancialIndicatorRoutingResult:
        """Select ratio periods per family and request only unresolved gaps."""

        route = self._routing_plan.route_for(
            MainlandCapability.FINANCIAL_INDICATORS
        )
        target_periods = tuple(
            sorted(set(request.eligible_reporting_period_ends), reverse=True)[:8]
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
        rejected_periods: dict[str, FinancialRejectedPeriod] = {}
        artifacts: dict[str, FinancialProviderArtifactEntry] = {}
        acquisition_outcomes: list[FinancialAcquisitionOutcome] = []
        overlaps: list[FinancialPeriodOverlapFinding] = []
        conflicts: list[FinancialCriticalValueConflict] = []
        conflicted_keys: set[tuple[FinancialRatioFamily, date]] = set()
        provider_calls: list[FinancialIndicatorProviderRouteCall] = []
        physical_attempt_ids: list[str] = []
        current_profile_provider: Literal["yfinance"] | None = None

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
                    subrequest_outcomes=response.subrequest_outcomes,
                    adapter_row_rejections=response.adapter_row_rejections,
                )
            )
            for attempt_id in response.physical_attempt_ids:
                if attempt_id not in physical_attempt_ids:
                    physical_attempt_ids.append(attempt_id)
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
                continue

            assert response.response_artifact is not None
            for artifact in response.retained_artifacts:
                artifacts.setdefault(
                    artifact.artifact_identity,
                    FinancialProviderArtifactEntry(artifact=artifact),
                )
            if response.degraded_current_profile:
                current_profile_provider = "yfinance"
            for candidate in response.candidates:
                identity = candidate.period_identity
                ratio_family = identity.ratio_family
                if ratio_family is None or ratio_family not in request.ratio_families:
                    continue
                assessment = assess_financial_period_candidate(
                    candidate,
                    requested_capability=FinancialCapability.RATIO_FAMILY,
                    requested_statement_type=None,
                    requested_ratio_family=ratio_family,
                    expected_instrument_identity=request.instrument_identity,
                    expected_frequency=FinancialReportingFrequency.QUARTERLY,
                    expected_currency=request.currency,
                    expected_consolidation_scope=request.consolidation_scope,
                    pit_as_of_date=request.as_of_date,
                )
                if (
                    assessment.usable_for_current_analysis
                    and identity.company_type is not request.company_type
                ):
                    assessment = mark_financial_period_company_type_conflicted(
                        assessment
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
                overlap, candidate_conflicts = compare_financial_period_candidates(
                    selected,
                    candidate,
                )
                overlaps.append(overlap)
                conflicts.extend(candidate_conflicts)
                if (
                    not candidate_conflicts
                    and overlap.disposition == "equivalent_unselected"
                ):
                    continue
                conflicted_keys.add(key)
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

        family_assessments: dict[
            FinancialRatioFamily,
            FinancialRatioHistoryCompletenessAssessment,
        ] = {}
        family_manifests: list[FinancialAcquisitionManifest] = []
        for family in request.ratio_families:
            completeness = assess_financial_ratio_history_coverage(
                instrument_identity=request.instrument_identity,
                ratio_family=family,
                company_type=request.company_type,
                consolidation_scope=request.consolidation_scope,
                currency=request.currency,
                assessments=tuple(
                    assessment
                    for (candidate_family, _), assessment in selected_assessments.items()
                    if candidate_family is family
                ),
                eligible_reporting_period_ends=target_periods,
            )
            family_assessments[family] = completeness
            family_candidates = tuple(
                item
                for item in provider_candidates.values()
                if item.candidate.period_identity.ratio_family is family
            )
            family_candidate_ids = {
                item.candidate.candidate_identity for item in family_candidates
            }
            family_selections = tuple(
                FinancialPeriodSelection.create(
                    candidate=candidate,
                    assessment=selected_assessments[(family, period)],
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
            family_manifests.append(
                FinancialAcquisitionManifest.create(
                    capability_routing_plan_signature=self._routing_plan.plan_signature,
                    instrument_identity=request.instrument_identity,
                    requested_capability=FinancialCapability.RATIO_FAMILY,
                    requested_statement_type=None,
                    requested_ratio_family=family,
                    artifacts=tuple(artifacts.values()),
                    acquisition_outcomes=tuple(acquisition_outcomes),
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
                )
            )

        return FinancialIndicatorRoutingResult(
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
                if family_assessments[family].complete
            ),
            missing_periods_by_family=tuple(
                (
                    family,
                    family_assessments[family].missing_reporting_period_ends,
                )
                for family in request.ratio_families
                if family_assessments[family].missing_reporting_period_ends
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
        )


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


def render_statement_routing_result(
    result: FinancialStatementRoutingResult,
) -> str:
    """Render only final selected normalized periods for the model boundary."""

    selection_by_candidate = {
        selection.candidate_identity: selection
        for selection in result.manifest.selections
    }
    if (
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
        "contract_version": "financial-selected-periods-v1",
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
    if result.missing_periods_by_family and result.current_profile_degraded:
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
    payload = {
        "contract_version": "financial-selected-indicators-v1",
        "status": status,
        "complete_families": [
            family.value for family in result.complete_families
        ],
        "periods": [
            {
                "ratio_family": candidate.period_identity.ratio_family.value,
                "period_end": candidate.period_identity.period_end.isoformat(),
                "provider": candidate.artifact.dataset.provider_id,
                "selection_disposition": selections[
                    candidate.candidate_identity
                ].disposition,
                "strict_pit_eligible": selections[
                    candidate.candidate_identity
                ].strict_pit_eligible,
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
        "missing_periods_by_family": {
            family.value: [period.isoformat() for period in periods]
            for family, periods in result.missing_periods_by_family
        },
        "current_profile_provider": result.current_profile_provider,
    }
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "FinancialAdapterRowRejection",
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

"""Provider-neutral acquisition of metadata required by financial eligibility."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from threading import Event
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from tradingagents.asset_configuration import RunAssetConfiguration
from tradingagents.capability_routing import (
    CAPABILITY_ROUTING_PLAN_VERSION,
    MainlandCapability,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityRoutingRequest,
    FinancialCompanyTypeEvidenceRoutingRequest,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialCompanyTypeResolutionMethod,
    FinancialConsolidationScope,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialCompanyTypeEvidenceBinding,
    FinancialCompanyTypeEvidenceProjection,
    FinancialEligibilityAcquisitionFailureReason,
    FinancialEligibilityAcquisitionResult,
    FinancialEligibilityMetadataBinding,
    FinancialListingMetadataProjection,
    build_financial_eligibility_acquisition_context,
    derive_mainland_financial_acquisition_period_supersets,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MainlandFinancialEligibilityProjectionBinding,
)
from tradingagents.evidence import InstrumentIdentityEvidence

_CLOSED_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    revalidate_instances="always",
)
_MAINLAND_TIMEZONE = ZoneInfo("Asia/Shanghai")


class FinancialEligibilityMetadataAcquisitionRequest(BaseModel):
    """Authoritative and supplied bindings needed to build one complete context."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["financial-eligibility-metadata-acquisition-request-v1"] = (
        "financial-eligibility-metadata-acquisition-request-v1"
    )
    canonical_run_id: str
    run_id: str
    asset_configuration: RunAssetConfiguration
    asset_configuration_signature: str
    instrument_identity: InstrumentIdentityEvidence
    authoritative_as_of_date: date
    as_of_date: date
    authoritative_availability_scope: FinancialAvailabilityScope
    availability_scope: FinancialAvailabilityScope
    acquisition_purpose: Literal["foreground_current_analysis"] = "foreground_current_analysis"
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding
    routing_plan_signature: str
    currency: str
    consolidation_scope: FinancialConsolidationScope


def _failed(
    reason: FinancialEligibilityAcquisitionFailureReason,
    *,
    resolution: FinancialCompanyTypeResolution | None = None,
) -> FinancialEligibilityAcquisitionResult:
    return FinancialEligibilityAcquisitionResult.failed(
        reason,
        company_type_resolution=resolution,
    )


def _resolution_failure(
    resolution: FinancialCompanyTypeResolution,
) -> FinancialEligibilityAcquisitionResult | None:
    if resolution.contradictory:
        return _failed(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_CONTRADICTORY,
            resolution=resolution,
        )
    if (
        resolution.company_type is FinancialCompanyType.UNKNOWN
        or resolution.method is FinancialCompanyTypeResolutionMethod.UNRESOLVED
    ):
        if resolution.ambiguous and resolution.classifier_inputs:
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS,
                resolution=resolution,
            )
        return _failed(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_UNRESOLVED,
            resolution=resolution,
        )
    if resolution.ambiguous:
        return _failed(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS,
            resolution=resolution,
        )
    return None


def _resolver_arguments(
    evidence: FinancialCompanyTypeEvidenceBinding,
) -> tuple[dict[str, str], tuple[str, ...]]:
    metadata: dict[str, str] = {}
    present_fields: list[str] = []
    for item in evidence.normalized_resolver_inputs:
        if item.key == "present_field":
            present_fields.append(item.value)
            continue
        if not item.key.startswith("metadata:"):
            raise ValueError("company-type evidence contains an unsupported input")
        key = item.key.removeprefix("metadata:")
        if key in metadata and metadata[key] != item.value:
            raise ValueError("company-type evidence contains contradictory metadata")
        metadata[key] = item.value
    return metadata, tuple(sorted(set(present_fields)))


class FinancialEligibilityMetadataAcquirer:
    """Acquire prerequisites and atomically construct one Ticket 02A context."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        router: MainlandFinancialCapabilityRouter,
        stop_signal: Event,
    ) -> None:
        self._routing_plan = routing_plan
        self._router = router
        self._stop_signal = stop_signal

    def __repr__(self) -> str:
        return "FinancialEligibilityMetadataAcquirer(plan_bound=True)"

    def _interrupted(self) -> bool:
        return self._stop_signal.is_set()

    def _binding(
        self,
        request: FinancialEligibilityMetadataAcquisitionRequest,
    ) -> FinancialEligibilityMetadataBinding:
        return FinancialEligibilityMetadataBinding(
            instrument_identity=request.instrument_identity,
            as_of_date=request.as_of_date,
            availability_scope=request.availability_scope,
            acquisition_purpose=request.acquisition_purpose,
            financial_eligibility_binding=request.financial_eligibility_binding,
            routing_plan_signature=request.routing_plan_signature,
        )

    def _retained_company_type_evidence(
        self,
        *,
        binding: FinancialEligibilityMetadataBinding,
        retained: Sequence[FinancialCompanyTypeEvidenceProjection | Mapping[str, object]],
    ) -> FinancialCompanyTypeAcquisitionEvidenceBinding | None:
        route = self._routing_plan.route_for(MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE)
        by_provider: dict[str, FinancialCompanyTypeAcquisitionEvidenceBinding] = {}
        for value in retained:
            try:
                projection = FinancialCompanyTypeEvidenceProjection.model_validate(value)
            except (TypeError, ValueError):
                continue
            evidence = projection.evidence
            if (
                projection.metadata_binding != binding
                or evidence.provider_id not in route
                or evidence.retrieved_at.astimezone(_MAINLAND_TIMEZONE).date() != binding.as_of_date
            ):
                continue
            current = by_provider.get(evidence.provider_id)
            if current is not None and current != evidence:
                return None
            by_provider[evidence.provider_id] = evidence
        return next(
            (by_provider[provider] for provider in route if provider in by_provider),
            None,
        )

    @staticmethod
    def _retained_listing_metadata(
        *,
        binding: FinancialEligibilityMetadataBinding,
        retained: Sequence[FinancialListingMetadataProjection | Mapping[str, object]],
    ) -> FinancialListingMetadataProjection | None:
        compatible: list[FinancialListingMetadataProjection] = []
        for value in retained:
            try:
                projection = FinancialListingMetadataProjection.model_validate(value)
            except (TypeError, ValueError):
                continue
            if (
                projection.metadata_binding == binding
                and projection.metadata.listing_provenance.observed_at.astimezone(
                    _MAINLAND_TIMEZONE
                ).date()
                == binding.as_of_date
            ):
                compatible.append(projection)
        unique = {item.metadata.model_dump_json(): item for item in compatible}
        if len(unique) != 1:
            return None
        return next(iter(unique.values()))

    def acquire(
        self,
        request: FinancialEligibilityMetadataAcquisitionRequest,
        *,
        retained_company_type_evidence: Sequence[
            FinancialCompanyTypeEvidenceProjection | Mapping[str, object]
        ] = (),
        retained_listing_metadata: Sequence[
            FinancialListingMetadataProjection | Mapping[str, object]
        ] = (),
    ) -> FinancialEligibilityAcquisitionResult:
        """Return exactly one complete context or one bounded typed failure."""

        if self._interrupted():
            return _failed(FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION)
        try:
            if (
                self._routing_plan.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
                or self._routing_plan.plan_version != CAPABILITY_ROUTING_PLAN_VERSION
                or self._router.routing_plan != self._routing_plan
                or request.run_id != request.canonical_run_id
                or request.asset_configuration_signature
                != request.asset_configuration.asset_configuration_signature
                or request.instrument_identity != request.asset_configuration.instrument_identity
                or request.authoritative_as_of_date != request.as_of_date
                or request.authoritative_availability_scope is not request.availability_scope
                or request.financial_eligibility_binding
                != self._routing_plan.financial_eligibility_binding
                or request.routing_plan_signature != self._routing_plan.plan_signature
                or request.currency != request.instrument_identity.currency
            ):
                raise ValueError("metadata acquisition plan binding mismatch")
            binding = self._binding(request)
        except (AttributeError, TypeError, ValueError):
            return _failed(FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH)

        evidence = self._retained_company_type_evidence(
            binding=binding,
            retained=retained_company_type_evidence,
        )
        if evidence is None:
            try:
                routed = self._router.route_company_type_evidence(
                    FinancialCompanyTypeEvidenceRoutingRequest(
                        metadata_binding=binding,
                    ),
                    interrupted=self._interrupted,
                )
                evidence = routed.selected_evidence
            except Exception:
                evidence = None
            if evidence is None:
                return _failed(
                    FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
                    if self._interrupted()
                    else FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE
                )
        if evidence.retrieved_at.astimezone(_MAINLAND_TIMEZONE).date() != binding.as_of_date:
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE
            )
        if self._interrupted():
            return _failed(FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION)
        try:
            classifier_metadata, present_fields = _resolver_arguments(evidence)
            resolution = resolve_financial_company_type(
                provider_declared_type=evidence.provider_declared_type,
                provider_declaration_qualified=evidence.provider_declaration_qualified,
                classifier_metadata=classifier_metadata,
                present_fields=present_fields,
            )
            if (
                resolution.classifier_inputs != evidence.normalized_resolver_inputs
                or resolution.provider_declared_type is not evidence.provider_declared_type
                or resolution.provider_declaration_qualified
                != evidence.provider_declaration_qualified
            ):
                raise ValueError("resolver result contradicts evidence binding")
        except (TypeError, ValueError):
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE
            )
        failure = _resolution_failure(resolution)
        if failure is not None:
            return failure
        if self._interrupted():
            return _failed(FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION)

        listing_projection = self._retained_listing_metadata(
            binding=binding,
            retained=retained_listing_metadata,
        )
        if listing_projection is None:
            try:
                routed_listing = self._router.route_capability(
                    FinancialCapabilityRoutingRequest(
                        instrument_identity=request.instrument_identity,
                        capability=MainlandCapability.ISSUER_LIFECYCLE,
                        range_start=request.as_of_date,
                        as_of_date=request.as_of_date,
                    )
                )
                metadata = routed_listing.listing_metadata
                listing_projection = (
                    FinancialListingMetadataProjection(
                        metadata_binding=binding,
                        metadata=metadata,
                    )
                    if metadata is not None
                    else None
                )
            except Exception:
                listing_projection = None
            if listing_projection is None:
                return _failed(
                    FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
                    if self._interrupted()
                    else FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE
                )
        if self._interrupted():
            return _failed(FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION)
        listing = listing_projection.metadata
        if (
            listing.listing_provenance.observed_at.astimezone(_MAINLAND_TIMEZONE).date()
            != binding.as_of_date
        ):
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE
            )
        try:
            if self._routing_plan.financial_eligibility_policy is None:
                raise ValueError("financial eligibility policy is unavailable")
            annual, reporting = derive_mainland_financial_acquisition_period_supersets(
                run_as_of_date=request.authoritative_as_of_date,
                listing_date=listing.listing_date,
                policy=self._routing_plan.financial_eligibility_policy,
            )
        except (AttributeError, TypeError, ValueError):
            return _failed(FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH)
        return build_financial_eligibility_acquisition_context(
            canonical_run_id=request.canonical_run_id,
            run_id=request.run_id,
            asset_configuration=request.asset_configuration,
            asset_configuration_signature=request.asset_configuration_signature,
            instrument_identity=request.instrument_identity,
            authoritative_as_of_date=request.authoritative_as_of_date,
            as_of_date=request.as_of_date,
            authoritative_availability_scope=request.authoritative_availability_scope,
            availability_scope=request.availability_scope,
            acquisition_purpose=request.acquisition_purpose,
            financial_eligibility_binding=request.financial_eligibility_binding,
            routing_plan=self._routing_plan,
            routing_plan_signature=request.routing_plan_signature,
            company_type_resolution=resolution,
            company_type_evidence=evidence,
            listing_date=listing.listing_date,
            listing_provenance=listing.listing_provenance,
            currency=request.currency,
            consolidation_scope=request.consolidation_scope,
            acquisition_annual_period_ends=annual,
            acquisition_reporting_period_ends=reporting,
            interrupted=self._interrupted(),
        )


__all__ = [
    "FinancialEligibilityMetadataAcquirer",
    "FinancialEligibilityMetadataAcquisitionRequest",
]

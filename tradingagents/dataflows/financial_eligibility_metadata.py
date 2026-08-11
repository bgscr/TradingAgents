"""Provider-neutral acquisition of metadata required by financial eligibility."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from threading import Event
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.asset_configuration import RunAssetConfiguration
from tradingagents.capability_routing import (
    CAPABILITY_ROUTING_PLAN_VERSION,
    CAPABILITY_ROUTING_PLAN_VERSION_1_3,
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
from tradingagents.dataflows.financial_temporal_eligibility import (
    FinancialMetadataTemporalEligibilityDecision,
    MarketSessionEligibilityBinding,
    TemporalEligibilityMode,
)
from tradingagents.dataflows.financial_temporal_proof_projection import (
    FinancialEligibilityTemporalEvaluationTransaction,
)
from tradingagents.evidence import InstrumentIdentityEvidence

_CLOSED_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    revalidate_instances="always",
)


class FinancialEligibilityMetadataAcquisitionRequest(BaseModel):
    """Authoritative and supplied bindings needed to build one complete context."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-eligibility-metadata-acquisition-request-v1",
        "financial-eligibility-metadata-acquisition-request-v2",
    ] = "financial-eligibility-metadata-acquisition-request-v1"
    canonical_run_id: str
    run_id: str
    asset_configuration: RunAssetConfiguration
    asset_configuration_signature: str
    instrument_identity: InstrumentIdentityEvidence
    authoritative_as_of_date: date | None = None
    as_of_date: date | None = None
    requested_session: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    authoritative_availability_scope: FinancialAvailabilityScope
    availability_scope: FinancialAvailabilityScope
    acquisition_purpose: Literal["foreground_current_analysis"] = "foreground_current_analysis"
    financial_eligibility_binding: MainlandFinancialEligibilityProjectionBinding
    routing_plan_signature: str
    currency: str
    consolidation_scope: FinancialConsolidationScope
    temporal_mode: TemporalEligibilityMode | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_evaluated_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    calendar_binding: MarketSessionEligibilityBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    strict_cutoff: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_request_shape(self) -> FinancialEligibilityMetadataAcquisitionRequest:
        temporal = self.contract_version.endswith("-v2")
        historical_shape = (
            self.authoritative_as_of_date is not None
            and self.as_of_date is not None
            and self.requested_session is None
        )
        temporal_shape = (
            self.authoritative_as_of_date is None
            and self.as_of_date is None
            and self.requested_session is not None
        )
        temporal_fields = (
            self.temporal_mode,
            self.eligibility_evaluated_at,
            self.calendar_binding,
        )
        if (
            temporal and (not temporal_shape or not all(temporal_fields))
        ) or (
            not temporal
            and (
                not historical_shape
                or any((*temporal_fields, self.strict_cutoff))
            )
        ):
            raise ValueError("metadata acquisition request temporal shape mismatch")
        if temporal:
            assert self.temporal_mode is not None
            assert self.calendar_binding is not None
            assert self.eligibility_evaluated_at is not None
            assert self.requested_session is not None
            strict = self.temporal_mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
            if (
                self.calendar_binding.requested_session != self.requested_session
                or self.calendar_binding.eligibility_evaluated_at
                != self.eligibility_evaluated_at
                or not self.calendar_binding.has_valid_identity()
                or strict != (self.strict_cutoff is not None)
            ):
                raise ValueError("metadata acquisition request temporal binding mismatch")
        return self


def _failed(
    reason: FinancialEligibilityAcquisitionFailureReason,
    *,
    resolution: FinancialCompanyTypeResolution | None = None,
    rejected_temporal_decision: FinancialMetadataTemporalEligibilityDecision | None = None,
    temporal_proof_unavailable: bool = False,
    temporal_context: bool = False,
) -> FinancialEligibilityAcquisitionResult:
    return FinancialEligibilityAcquisitionResult.failed(
        reason,
        company_type_resolution=resolution,
        rejected_temporal_decision=rejected_temporal_decision,
        temporal_proof_unavailable=temporal_proof_unavailable,
        temporal_context=temporal_context,
    )


def _resolution_failure(
    resolution: FinancialCompanyTypeResolution,
    *,
    temporal_context: bool = False,
) -> FinancialEligibilityAcquisitionResult | None:
    if resolution.contradictory:
        return _failed(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_CONTRADICTORY,
            resolution=resolution,
            temporal_context=temporal_context,
        )
    if (
        resolution.company_type is FinancialCompanyType.UNKNOWN
        or resolution.method is FinancialCompanyTypeResolutionMethod.UNRESOLVED
    ):
        if resolution.ambiguous and resolution.classifier_inputs:
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS,
                resolution=resolution,
                temporal_context=temporal_context,
            )
        return _failed(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_UNRESOLVED,
            resolution=resolution,
            temporal_context=temporal_context,
        )
    if resolution.ambiguous:
        return _failed(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS,
            resolution=resolution,
            temporal_context=temporal_context,
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
        temporal_transaction_factory: Callable[..., FinancialEligibilityTemporalEvaluationTransaction] = (
            FinancialEligibilityTemporalEvaluationTransaction
        ),
    ) -> None:
        self._routing_plan = routing_plan
        self._router = router
        self._stop_signal = stop_signal
        self._temporal_transaction_factory = temporal_transaction_factory

    def __repr__(self) -> str:
        return "FinancialEligibilityMetadataAcquirer(plan_bound=True)"

    def _interrupted(self) -> bool:
        return self._stop_signal.is_set()

    def _binding(
        self,
        request: FinancialEligibilityMetadataAcquisitionRequest,
    ) -> FinancialEligibilityMetadataBinding:
        if request.contract_version.endswith("-v2"):
            return FinancialEligibilityMetadataBinding(
                contract_version="financial-eligibility-metadata-binding-v2",
                instrument_identity=request.instrument_identity,
                requested_session=request.requested_session,
                availability_scope=request.availability_scope,
                acquisition_purpose=request.acquisition_purpose,
                financial_eligibility_binding=request.financial_eligibility_binding,
                routing_plan_signature=request.routing_plan_signature,
                temporal_mode=request.temporal_mode,
                eligibility_evaluated_at=request.eligibility_evaluated_at,
                calendar_binding=request.calendar_binding,
                strict_cutoff=request.strict_cutoff,
            )
        return FinancialEligibilityMetadataBinding(
            instrument_identity=request.instrument_identity,
            as_of_date=request.as_of_date,
            availability_scope=request.availability_scope,
            acquisition_purpose=request.acquisition_purpose,
            financial_eligibility_binding=request.financial_eligibility_binding,
            routing_plan_signature=request.routing_plan_signature,
        )

    @staticmethod
    def _retained_temporal_binding_compatible(
        retained: FinancialEligibilityMetadataBinding,
        current: FinancialEligibilityMetadataBinding,
    ) -> bool:
        """Compare run intent while deliberately excluding an old run's frontier."""

        return (
            retained.contract_version == "financial-eligibility-metadata-binding-v2"
            and current.contract_version == "financial-eligibility-metadata-binding-v2"
            and retained.instrument_identity == current.instrument_identity
            and retained.requested_session == current.requested_session
            and retained.availability_scope is current.availability_scope
            and retained.acquisition_purpose == current.acquisition_purpose
            and retained.financial_eligibility_binding
            == current.financial_eligibility_binding
            and retained.routing_plan_signature == current.routing_plan_signature
            and retained.temporal_mode is current.temporal_mode
            and retained.strict_cutoff == current.strict_cutoff
        )

    def _retained_company_type_evidence(
        self,
        *,
        binding: FinancialEligibilityMetadataBinding,
        retained: Sequence[FinancialCompanyTypeEvidenceProjection | Mapping[str, object]],
        transaction: FinancialEligibilityTemporalEvaluationTransaction | None,
    ) -> tuple[
        FinancialCompanyTypeAcquisitionEvidenceBinding,
        FinancialMetadataTemporalEligibilityDecision | None,
    ] | None:
        route = self._routing_plan.route_for(MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE)
        by_provider: dict[
            str,
            tuple[
                FinancialCompanyTypeAcquisitionEvidenceBinding,
                FinancialMetadataTemporalEligibilityDecision | None,
            ],
        ] = {}
        for value in retained:
            try:
                projection = FinancialCompanyTypeEvidenceProjection.model_validate(value)
            except (TypeError, ValueError):
                continue
            evidence = projection.evidence
            temporal = transaction is not None
            compatible_binding = (
                self._retained_temporal_binding_compatible(
                    projection.metadata_binding,
                    binding,
                )
                if temporal
                else projection.metadata_binding == binding
            )
            if (
                not compatible_binding
                or evidence.provider_id not in route
                or temporal
                != (
                    projection.contract_version
                    == "financial-company-type-evidence-projection-v2"
                    and evidence.temporal_evidence is not None
                )
            ):
                continue
            decision = (
                transaction.evaluate(evidence.temporal_evidence)
                if temporal and evidence.temporal_evidence is not None
                else None
            )
            candidate = (evidence, decision)
            current = by_provider.get(evidence.provider_id)
            if current is not None and current != candidate:
                return None
            by_provider[evidence.provider_id] = candidate
        return next(
            (by_provider[provider] for provider in route if provider in by_provider),
            None,
        )

    @staticmethod
    def _retained_listing_metadata(
        *,
        binding: FinancialEligibilityMetadataBinding,
        retained: Sequence[FinancialListingMetadataProjection | Mapping[str, object]],
        transaction: FinancialEligibilityTemporalEvaluationTransaction | None,
    ) -> tuple[
        FinancialListingMetadataProjection,
        FinancialMetadataTemporalEligibilityDecision | None,
    ] | None:
        compatible: list[
            tuple[
                FinancialListingMetadataProjection,
                FinancialMetadataTemporalEligibilityDecision | None,
            ]
        ] = []
        for value in retained:
            try:
                projection = FinancialListingMetadataProjection.model_validate(value)
            except (TypeError, ValueError):
                continue
            temporal = transaction is not None
            compatible_binding = (
                FinancialEligibilityMetadataAcquirer._retained_temporal_binding_compatible(
                    projection.metadata_binding,
                    binding,
                )
                if temporal
                else projection.metadata_binding == binding
            )
            proof = projection.metadata.temporal_evidence
            if (
                compatible_binding
                and temporal
                == (
                    projection.contract_version
                    == "financial-listing-metadata-projection-v2"
                    and proof is not None
                )
            ):
                decision = transaction.evaluate(proof) if transaction is not None and proof is not None else None
                compatible.append((projection, decision))
        unique = {
            item[0].metadata.model_dump_json(): item for item in compatible
        }
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

        temporal = request.contract_version.endswith("-v2")
        if self._interrupted():
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION,
                temporal_context=temporal,
            )
        try:
            expected_plan_version = (
                CAPABILITY_ROUTING_PLAN_VERSION_1_3
                if temporal
                else CAPABILITY_ROUTING_PLAN_VERSION
            )
            if (
                self._routing_plan.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
                or self._routing_plan.plan_version != expected_plan_version
                or self._router.routing_plan != self._routing_plan
                or request.run_id != request.canonical_run_id
                or request.asset_configuration_signature
                != request.asset_configuration.asset_configuration_signature
                or request.instrument_identity != request.asset_configuration.instrument_identity
                or (
                    request.authoritative_as_of_date != request.as_of_date
                    if not temporal
                    else request.requested_session is None
                )
                or request.authoritative_availability_scope is not request.availability_scope
                or request.financial_eligibility_binding
                != self._routing_plan.financial_eligibility_binding
                or request.routing_plan_signature != self._routing_plan.plan_signature
                or request.currency != request.instrument_identity.currency
            ):
                raise ValueError("metadata acquisition plan binding mismatch")
            binding = self._binding(request)
            transaction = (
                self._temporal_transaction_factory(
                    mode=request.temporal_mode,
                    calendar_binding=request.calendar_binding,
                    strict_cutoff=request.strict_cutoff,
                )
                if temporal
                else None
            )
        except (AttributeError, TypeError, ValueError):
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH,
                temporal_context=temporal,
            )

        try:
            retained_company = self._retained_company_type_evidence(
                binding=binding,
                retained=retained_company_type_evidence,
                transaction=transaction,
            )
        except Exception:
            return _failed(
                (
                    FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE
                    if temporal
                    else FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
                ),
                temporal_proof_unavailable=temporal,
                temporal_context=temporal,
            )
        evidence = retained_company[0] if retained_company is not None else None
        company_temporal_decision = (
            retained_company[1] if retained_company is not None else None
        )
        if evidence is None:
            try:
                routed = self._router.route_company_type_evidence(
                    FinancialCompanyTypeEvidenceRoutingRequest(
                        contract_version=(
                            "financial-company-type-evidence-routing-request-v2"
                            if temporal
                            else "financial-company-type-evidence-routing-request-v1"
                        ),
                        metadata_binding=binding,
                    ),
                    interrupted=self._interrupted,
                    temporal_transaction=transaction,
                )
                evidence = routed.selected_evidence
                company_temporal_decision = (
                    routed.provider_calls[-1].temporal_decision
                    if routed.selected_evidence is not None
                    else None
                )
                rejected_route_decision = next(
                    (
                        call.temporal_decision
                        for call in routed.provider_calls
                        if call.temporal_decision is not None
                        and call.temporal_decision.result == "rejected"
                    ),
                    None,
                )
            except Exception:
                evidence = None
                rejected_route_decision = None
            if evidence is None:
                if temporal and rejected_route_decision is not None:
                    return _failed(
                        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED,
                        rejected_temporal_decision=rejected_route_decision,
                        temporal_context=True,
                    )
                return _failed(
                    FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
                    if self._interrupted()
                    else (
                        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE
                        if temporal
                        else FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE
                    ),
                    temporal_proof_unavailable=temporal,
                    temporal_context=temporal,
                )
        if temporal and company_temporal_decision is None:
            proof = evidence.temporal_evidence
            if proof is None:
                return _failed(
                    FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE,
                    temporal_proof_unavailable=True,
                    temporal_context=True,
                )
            return _failed(
                (
                    FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE
                    if temporal
                    else FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
                ),
                temporal_proof_unavailable=True,
                temporal_context=True,
            )
        if (
            company_temporal_decision is not None
            and company_temporal_decision.result != "accepted"
        ):
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED,
                rejected_temporal_decision=company_temporal_decision,
                temporal_context=True,
            )
        if self._interrupted():
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION,
                temporal_context=temporal,
            )
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
                FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE,
                temporal_context=temporal,
            )
        failure = _resolution_failure(resolution, temporal_context=temporal)
        if failure is not None:
            return failure
        if self._interrupted():
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION,
                temporal_context=temporal,
            )

        try:
            retained_listing = self._retained_listing_metadata(
                binding=binding,
                retained=retained_listing_metadata,
                transaction=transaction,
            )
        except Exception:
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE,
                temporal_proof_unavailable=temporal,
                temporal_context=temporal,
            )
        listing_projection = retained_listing[0] if retained_listing is not None else None
        listing_temporal_decision = (
            retained_listing[1] if retained_listing is not None else None
        )
        listing = listing_projection.metadata if listing_projection is not None else None
        if listing is None:
            try:
                routed_listing = self._router.route_capability(
                    FinancialCapabilityRoutingRequest(
                        instrument_identity=request.instrument_identity,
                        capability=MainlandCapability.ISSUER_LIFECYCLE,
                        range_start=(
                            request.requested_session
                            if temporal
                            else request.as_of_date
                        ),
                        as_of_date=(
                            request.requested_session
                            if temporal
                            else request.as_of_date
                        ),
                    )
                )
                listing = routed_listing.listing_metadata
            except Exception:
                listing = None
            if listing is None:
                return _failed(
                    FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
                    if self._interrupted()
                    else (
                        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE
                        if temporal
                        else FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE
                    ),
                    temporal_proof_unavailable=temporal,
                    temporal_context=temporal,
                )
        if self._interrupted():
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION,
                temporal_context=temporal,
            )
        assert listing is not None
        if temporal and listing_temporal_decision is None:
            proof = listing.temporal_evidence
            if proof is None:
                return _failed(
                    FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE,
                    temporal_proof_unavailable=True,
                    temporal_context=True,
                )
            assert transaction is not None
            try:
                listing_temporal_decision = transaction.evaluate(proof)
            except Exception:
                return _failed(
                    FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE,
                    temporal_proof_unavailable=True,
                    temporal_context=True,
                )
        if (
            listing_temporal_decision is not None
            and listing_temporal_decision.result != "accepted"
        ):
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED,
                rejected_temporal_decision=listing_temporal_decision,
                temporal_context=True,
            )
        try:
            if self._routing_plan.financial_eligibility_policy is None:
                raise ValueError("financial eligibility policy is unavailable")
            annual, reporting = derive_mainland_financial_acquisition_period_supersets(
                run_as_of_date=(
                    request.requested_session
                    if temporal
                    else request.authoritative_as_of_date
                ),
                listing_date=listing.listing_date,
                policy=self._routing_plan.financial_eligibility_policy,
            )
        except (AttributeError, TypeError, ValueError):
            return _failed(
                FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH,
                temporal_context=temporal,
            )
        return build_financial_eligibility_acquisition_context(
            canonical_run_id=request.canonical_run_id,
            run_id=request.run_id,
            asset_configuration=request.asset_configuration,
            asset_configuration_signature=request.asset_configuration_signature,
            instrument_identity=request.instrument_identity,
            authoritative_as_of_date=request.authoritative_as_of_date,
            as_of_date=request.as_of_date,
            requested_session=request.requested_session,
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
            temporal_mode=request.temporal_mode,
            eligibility_evaluated_at=request.eligibility_evaluated_at,
            calendar_binding=request.calendar_binding,
            company_type_temporal_decision=company_temporal_decision,
            listing_temporal_decision=listing_temporal_decision,
            listing_temporal_evidence=listing.temporal_evidence,
            strict_cutoff=request.strict_cutoff,
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

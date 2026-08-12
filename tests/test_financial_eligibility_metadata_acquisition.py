from __future__ import annotations

import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier, Event, Lock

import pandas as pd
import pytest

import tradingagents.dataflows.financial_eligibility_metadata as metadata_module
import tradingagents.dataflows.financial_temporal_eligibility as temporal_module
from tradingagents.asset_configuration import (
    RunAssetConfiguration,
    resolve_run_asset_configuration,
)
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
    preflight_mainland_capability_routing,
    prepare_mainland_temporal_capability_routing_plan,
)
from tradingagents.dataflows.baostock_capabilities import BaoStockCapabilityAdapter
from tradingagents.dataflows.financial_capability_routing import (
    FinancialCapabilityProviderResponse,
    FinancialCapabilityRoutingRequest,
    FinancialCompanyTypeEvidenceProviderProjection,
    FinancialCompanyTypeEvidenceProviderResponse,
    FinancialCompanyTypeEvidenceRoutingRequest,
    FinancialCompanyTypeEvidenceRoutingResult,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialCompanyTypeClassifierInput,
    FinancialConsolidationScope,
    FinancialListingProvenance,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialCompanyTypeEvidenceProjection,
    FinancialCompanyTypeProviderTemporalProofBinding,
    FinancialEligibilityAcquisitionFailureReason,
    FinancialEligibilityAcquisitionResult,
    FinancialEligibilityMetadataBinding,
    FinancialListingMetadata,
    FinancialListingMetadataProjection,
)
from tradingagents.dataflows.financial_eligibility_metadata import (
    FinancialEligibilityMetadataAcquirer,
    FinancialEligibilityMetadataAcquisitionRequest,
)
from tradingagents.dataflows.financial_temporal_eligibility import (
    EvidenceEffectiveAsOfDerivation,
    EvidenceEffectiveAsOfSemantics,
    ExactRevisionAvailabilityProof,
    ExactRevisionAvailabilityScope,
    FinancialMetadataTemporalEligibilityDecision,
    FinancialMetadataTemporalEvidence,
    ProviderPublicationDateDerivation,
    ProviderPublicationDateSemantics,
    PublicationDateKind,
    TemporalEligibilityMode,
    TemporalEvidenceArtifactBinding,
    TemporalEvidenceClass,
    build_market_session_eligibility_binding,
    evaluate_financial_metadata_temporal_eligibility,
)
from tradingagents.dataflows.financial_temporal_proof_projection import (
    FinancialEligibilityTemporalEvaluationTransaction,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestKey,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import InstrumentIdentityEvidence
from tradingagents.market_history import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
    MarketSession,
    MarketSessionStatus,
    ProvenanceClass,
    ProviderRequestCoordinator,
    RequestPriority,
    StoredSessionCalendar,
    upstream_service_identity_for_provider,
)
from tradingagents.qualified_routing_eligibility_lifecycle import (
    FinancialEligibilityAcquisitionContextOwner,
    QualifiedFinancialRoutingComposition,
)

_AS_OF = date(2026, 8, 8)
_OBSERVED_AT = datetime(2026, 8, 8, 1, 2, tzinfo=timezone.utc)
_RETRIEVED_AT = datetime(2026, 8, 8, 1, 3, tzinfo=timezone.utc)


def _run_id(label: str) -> str:
    return f"run:{sha256(label.encode('utf-8')).hexdigest()}"


def _qualified_config() -> dict[str, object]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": ("statements", "financial_indicators"),
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    return config


def _asset(symbol: str = "601328.SS") -> RunAssetConfiguration:
    return resolve_run_asset_configuration(
        symbol,
        config=copy.deepcopy(DEFAULT_CONFIG),
    )


def _qualified_plan(
    monkeypatch: pytest.MonkeyPatch,
    asset: RunAssetConfiguration,
) -> MainlandCapabilityRoutingPlan:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    result = preflight_mainland_capability_routing(
        asset,
        config=_qualified_config(),
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert result.plan is not None
    return result.plan


def _metadata_binding(
    asset: RunAssetConfiguration,
    plan: MainlandCapabilityRoutingPlan,
) -> FinancialEligibilityMetadataBinding:
    assert plan.financial_eligibility_binding is not None
    return FinancialEligibilityMetadataBinding(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
    )


def _company_type_projection(
    asset: RunAssetConfiguration,
    plan: MainlandCapabilityRoutingPlan,
) -> FinancialCompanyTypeEvidenceProjection:
    evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        provider_id="tushare",
        artifact_identity="provider-artifact:v1:" + "1" * 64,
        source_ref="acq.v1:financial.company-type:tushare",
        observed_at=_OBSERVED_AT,
        retrieved_at=_RETRIEVED_AT,
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        normalized_resolver_inputs=(
            FinancialCompanyTypeClassifierInput(
                key="metadata:industry",
                value="bank",
            ),
            FinancialCompanyTypeClassifierInput(
                key="present_field",
                value="customer_deposits",
            ),
        ),
    )
    return FinancialCompanyTypeEvidenceProjection(
        metadata_binding=_metadata_binding(asset, plan),
        evidence=evidence,
    )


def _company_type_evidence(
    provider_id: str,
    *,
    instrument_identity: InstrumentIdentityEvidence,
    as_of_date: date = _AS_OF,
    declared_type: FinancialCompanyType | None,
    declaration_qualified: bool,
    inputs: tuple[FinancialCompanyTypeClassifierInput, ...],
) -> FinancialCompanyTypeAcquisitionEvidenceBinding:
    return FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=instrument_identity,
        as_of_date=as_of_date,
        provider_id=provider_id,
        artifact_identity=(
            "provider-artifact:v1:" + sha256(f"company-type-{provider_id}".encode()).hexdigest()
        ),
        source_ref=f"acq.v1:financial.company-type:{provider_id}",
        observed_at=_OBSERVED_AT,
        retrieved_at=_RETRIEVED_AT,
        provider_declared_type=declared_type,
        provider_declaration_qualified=declaration_qualified,
        normalized_resolver_inputs=inputs,
    )


def _listing_projection(
    asset: RunAssetConfiguration,
    plan: MainlandCapabilityRoutingPlan,
) -> FinancialListingMetadataProjection:
    return FinancialListingMetadataProjection(
        metadata_binding=_metadata_binding(asset, plan),
        metadata=FinancialListingMetadata(
            instrument_identity=asset.instrument_identity,
            as_of_date=_AS_OF,
            listing_date=date(1987, 4, 1),
            listing_provenance=FinancialListingProvenance(
                provider_id="baostock",
                source_ref="provider-artifact:v1:" + "2" * 64,
                observed_at=_OBSERVED_AT,
            ),
        ),
    )


def _request(
    asset: RunAssetConfiguration,
    plan: MainlandCapabilityRoutingPlan,
) -> FinancialEligibilityMetadataAcquisitionRequest:
    assert plan.financial_eligibility_binding is not None
    run_id = _run_id(asset.instrument_identity.symbol)
    return FinancialEligibilityMetadataAcquisitionRequest(
        canonical_run_id=run_id,
        run_id=run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        authoritative_as_of_date=_AS_OF,
        as_of_date=_AS_OF,
        authoritative_availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
    )


@pytest.mark.unit
def test_historical_v1_serialization_does_not_emit_temporal_v2_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    request = _request(asset, plan)
    company = _company_type_projection(asset, plan)
    response = FinancialCompanyTypeEvidenceProviderResponse.available(
        provider_id="tushare",
        metadata_binding=company.metadata_binding,
        source_projection_kind="tushare_statement",
        evidence=company.evidence,
    )
    routed = FinancialCompanyTypeEvidenceRoutingResult(
        route=("tushare",),
        provider_calls=(response,),
        selected_provider="tushare",
        selected_evidence=company.evidence,
    )
    assert routed.contract_version == "financial-company-type-evidence-routing-result-v1"
    failure = FinancialEligibilityAcquisitionResult.failed(
        FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE
    )

    request_payload = request.model_dump(mode="json")
    assert {
        "requested_session",
        "temporal_mode",
        "eligibility_evaluated_at",
        "calendar_binding",
        "strict_cutoff",
    }.isdisjoint(request_payload)
    assert "temporal_decision" not in response.model_dump(mode="json")
    assert "selected_temporal_decision" not in routed.model_dump(mode="json")
    assert failure.failure is not None
    failure_payload = failure.failure.model_dump(mode="json")
    assert "rejected_temporal_decision" not in failure_payload
    assert "temporal_rejection_reasons" not in failure_payload

    request_with_cutoff = request.model_dump(mode="python")
    request_with_cutoff["asset_configuration"] = request.asset_configuration
    request_with_cutoff["strict_cutoff"] = _OBSERVED_AT
    with pytest.raises(ValueError, match="temporal shape mismatch"):
        FinancialEligibilityMetadataAcquisitionRequest.model_validate(
            request_with_cutoff
        )
    metadata_with_cutoff = company.metadata_binding.model_dump(mode="python")
    metadata_with_cutoff["strict_cutoff"] = _OBSERVED_AT
    with pytest.raises(ValueError, match="requires a CNY"):
        FinancialEligibilityMetadataBinding.model_validate(metadata_with_cutoff)

    temporal_plan = prepare_mainland_temporal_capability_routing_plan(plan)
    temporal_company, temporal_listing = _retained_temporal_projections(
        asset,
        temporal_plan,
        evaluated_at=datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="acquisition binding mismatch"):
        FinancialCompanyTypeEvidenceProjection(
            metadata_binding=company.metadata_binding,
            evidence=company.evidence,
            temporal_decision=temporal_company.temporal_decision,
        )
    listing = _listing_projection(asset, plan)
    with pytest.raises(ValueError, match="acquisition binding mismatch"):
        FinancialListingMetadataProjection(
            metadata_binding=listing.metadata_binding,
            metadata=listing.metadata,
            temporal_decision=temporal_listing.temporal_decision,
        )


@pytest.mark.unit
def test_temporal_run_emits_result_v2_for_non_temporal_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    stop_signal = Event()
    stop_signal.set()
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        stop_signal=stop_signal,
    ).acquire(
        _temporal_request(
            asset,
            plan,
            evaluated_at=datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc),
        )
    )

    assert result.contract_version == "financial-eligibility-acquisition-result-v2"
    assert result.failure is not None
    assert result.failure.contract_version == result.contract_version
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
    )


def _stored_temporal_calendar() -> StoredSessionCalendar:
    return StoredSessionCalendar(
        calendar_revision_id="market-session-calendar:v1:" + "7" * 64,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provider_dataset_id="provider-dataset:fixture-calendar-v1",
        observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        sessions=(
            MarketSession(date(2026, 8, 10), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 11), MarketSessionStatus.OPEN),
        ),
    )


def _temporal_calendar(evaluated_at: datetime):
    return build_market_session_eligibility_binding(
        _stored_temporal_calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=evaluated_at,
    )


def _temporal_proof(
    *,
    evidence_class: TemporalEvidenceClass,
    artifact_identity: str,
    row_identity: str,
    effective: date,
    effective_semantics: EvidenceEffectiveAsOfSemantics,
    effective_source_field: str,
    publication_semantics: ProviderPublicationDateSemantics,
    ann_source_field: str,
    first_source_field: str,
) -> FinancialMetadataTemporalEvidence:
    artifact = TemporalEvidenceArtifactBinding(
        artifact_identity=artifact_identity,
        revision_identity=artifact_identity,
        row_identity=row_identity,
    )
    retrieved = datetime(2026, 8, 10, 16, 48, 7, tzinfo=timezone.utc)
    return FinancialMetadataTemporalEvidence(
        evidence_class=evidence_class,
        artifact=artifact,
        evidence_effective_as_of=effective,
        evidence_effective_as_of_derivation=EvidenceEffectiveAsOfDerivation(
            semantics=effective_semantics,
            source_field=effective_source_field,
            artifact=artifact,
        ),
        ann_date=date(2026, 8, 10),
        ann_date_derivation=ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.ANN_DATE,
            semantics=publication_semantics,
            source_field=ann_source_field,
            artifact=artifact,
        ),
        f_ann_date=date(2026, 8, 10),
        f_ann_date_derivation=ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.F_ANN_DATE,
            semantics=publication_semantics,
            source_field=first_source_field,
            artifact=artifact,
        ),
        observed_at=retrieved,
        retrieved_at=retrieved,
    )


def _temporal_request(
    asset: RunAssetConfiguration,
    plan: MainlandCapabilityRoutingPlan,
    *,
    evaluated_at: datetime,
) -> FinancialEligibilityMetadataAcquisitionRequest:
    run_id = _run_id("temporal-" + evaluated_at.isoformat())
    binding = _temporal_calendar(evaluated_at)
    assert plan.financial_eligibility_binding is not None
    return FinancialEligibilityMetadataAcquisitionRequest(
        contract_version="financial-eligibility-metadata-acquisition-request-v2",
        canonical_run_id=run_id,
        run_id=run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        requested_session=date(2026, 8, 10),
        authoritative_availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        temporal_mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        eligibility_evaluated_at=evaluated_at,
        calendar_binding=binding,
    )


def _retained_temporal_projections(
    asset: RunAssetConfiguration,
    plan: MainlandCapabilityRoutingPlan,
    *,
    evaluated_at: datetime,
):
    calendar_binding = _temporal_calendar(evaluated_at)
    metadata_binding = FinancialEligibilityMetadataBinding(
        contract_version="financial-eligibility-metadata-binding-v2",
        instrument_identity=asset.instrument_identity,
        requested_session=date(2026, 8, 10),
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        temporal_mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        eligibility_evaluated_at=evaluated_at,
        calendar_binding=calendar_binding,
    )
    company_artifact = "financial-provider-artifact:v1:" + "4" * 64
    company_proof = _temporal_proof(
        evidence_class=TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
        artifact_identity=company_artifact,
        row_identity="financial-period-candidate:v1:" + "5" * 64,
        effective=date(2026, 6, 30),
        effective_semantics=EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END,
        effective_source_field="end_date",
        publication_semantics=(
            ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
        ),
        ann_source_field="ann_date",
        first_source_field="f_ann_date",
    )
    company_evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=asset.instrument_identity,
        requested_session=date(2026, 8, 10),
        temporal_evidence=company_proof,
        provider_id="tushare",
        artifact_identity=company_artifact,
        source_ref=company_artifact,
        observed_at=company_proof.observed_at,
        retrieved_at=company_proof.retrieved_at,
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        normalized_resolver_inputs=(
            FinancialCompanyTypeClassifierInput(key="metadata:industry", value="bank"),
            FinancialCompanyTypeClassifierInput(
                key="present_field", value="customer_deposits"
            ),
        ),
    )
    company_decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=company_proof,
        calendar_binding=calendar_binding,
    )
    company_projection = FinancialCompanyTypeEvidenceProjection(
        contract_version="financial-company-type-evidence-projection-v2",
        metadata_binding=metadata_binding,
        evidence=company_evidence,
        temporal_decision=company_decision,
    )
    listing_artifact = "provider-artifact:v1:" + "6" * 64
    listing_proof = _temporal_proof(
        evidence_class=TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING,
        artifact_identity=listing_artifact,
        row_identity="provider-row-artifact:v1:" + "7" * 64,
        effective=date(2010, 1, 4),
        effective_semantics=EvidenceEffectiveAsOfSemantics.BAOSTOCK_LIFECYCLE_LISTING_DATE,
        effective_source_field="ipoDate",
        publication_semantics=(
            ProviderPublicationDateSemantics.BAOSTOCK_LIFECYCLE_PUBLICATION_FIELDS
        ),
        ann_source_field="announcement_date",
        first_source_field="first_publication_date",
    )
    listing_metadata = FinancialListingMetadata(
        contract_version="financial-listing-metadata-v2",
        instrument_identity=asset.instrument_identity,
        requested_session=date(2026, 8, 10),
        listing_date=date(2010, 1, 4),
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref=listing_artifact,
            observed_at=listing_proof.observed_at,
        ),
        temporal_evidence=listing_proof,
    )
    listing_decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=listing_proof,
        calendar_binding=calendar_binding,
    )
    listing_projection = FinancialListingMetadataProjection(
        contract_version="financial-listing-metadata-projection-v2",
        metadata_binding=metadata_binding,
        metadata=listing_metadata,
        temporal_decision=listing_decision,
    )
    return company_projection, listing_projection


def _matrix_calendar() -> StoredSessionCalendar:
    return StoredSessionCalendar(
        calendar_revision_id="market-session-calendar:v1:" + "8" * 64,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provider_dataset_id="provider-dataset:ticket-06b-calendar-v1",
        observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        sessions=tuple(
            MarketSession(
                date(2026, 8, day),
                (
                    MarketSessionStatus.OPEN
                    if day in {7, 10, 11, 17}
                    else MarketSessionStatus.CLOSED
                ),
            )
            for day in range(7, 18)
        ),
    )


def _matrix_binding(
    *,
    requested_session: date,
    evaluated_at: datetime,
):
    return build_market_session_eligibility_binding(
        _matrix_calendar(),
        requested_session=requested_session,
        eligibility_evaluated_at=evaluated_at,
    )


def _matrix_proof(
    *,
    subject: str,
    requested_session: date,
    retrieved_at: datetime,
    observed_at: datetime,
    changes: dict[str, object],
    exact_scope: ExactRevisionAvailabilityScope | None,
) -> FinancialMetadataTemporalEvidence:
    company = subject == "company"
    artifact_identity = (
        "financial-provider-artifact:v1:" + "c" * 64
        if company
        else "provider-artifact:v1:" + "d" * 64
    )
    artifact = TemporalEvidenceArtifactBinding(
        artifact_identity=artifact_identity,
        revision_identity=artifact_identity,
        row_identity=(
            "financial-period-candidate:v1:" + "e" * 64
            if company
            else "provider-row-artifact:v1:" + "f" * 64
        ),
    )
    evidence_class = (
        TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE
        if company
        else TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING
    )
    effective_semantics = (
        EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END
        if company
        else EvidenceEffectiveAsOfSemantics.BAOSTOCK_LIFECYCLE_LISTING_DATE
    )
    publication_semantics = (
        ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
        if company
        else ProviderPublicationDateSemantics.BAOSTOCK_LIFECYCLE_PUBLICATION_FIELDS
    )
    payload: dict[str, object] = {
        "evidence_class": evidence_class,
        "artifact": artifact,
        "evidence_effective_as_of": (
            date(2026, 6, 30) if company else date(2010, 1, 4)
        ),
        "evidence_effective_as_of_derivation": EvidenceEffectiveAsOfDerivation(
            semantics=effective_semantics,
            source_field="end_date" if company else "ipoDate",
            artifact=artifact,
        ),
        "ann_date": requested_session,
        "ann_date_derivation": ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.ANN_DATE,
            semantics=publication_semantics,
            source_field="ann_date" if company else "announcement_date",
            artifact=artifact,
        ),
        "f_ann_date": requested_session,
        "f_ann_date_derivation": ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.F_ANN_DATE,
            semantics=publication_semantics,
            source_field="f_ann_date" if company else "first_publication_date",
            artifact=artifact,
        ),
        "observed_at": observed_at,
        "retrieved_at": retrieved_at,
        "retrieval_local_date": retrieved_at.astimezone(
            timezone(timedelta(hours=8))
        ).date(),
    }
    if exact_scope is not None:
        payload["exact_revision_availability_proof"] = (
            ExactRevisionAvailabilityProof(
                exact_revision_authoritative_available_at=(
                    _session_time(requested_session, 14, 59)
                ),
                authoritative=True,
                scope=exact_scope,
                proof_identity="exchange-revision-proof:v1:" + "9" * 64,
                artifact=artifact,
            )
        )
    payload.update(changes)
    return FinancialMetadataTemporalEvidence(**payload)


def _session_time(
    session: date,
    hour: int,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(
        session.year,
        session.month,
        session.day,
        hour,
        minute,
        second,
        tzinfo=timezone(timedelta(hours=8)),
    )


def _matrix_metadata_binding(
    *,
    asset: RunAssetConfiguration,
    plan: MainlandCapabilityRoutingPlan,
    requested_session: date,
    mode: TemporalEligibilityMode,
    evaluated_at: datetime,
):
    calendar_binding = _matrix_binding(
        requested_session=requested_session,
        evaluated_at=evaluated_at,
    )
    strict = mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
    return FinancialEligibilityMetadataBinding(
        contract_version="financial-eligibility-metadata-binding-v2",
        instrument_identity=asset.instrument_identity,
        requested_session=requested_session,
        availability_scope=(
            FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
            if strict
            else FinancialAvailabilityScope.CURRENT_ONLY
        ),
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        temporal_mode=mode,
        eligibility_evaluated_at=evaluated_at,
        calendar_binding=calendar_binding,
        strict_cutoff=(
            calendar_binding.requested_session_close if strict else None
        ),
    )


def _matrix_request(
    *,
    asset: RunAssetConfiguration,
    plan: MainlandCapabilityRoutingPlan,
    binding: FinancialEligibilityMetadataBinding,
    label: str,
) -> FinancialEligibilityMetadataAcquisitionRequest:
    run_id = _run_id("ticket-06b-" + label)
    return FinancialEligibilityMetadataAcquisitionRequest(
        contract_version="financial-eligibility-metadata-acquisition-request-v2",
        canonical_run_id=run_id,
        run_id=run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        requested_session=binding.requested_session,
        authoritative_availability_scope=binding.availability_scope,
        availability_scope=binding.availability_scope,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        temporal_mode=binding.temporal_mode,
        eligibility_evaluated_at=binding.eligibility_evaluated_at,
        calendar_binding=binding.calendar_binding,
        strict_cutoff=binding.strict_cutoff,
    )


def _matrix_company_evidence(
    asset: RunAssetConfiguration,
    requested_session: date,
    proof: FinancialMetadataTemporalEvidence,
) -> FinancialCompanyTypeAcquisitionEvidenceBinding:
    return FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=asset.instrument_identity,
        requested_session=requested_session,
        temporal_evidence=proof,
        provider_id="tushare",
        artifact_identity=proof.artifact.artifact_identity,
        source_ref=proof.artifact.artifact_identity,
        observed_at=proof.observed_at,
        retrieved_at=proof.retrieved_at,
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        normalized_resolver_inputs=(
            FinancialCompanyTypeClassifierInput(
                key="metadata:industry",
                value="bank",
            ),
            FinancialCompanyTypeClassifierInput(
                key="present_field",
                value="customer_deposits",
            ),
        ),
    )


def _matrix_listing_metadata(
    asset: RunAssetConfiguration,
    requested_session: date,
    proof: FinancialMetadataTemporalEvidence,
) -> FinancialListingMetadata:
    return FinancialListingMetadata(
        contract_version="financial-listing-metadata-v2",
        instrument_identity=asset.instrument_identity,
        requested_session=requested_session,
        listing_date=date(2010, 1, 4),
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref=proof.artifact.artifact_identity,
            observed_at=proof.observed_at,
        ),
        temporal_evidence=proof,
    )


@pytest.mark.unit
@pytest.mark.parametrize("origin", ("fresh", "retained"))
@pytest.mark.parametrize("subject", ("company", "listing"))
def test_ticket_06b_complete_temporal_matrix_and_replay_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    subject: str,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    real_0048 = datetime(2026, 8, 10, 16, 48, 7, tzinfo=timezone.utc)
    scenarios = (
        (
            "requested_day_after_close",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 10), 15, 1),
            {},
            None,
            "accepted",
            "rejected",
        ),
        (
            "real_ticket07_0048",
            date(2026, 8, 10),
            real_0048,
            _session_time(date(2026, 8, 11), 0, 49),
            {"retrieval_local_date": date(2099, 1, 1)},
            None,
            "accepted",
            "rejected",
        ),
        (
            "immediately_before_next_open",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 9, 29, 59),
            {},
            None,
            "accepted",
            "rejected",
        ),
        (
            "during_next_session_before_close",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 14, 59, 59),
            {},
            None,
            "accepted",
            "rejected",
        ),
        (
            "at_next_session_close",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 15),
            {},
            None,
            "rejected",
            "rejected",
        ),
        (
            "after_next_session_close",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 15, 0, 1),
            {},
            None,
            "rejected",
            "rejected",
        ),
        (
            "friday_to_saturday",
            date(2026, 8, 7),
            _session_time(date(2026, 8, 7), 15, 0, 1),
            _session_time(date(2026, 8, 8), 12),
            {},
            None,
            "accepted",
            "rejected",
        ),
        (
            "friday_to_sunday",
            date(2026, 8, 7),
            _session_time(date(2026, 8, 7), 15, 0, 1),
            _session_time(date(2026, 8, 9), 12),
            {},
            None,
            "accepted",
            "rejected",
        ),
        (
            "mainland_holiday_interval",
            date(2026, 8, 11),
            _session_time(date(2026, 8, 11), 15, 0, 1),
            _session_time(date(2026, 8, 14), 12),
            {},
            None,
            "accepted",
            "rejected",
        ),
        (
            "stale_after_later_sessions",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 17), 15),
            {},
            None,
            "rejected",
            "rejected",
        ),
        (
            "ann_date_after_requested",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 0, 49),
            {"ann_date": date(2026, 8, 11)},
            None,
            "rejected",
            "rejected",
        ),
        (
            "missing_f_ann_date",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 0, 49),
            {"f_ann_date": None, "f_ann_date_derivation": None},
            None,
            "accepted",
            "rejected",
        ),
        (
            "f_ann_date_after_requested",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 0, 49),
            {"f_ann_date": date(2026, 8, 11)},
            None,
            "accepted",
            "rejected",
        ),
        (
            "direct_strict_pre_cutoff",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 14, 59),
            _session_time(date(2026, 8, 11), 0, 49),
            {},
            None,
            "rejected",
            "accepted",
        ),
        (
            "exact_revision_authoritative",
            date(2026, 8, 10),
            real_0048,
            _session_time(date(2026, 8, 11), 0, 49),
            {},
            ExactRevisionAvailabilityScope.EXACT_IMMUTABLE_REVISION,
            "accepted",
            "accepted",
        ),
        (
            "generic_filing_availability",
            date(2026, 8, 10),
            real_0048,
            _session_time(date(2026, 8, 11), 0, 49),
            {},
            ExactRevisionAvailabilityScope.GENERIC_FILING,
            "accepted",
            "rejected",
        ),
        (
            "missing_effective_as_of",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 0, 49),
            {
                "evidence_effective_as_of": None,
                "evidence_effective_as_of_derivation": None,
            },
            None,
            "rejected",
            "rejected",
        ),
        (
            "missing_ann_date",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 0, 49),
            {"ann_date": None, "ann_date_derivation": None},
            None,
            "rejected",
            "rejected",
        ),
        (
            "observation_after_retrieval",
            date(2026, 8, 10),
            _session_time(date(2026, 8, 10), 15, 0, 1),
            _session_time(date(2026, 8, 11), 0, 49),
            {},
            None,
            "rejected",
            "rejected",
        ),
    )
    recorded = []
    replay_records = []
    for (
        name,
        requested_session,
        retrieved_at,
        evaluated_at,
        changes,
        exact_scope,
        expected_current,
        expected_strict,
    ) in scenarios:
        observed_at = (
            retrieved_at + timedelta(seconds=1)
            if name == "observation_after_retrieval"
            else retrieved_at
        )
        proof = _matrix_proof(
            subject=subject,
            requested_session=requested_session,
            retrieved_at=retrieved_at,
            observed_at=observed_at,
            changes=changes,
            exact_scope=exact_scope,
        )
        if origin == "retained":
            proof = FinancialMetadataTemporalEvidence.model_validate_json(
                proof.model_dump_json()
            )
        binding = _matrix_binding(
            requested_session=requested_session,
            evaluated_at=evaluated_at,
        )
        for mode, expected in (
            (TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY, expected_current),
            (TemporalEligibilityMode.STRICT_HISTORICAL_PIT, expected_strict),
        ):
            decision = evaluate_financial_metadata_temporal_eligibility(
                mode=mode,
                evidence=proof,
                calendar_binding=binding,
                strict_cutoff=(
                    binding.requested_session_close
                    if mode is TemporalEligibilityMode.STRICT_HISTORICAL_PIT
                    else None
                ),
            )
            assert decision.result == expected, (origin, subject, name, mode)
            assert tuple(reason.value for reason in decision.reasons) == tuple(
                sorted(reason.value for reason in decision.reasons)
            )
            recorded.append((name, mode, decision))

            metadata_binding = _matrix_metadata_binding(
                asset=asset,
                plan=plan,
                requested_session=requested_session,
                mode=mode,
                evaluated_at=evaluated_at,
            )
            support_subject = "listing" if subject == "company" else "company"
            support_retrieved_at = _session_time(
                requested_session,
                15,
                0,
                1,
            )
            support_proof = _matrix_proof(
                subject=support_subject,
                requested_session=requested_session,
                retrieved_at=support_retrieved_at,
                observed_at=support_retrieved_at,
                changes={},
                exact_scope=(
                    ExactRevisionAvailabilityScope.EXACT_IMMUTABLE_REVISION
                ),
            )
            support_decision = evaluate_financial_metadata_temporal_eligibility(
                mode=mode,
                evidence=support_proof,
                calendar_binding=binding,
                strict_cutoff=metadata_binding.strict_cutoff,
            )
            if subject == "listing" and support_decision.result != "accepted":
                assert decision.result == "rejected"
                replay_records.append(
                    (
                        None,
                        FinancialEligibilityAcquisitionResult.failed(
                            FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED,
                            rejected_temporal_decision=decision,
                        ),
                        decision,
                    )
                )
                continue
            company_proof = proof if subject == "company" else support_proof
            company_decision = decision if subject == "company" else support_decision
            listing_proof = proof if subject == "listing" else support_proof
            listing_decision = decision if subject == "listing" else support_decision
            company_projection_unavailable = subject == "company" and name in {
                "missing_effective_as_of",
                "missing_ann_date",
                "observation_after_retrieval",
            }
            listing_projection_unavailable = (
                subject == "listing" and name == "missing_effective_as_of"
            )
            if (
                company_projection_unavailable
                or listing_projection_unavailable
            ):
                # The provider contract rejects these company payloads before an
                # acquisition projection can exist, or the company prerequisite
                # deterministically ends acquisition before listing is reachable.
                assert decision.result == "rejected"
                replay_records.append(
                    (
                        None,
                        FinancialEligibilityAcquisitionResult.failed(
                            FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED,
                            rejected_temporal_decision=decision,
                        ),
                        decision,
                    )
                )
                continue
            company_evidence = _matrix_company_evidence(
                asset,
                requested_session,
                company_proof,
            )
            listing_metadata = _matrix_listing_metadata(
                asset,
                requested_session,
                listing_proof,
            )
            company_projection = FinancialCompanyTypeEvidenceProjection(
                contract_version="financial-company-type-evidence-projection-v2",
                metadata_binding=metadata_binding,
                evidence=company_evidence,
                temporal_decision=company_decision,
            )
            listing_projection = FinancialListingMetadataProjection(
                contract_version="financial-listing-metadata-projection-v2",
                metadata_binding=metadata_binding,
                metadata=listing_metadata,
                temporal_decision=listing_decision,
            )

            def company_source(request, evidence=company_evidence):
                return FinancialCompanyTypeEvidenceProviderProjection(
                    provider_id="tushare",
                    metadata_binding=request.metadata_binding,
                    source_projection_kind="tushare_statement",
                    evidence=evidence,
                )

            def listing_source(
                _request,
                proof=listing_proof,
                metadata=listing_metadata,
            ):
                return FinancialCapabilityProviderResponse.available(
                    provider_id="baostock",
                    capability=MainlandCapability.ISSUER_LIFECYCLE,
                    retained_artifact_identities=(
                        proof.artifact.artifact_identity,
                    ),
                    candidate_count=1,
                    subrequest_keys=(),
                    physical_attempt_ids=(),
                    listing_metadata=metadata,
                )

            transactions: list[
                FinancialEligibilityTemporalEvaluationTransaction
            ] = []

            def transaction_factory(
                *,
                _transactions=transactions,
                **kwargs,
            ):
                transaction = FinancialEligibilityTemporalEvaluationTransaction(
                    **kwargs
                )
                _transactions.append(transaction)
                return transaction

            acquisition_request = _matrix_request(
                asset=asset,
                plan=plan,
                binding=metadata_binding,
                label=f"{origin}-{subject}-{name}-{mode.value}",
            )
            result = FinancialEligibilityMetadataAcquirer(
                routing_plan=plan,
                router=MainlandFinancialCapabilityRouter(
                    routing_plan=plan,
                    statement_sources={},
                    company_type_evidence_sources=(
                        {"tushare": company_source}
                        if origin == "fresh"
                        else {}
                    ),
                    capability_sources=(
                        {
                            MainlandCapability.ISSUER_LIFECYCLE: {
                                "baostock": listing_source
                            }
                        }
                        if origin == "fresh"
                        else {}
                    ),
                ),
                stop_signal=Event(),
                temporal_transaction_factory=transaction_factory,
            ).acquire(
                acquisition_request,
                retained_company_type_evidence=(
                    (company_projection,) if origin == "retained" else ()
                ),
                retained_listing_metadata=(
                    (listing_projection,) if origin == "retained" else ()
                ),
            )
            assert len(transactions) == 1
            assert transactions[0].evaluation_count in {1, 2}
            if expected == "accepted":
                assert result.context is not None, (origin, subject, name, mode)
                temporal = result.context.temporal_eligibility_binding
                assert temporal is not None
                acquired_decision = (
                    temporal.company_type_temporal_decision
                    if subject == "company"
                    else temporal.listing_temporal_decision
                )
            else:
                assert result.failure is not None, (origin, subject, name, mode)
                assert result.failure.reason is (
                    FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED
                )
                acquired_decision = result.failure.rejected_temporal_decision
                assert acquired_decision is not None
            assert acquired_decision == decision
            replay_records.append((acquisition_request, result, decision))

    monkeypatch.setattr(
        temporal_module,
        "evaluate_financial_metadata_temporal_eligibility",
        lambda **_kwargs: pytest.fail("temporal replay invoked the evaluator"),
    )
    monkeypatch.setattr(
        temporal_module,
        "latest_completed_session",
        lambda *_args, **_kwargs: pytest.fail(
            "temporal replay consulted the restore-time frontier"
        ),
    )
    monkeypatch.setattr(
        FinancialEligibilityMetadataAcquirer,
        "acquire",
        lambda *_args, **_kwargs: pytest.fail(
            "temporal replay performed metadata acquisition"
        ),
    )
    monkeypatch.setattr(
        ProviderSubrequestCache,
        "execute",
        lambda *_args, **_kwargs: pytest.fail(
            "temporal replay performed provider/cache work"
        ),
    )
    monkeypatch.setattr(
        FinancialEligibilityAcquisitionContextOwner,
        "acquire_context",
        lambda *_args, **_kwargs: pytest.fail(
            "temporal replay sampled a clock or acquired a new context"
        ),
    )
    for name, mode, decision in recorded:
        replayed = FinancialMetadataTemporalEligibilityDecision.model_validate_json(
            decision.model_dump_json()
        )
        assert replayed is not decision
        assert replayed == decision, (origin, subject, name, mode)
        assert replayed.decision_identity == decision.decision_identity
        assert replayed.result == decision.result
        assert replayed.reasons == decision.reasons
        assert replayed.calendar_binding == decision.calendar_binding
        assert replayed.identity_payload() == decision.identity_payload()
    assert len(replay_records) == len(recorded)
    for request, result, decision in replay_records:
        if request is not None:
            restored_request = FinancialEligibilityMetadataAcquisitionRequest.model_validate(
                {
                    **request.model_dump(mode="python"),
                    "asset_configuration": request.asset_configuration,
                }
            )
            assert restored_request == request
        restored_result = FinancialEligibilityAcquisitionResult.model_validate_json(
            result.model_dump_json()
        )
        assert restored_result == result
        if restored_result.context is not None:
            temporal = restored_result.context.temporal_eligibility_binding
            assert temporal is not None
            restored_decisions = (
                temporal.company_type_temporal_decision,
                temporal.listing_temporal_decision,
            )
            assert decision in restored_decisions
        else:
            assert restored_result.failure is not None
            assert restored_result.failure.rejected_temporal_decision == decision


class _FailingTemporalEvaluationTransaction(
    FinancialEligibilityTemporalEvaluationTransaction
):
    def __init__(self, *, fail_on: int, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.fail_on = fail_on
        self.calls = 0

    def evaluate(
        self,
        evidence: FinancialMetadataTemporalEvidence,
    ):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("sensitive-evaluator-detail")
        return super().evaluate(evidence)


@pytest.mark.unit
def test_temporal_company_evidence_retains_an_exact_provider_proof_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    company, _listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc),
    )

    source = company.evidence.provider_temporal_proof
    assert source is not None
    proof = company.evidence.temporal_evidence
    assert proof is not None
    assert source.provider_id == "tushare"
    assert source.provider_effective_source_field == "end_date"
    assert source.artifact == proof.artifact
    assert source.evidence_effective_as_of == proof.evidence_effective_as_of
    assert (
        source.evidence_effective_as_of_derivation
        == proof.evidence_effective_as_of_derivation
    )
    assert source.ann_date == proof.ann_date
    assert source.ann_date_derivation == proof.ann_date_derivation
    assert source.f_ann_date == proof.f_ann_date
    assert source.f_ann_date_derivation == proof.f_ann_date_derivation


@pytest.mark.unit
def test_temporal_listing_route_uses_v2_outer_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    _company, listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc),
    )

    response = FinancialCapabilityProviderResponse.available(
        provider_id="baostock",
        capability=MainlandCapability.ISSUER_LIFECYCLE,
        retained_artifact_identities=(
            listing.metadata.temporal_evidence.artifact.artifact_identity,
        ),
        candidate_count=1,
        subrequest_keys=(),
        physical_attempt_ids=(),
        listing_metadata=listing.metadata,
    )
    routed = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": lambda _request: response,
            }
        },
    ).route_capability(
        FinancialCapabilityRoutingRequest(
            instrument_identity=asset.instrument_identity,
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            range_start=date(2026, 8, 10),
            as_of_date=date(2026, 8, 10),
        )
    )

    assert response.contract_version == "financial-capability-provider-response-v2"
    assert routed.contract_version == "financial-capability-routing-result-v2"


@pytest.mark.unit
@pytest.mark.parametrize("fail_on", (1, 2))
def test_retained_temporal_evaluator_exception_fails_typed_and_unbound(
    monkeypatch: pytest.MonkeyPatch,
    fail_on: int,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    evaluated_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    company, listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=evaluated_at,
    )
    transactions: list[_FailingTemporalEvaluationTransaction] = []

    def transaction_factory(**kwargs):
        transaction = _FailingTemporalEvaluationTransaction(
            fail_on=fail_on,
            **kwargs,
        )
        transactions.append(transaction)
        return transaction

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        stop_signal=Event(),
        temporal_transaction_factory=transaction_factory,
    ).acquire(
        _temporal_request(asset, plan, evaluated_at=evaluated_at),
        retained_company_type_evidence=(company,),
        retained_listing_metadata=(listing,),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE
    )
    assert result.contract_version == "financial-eligibility-acquisition-result-v2"
    assert "sensitive-evaluator-detail" not in repr(result)
    assert len(transactions) == 1
    assert transactions[0].calls == fail_on


def _history_config(tmp_path) -> MarketHistoryConfig:
    root = tmp_path / "history"
    return MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=root / "market_history.sqlite3",
        payload_root=root / "payloads",
        backup_root=root / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )


class _FakeBaoStockLifecycleTransport:
    def __init__(self) -> None:
        self.lifecycle_calls = 0

    def query_lifecycle(self, **_kwargs: object) -> pd.DataFrame:
        self.lifecycle_calls += 1
        return pd.DataFrame(
            {
                "code": ["sh.601328"],
                "code_name": ["descriptive name only"],
                "ipoDate": ["2007-05-15"],
                "outDate": [""],
                "type": ["1"],
                "status": ["1"],
            }
        )

    def query_raw_daily(self, **_kwargs: object) -> pd.DataFrame:
        raise AssertionError("raw daily must not be called for eligibility metadata")

    def query_forward_adjustment_factors(self, **_kwargs: object) -> pd.DataFrame:
        raise AssertionError("factors must not be called for eligibility metadata")

    def query_session_status(self, **_kwargs: object) -> pd.DataFrame:
        raise AssertionError("status must not be called for eligibility metadata")


@pytest.mark.unit
def test_exact_retained_metadata_builds_context_without_source_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
    )
    acquirer = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    )

    result = acquirer.acquire(
        _request(asset, plan),
        retained_company_type_evidence=(_company_type_projection(asset, plan),),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.outcome == "success"
    assert result.context is not None
    assert result.failure is None
    assert result.context.company_type_resolution.company_type is FinancialCompanyType.BANK
    assert result.context.company_type_evidence.provider_id == "tushare"
    assert result.context.listing_date == date(1987, 4, 1)
    assert result.context.listing_provenance.provider_id == "baostock"


@pytest.mark.unit
def test_retained_temporal_proof_is_reevaluated_once_under_new_run_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    retained_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    current_at = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    company, listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=retained_at,
    )
    transactions: list[FinancialEligibilityTemporalEvaluationTransaction] = []

    def transaction_factory(**kwargs):
        transaction = FinancialEligibilityTemporalEvaluationTransaction(**kwargs)
        transactions.append(transaction)
        return transaction

    def forbidden_source(_request):
        raise AssertionError("compatible retained proof must not perform provider work")

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {"baostock": forbidden_source}
        },
        company_type_evidence_sources={"tushare": forbidden_source},
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
        temporal_transaction_factory=transaction_factory,
    ).acquire(
        _temporal_request(asset, plan, evaluated_at=current_at),
        retained_company_type_evidence=(company,),
        retained_listing_metadata=(listing,),
    )

    assert result.outcome == "success"
    assert result.contract_version == "financial-eligibility-acquisition-result-v2"
    assert result.context is not None
    assert result.context.contract_version == "financial-eligibility-acquisition-context-v2"
    temporal = result.context.temporal_eligibility_binding
    assert temporal is not None
    assert temporal.eligibility_evaluated_at == current_at
    assert temporal.company_type_temporal_decision != company.temporal_decision
    assert temporal.listing_temporal_decision != listing.temporal_decision
    assert len(transactions) == 1
    assert transactions[0].evaluation_count == 2


@pytest.mark.unit
def test_stale_retained_decision_rejects_without_rebinding_old_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    company, listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc),
    )
    transactions: list[FinancialEligibilityTemporalEvaluationTransaction] = []

    def transaction_factory(**kwargs):
        transaction = FinancialEligibilityTemporalEvaluationTransaction(**kwargs)
        transactions.append(transaction)
        return transaction

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        stop_signal=Event(),
        temporal_transaction_factory=transaction_factory,
    ).acquire(
        _temporal_request(
            asset,
            plan,
            evaluated_at=datetime(2026, 8, 11, 8, tzinfo=timezone.utc),
        ),
        retained_company_type_evidence=(company,),
        retained_listing_metadata=(listing,),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED
    )
    assert result.failure.rejected_temporal_decision is not None
    assert result.failure.rejected_temporal_decision != company.temporal_decision
    assert len(transactions) == 1
    assert transactions[0].evaluation_count == 1


@pytest.mark.unit
def test_first_use_single_flight_fixes_one_temporal_frontier_for_32_followers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    fixed_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    transactions: list[FinancialEligibilityTemporalEvaluationTransaction] = []
    counts = {
        "clock": 0,
        "calendar": 0,
        "metadata_acquisition": 0,
        "company_source": 0,
        "company_transport": 0,
        "lifecycle_source": 0,
        "lifecycle_transport": 0,
    }
    counts_lock = Lock()
    provider_attempt_ids: list[str] = []
    provider_outcomes: list[tuple[str, str, int]] = []
    source_errors: list[tuple[str, str, object]] = []
    calendar_entered = Event()
    release_calendar = Event()
    synchronization_timeout = 30.0

    def eligibility_clock() -> datetime:
        with counts_lock:
            counts["clock"] += 1
        return fixed_at

    def calendar_provider() -> StoredSessionCalendar:
        with counts_lock:
            counts["calendar"] += 1
        calendar_entered.set()
        assert release_calendar.wait(timeout=synchronization_timeout)
        return _stored_temporal_calendar()

    def transaction_factory(**kwargs):
        transaction = FinancialEligibilityTemporalEvaluationTransaction(**kwargs)
        transactions.append(transaction)
        return transaction

    run_id = _run_id("temporal-owner-fresh")
    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        tushare_upstream, tushare_name = upstream_service_identity_for_provider(
            "tushare",
            account_scope=plan.account_scope_label,
        )
        baostock_upstream, baostock_name = upstream_service_identity_for_provider(
            "baostock"
        )
        coordinator.register_upstream_service(
            tushare_upstream,
            tushare_name,
            account_scope=plan.account_scope_label,
        )
        coordinator.register_upstream_service(baostock_upstream, baostock_name)
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id=run_id,
        )

        def execute_fixture(
            *,
            provider_id: str,
            upstream_service_id: str,
            capacity_scope: str,
            requested_fields: tuple[str, ...],
            payload: bytes,
        ):
            key = ProviderSubrequestKey.create(
                provider_id=provider_id,
                upstream_service_id=upstream_service_id,
                account_scope=(
                    plan.account_scope_label
                    if provider_id == "tushare"
                    else "not-applicable"
                ),
                capacity_scope=capacity_scope,
                instrument_identity=asset.instrument_identity,
                requested_range_start="2026-08-10",
                requested_range_end="2026-08-10",
                requested_fields=requested_fields,
                as_of_date=date(2026, 8, 10),
                qualification_profile=plan.qualification_profile,
                normalizer_version=f"ticket-06b-{capacity_scope}-v1",
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
            )

            def physical_request() -> bytes:
                with counts_lock:
                    transport_name = (
                        "company_transport"
                        if provider_id == "tushare"
                        else "lifecycle_transport"
                    )
                    counts[transport_name] += 1
                return payload

            try:
                acquired = cache.execute(
                    key,
                    owner_id="ticket-06b-fresh-single-flight",
                    priority=RequestPriority.INTERACTIVE_MAINLAND,
                    now=lambda: fixed_at,
                    sleep=lambda _seconds: None,
                    lease_duration=timedelta(seconds=30),
                    operation=f"financial_eligibility:{capacity_scope}",
                    media_type="application/json",
                    physical_request=physical_request,
                )
            except Exception as exc:
                source_errors.append(
                    (provider_id, type(exc).__name__, getattr(exc, "reason", None))
                )
                raise
            provider_outcomes.append(
                (provider_id, acquired.outcome.kind.value, len(acquired.attempt_events))
            )
            return key, acquired

        def company_source(request):
            with counts_lock:
                counts["company_source"] += 1
            key, acquired = execute_fixture(
                provider_id="tushare",
                upstream_service_id=tushare_upstream,
                capacity_scope="financial_company_type_evidence",
                requested_fields=("ann_date", "end_date", "f_ann_date"),
                payload=b'{"company_type":"bank","fixture":"ticket-06b"}',
            )
            assert acquired.artifact is not None
            proof = _matrix_proof(
                subject="company",
                requested_session=date(2026, 8, 10),
                retrieved_at=datetime(2026, 8, 10, 16, 48, 7, tzinfo=timezone.utc),
                observed_at=datetime(2026, 8, 10, 16, 48, 7, tzinfo=timezone.utc),
                changes={},
                exact_scope=None,
            )
            evidence = _matrix_company_evidence(asset, date(2026, 8, 10), proof)
            attempt_ids = tuple(
                event.attempt_event_id for event in acquired.attempt_events
            )
            provider_attempt_ids.extend(attempt_ids)
            return FinancialCompanyTypeEvidenceProviderProjection(
                provider_id="tushare",
                metadata_binding=request.metadata_binding,
                source_projection_kind="tushare_statement",
                evidence=evidence,
                subrequest_keys=(key.subrequest_key,),
                physical_attempt_ids=attempt_ids,
            )

        def lifecycle_source(request):
            with counts_lock:
                counts["lifecycle_source"] += 1
            key, acquired = execute_fixture(
                provider_id="baostock",
                upstream_service_id=baostock_upstream,
                capacity_scope="issuer_lifecycle",
                requested_fields=("ann_date", "f_ann_date", "ipoDate"),
                payload=b'{"fixture":"ticket-06b","ipoDate":"2010-01-04"}',
            )
            assert acquired.artifact is not None
            proof = _matrix_proof(
                subject="listing",
                requested_session=date(2026, 8, 10),
                retrieved_at=datetime(2026, 8, 10, 16, 48, 7, tzinfo=timezone.utc),
                observed_at=datetime(2026, 8, 10, 16, 48, 7, tzinfo=timezone.utc),
                changes={},
                exact_scope=None,
            )
            metadata = _matrix_listing_metadata(asset, date(2026, 8, 10), proof)
            attempt_ids = tuple(
                event.attempt_event_id for event in acquired.attempt_events
            )
            provider_attempt_ids.extend(attempt_ids)
            return FinancialCapabilityProviderResponse.available(
                provider_id="baostock",
                capability=MainlandCapability.ISSUER_LIFECYCLE,
                retained_artifact_identities=(proof.artifact.artifact_identity,),
                candidate_count=1,
                subrequest_keys=(key.subrequest_key,),
                physical_attempt_ids=attempt_ids,
                listing_metadata=metadata,
            )

        router = MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            capability_sources={
                MainlandCapability.ISSUER_LIFECYCLE: {
                    "baostock": lifecycle_source
                }
            },
            company_type_evidence_sources={"tushare": company_source},
        )
        metadata_acquirer = FinancialEligibilityMetadataAcquirer(
            router=router,
            routing_plan=plan,
            stop_signal=Event(),
            temporal_transaction_factory=transaction_factory,
        )
        original_acquire = metadata_acquirer.acquire

        def counted_metadata_acquisition(*args, **kwargs):
            with counts_lock:
                counts["metadata_acquisition"] += 1
            return original_acquire(*args, **kwargs)

        metadata_acquirer.acquire = counted_metadata_acquisition
        owner = FinancialEligibilityAcquisitionContextOwner(
            run_id=run_id,
            asset_configuration=asset,
            routing_plan=plan,
            metadata_acquirer=metadata_acquirer,
            unbound_financial_routing=QualifiedFinancialRoutingComposition(
                router=router,
                statement_request_factory=lambda *_args: None,
                provider_subrequest_cache=cache,
            ),
            expected_as_of_date=date(2026, 8, 10),
            availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            eligibility_clock=eligibility_clock,
            calendar_provider=calendar_provider,
            interrupt_runtime=lambda: None,
            close_runtime=lambda: None,
        )
        start = Barrier(33)

        def acquire():
            start.wait(timeout=synchronization_timeout)
            return owner.acquire_context(as_of_date=date(2026, 8, 10))

        with ThreadPoolExecutor(max_workers=32) as executor:
            futures = [executor.submit(acquire) for _ in range(32)]
            start.wait(timeout=synchronization_timeout)
            assert calendar_entered.wait(timeout=synchronization_timeout)
            release_calendar.set()
            results = tuple(
                future.result(timeout=synchronization_timeout) for future in futures
            )
        checkpoint = cache.checkpoint()

    assert not source_errors, source_errors
    assert counts["company_source"] == counts["lifecycle_source"] == 1, (
        counts,
        provider_outcomes,
        len(transactions),
        tuple(transaction.evaluation_count for transaction in transactions),
    )
    assert all(result.outcome == "success" for result in results), results[0].failure
    assert provider_outcomes == [
        ("tushare", "available", 1),
        ("baostock", "available", 1),
    ]
    assert counts == {
        "clock": 1,
        "calendar": 1,
        "metadata_acquisition": 1,
        "company_source": 1,
        "company_transport": 1,
        "lifecycle_source": 1,
        "lifecycle_transport": 1,
    }
    assert len(transactions) == 1
    assert transactions[0].evaluation_count == 2
    assert len(provider_attempt_ids) == len(set(provider_attempt_ids)) == 2
    assert checkpoint["contract_version"] == "1.2"
    assert len(checkpoint["entries"]) == 2
    assert sum(
        len(entry["attempt_events"]) for entry in checkpoint["entries"]
    ) == 2
    assert len({id(result) for result in results}) == 1
    assert results[0].outcome == "success", results[0].failure
    assert results[0].context is owner.context
    assert owner.context is not None
    temporal = owner.context.temporal_eligibility_binding
    assert temporal is not None
    assert temporal.eligibility_evaluated_at == fixed_at
    assert temporal.calendar_binding == _temporal_calendar(fixed_at)


@pytest.mark.unit
def test_fresh_temporal_company_type_route_uses_the_active_transaction_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    evaluated_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    company, _listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=evaluated_at,
    )

    def source(
        request: FinancialCompanyTypeEvidenceRoutingRequest,
    ) -> FinancialCompanyTypeEvidenceProviderProjection:
        return FinancialCompanyTypeEvidenceProviderProjection(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=company.evidence,
        )

    transaction = FinancialEligibilityTemporalEvaluationTransaction(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        calendar_binding=_temporal_calendar(evaluated_at),
    )
    routed = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={"tushare": source},
    ).route_company_type_evidence(
        FinancialCompanyTypeEvidenceRoutingRequest(
            contract_version="financial-company-type-evidence-routing-request-v2",
            metadata_binding=company.metadata_binding,
        ),
        temporal_transaction=transaction,
    )

    assert transaction.evaluation_count == 1
    assert (
        routed.contract_version
        == "financial-company-type-evidence-routing-result-v2"
    )
    assert routed.provider_calls[-1].temporal_decision is not None
    assert routed.provider_calls[-1].temporal_decision.result == "accepted"
    assert routed.physical_attempt_ids == ()

    with pytest.raises(
        ValueError,
        match="available company-type temporal proof is unavailable",
    ):
        FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=company.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=company.evidence,
        )


@pytest.mark.unit
def test_temporal_company_type_route_continues_after_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    evaluated_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    company, _listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=evaluated_at,
    )
    calls: list[str] = []

    def evidence_for(
        *,
        provider_id: str,
        proof: FinancialMetadataTemporalEvidence,
    ) -> FinancialCompanyTypeAcquisitionEvidenceBinding:
        return FinancialCompanyTypeAcquisitionEvidenceBinding.create(
            instrument_identity=asset.instrument_identity,
            requested_session=date(2026, 8, 10),
            temporal_evidence=proof,
            provider_temporal_proof=(
                FinancialCompanyTypeProviderTemporalProofBinding.from_temporal_evidence(
                    provider_id=provider_id,
                    evidence=proof,
                    provider_effective_source_field="报告日",
                )
                if provider_id == "akshare_sina"
                else None
            ),
            provider_id=provider_id,
            artifact_identity=proof.artifact.artifact_identity,
            source_ref=proof.artifact.artifact_identity,
            observed_at=proof.observed_at,
            retrieved_at=proof.retrieved_at,
            provider_declared_type=FinancialCompanyType.BANK,
            provider_declaration_qualified=True,
            normalized_resolver_inputs=(
                FinancialCompanyTypeClassifierInput(
                    key="metadata:industry",
                    value="bank",
                ),
            ),
        )

    rejected_proof = _temporal_proof(
        evidence_class=TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
        artifact_identity="financial-provider-artifact:v1:" + "8" * 64,
        row_identity="financial-period-candidate:v1:" + "9" * 64,
        effective=date(2026, 8, 11),
        effective_semantics=EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END,
        effective_source_field="end_date",
        publication_semantics=(
            ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
        ),
        ann_source_field="ann_date",
        first_source_field="f_ann_date",
    )
    accepted_proof = _temporal_proof(
        evidence_class=TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE,
        artifact_identity="financial-provider-artifact:v1:" + "a" * 64,
        row_identity="financial-period-candidate:v1:" + "b" * 64,
        effective=date(2026, 6, 30),
        effective_semantics=(
            EvidenceEffectiveAsOfSemantics.AKSHARE_SINA_STATEMENT_PERIOD_END
        ),
        effective_source_field="report_period",
        publication_semantics=(
            ProviderPublicationDateSemantics.AKSHARE_SINA_STATEMENT_PUBLICATION_FIELDS
        ),
        ann_source_field="公告日期",
        first_source_field="首次公告日期",
    )

    def source_for(provider_id: str, proof: FinancialMetadataTemporalEvidence):
        def source(
            request: FinancialCompanyTypeEvidenceRoutingRequest,
        ) -> FinancialCompanyTypeEvidenceProviderProjection:
            calls.append(provider_id)
            return FinancialCompanyTypeEvidenceProviderProjection(
                provider_id=provider_id,
                metadata_binding=request.metadata_binding,
                source_projection_kind=f"{provider_id}_statement",
                evidence=evidence_for(provider_id=provider_id, proof=proof),
            )

        return source

    transaction = FinancialEligibilityTemporalEvaluationTransaction(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        calendar_binding=_temporal_calendar(evaluated_at),
    )
    routed = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={
            "tushare": source_for("tushare", rejected_proof),
            "akshare_sina": source_for("akshare_sina", accepted_proof),
        },
    ).route_company_type_evidence(
        FinancialCompanyTypeEvidenceRoutingRequest(
            contract_version="financial-company-type-evidence-routing-request-v2",
            metadata_binding=company.metadata_binding,
        ),
        temporal_transaction=transaction,
    )

    assert calls == ["tushare", "akshare_sina"]
    assert routed.selected_provider == "akshare_sina"
    assert tuple(
        call.temporal_decision.result
        for call in routed.provider_calls
        if call.temporal_decision is not None
    ) == ("rejected", "accepted")
    assert transaction.evaluation_count == 2
    assert routed.physical_attempt_ids == ()


@pytest.mark.unit
def test_temporal_acquirer_preserves_route_order_rejection_when_no_source_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    evaluated_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    company, listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=evaluated_at,
    )
    proof = company.evidence.temporal_evidence
    assert proof is not None
    rejected_proof = proof.model_copy(
        update={"evidence_effective_as_of": date(2026, 8, 11)}
    )
    rejected_evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=asset.instrument_identity,
        requested_session=date(2026, 8, 10),
        temporal_evidence=rejected_proof,
        provider_id="tushare",
        artifact_identity=rejected_proof.artifact.artifact_identity,
        source_ref=rejected_proof.artifact.artifact_identity,
        observed_at=rejected_proof.observed_at,
        retrieved_at=rejected_proof.retrieved_at,
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        normalized_resolver_inputs=(
            FinancialCompanyTypeClassifierInput(key="metadata:industry", value="bank"),
        ),
    )
    transactions: list[FinancialEligibilityTemporalEvaluationTransaction] = []

    def source(
        request: FinancialCompanyTypeEvidenceRoutingRequest,
    ) -> FinancialCompanyTypeEvidenceProviderProjection:
        return FinancialCompanyTypeEvidenceProviderProjection(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=rejected_evidence,
        )

    def transaction_factory(**kwargs):
        transaction = FinancialEligibilityTemporalEvaluationTransaction(**kwargs)
        transactions.append(transaction)
        return transaction

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            company_type_evidence_sources={"tushare": source},
        ),
        stop_signal=Event(),
        temporal_transaction_factory=transaction_factory,
    ).acquire(
        _temporal_request(asset, plan, evaluated_at=evaluated_at),
        retained_listing_metadata=(listing,),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED
    )
    assert result.failure.rejected_temporal_decision is not None
    assert result.failure.rejected_temporal_decision.result == "rejected"
    assert len(transactions) == 1
    assert transactions[0].evaluation_count == 1


@pytest.mark.unit
def test_temporal_company_type_response_binds_requested_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    evaluated_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    company, _listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=evaluated_at,
    )
    mismatched_evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=company.evidence.instrument_identity,
        requested_session=date(2026, 8, 11),
        temporal_evidence=company.evidence.temporal_evidence,
        provider_id=company.evidence.provider_id,
        artifact_identity=company.evidence.artifact_identity,
        source_ref=company.evidence.source_ref,
        observed_at=company.evidence.observed_at,
        retrieved_at=company.evidence.retrieved_at,
        provider_declared_type=company.evidence.provider_declared_type,
        provider_declaration_qualified=(
            company.evidence.provider_declaration_qualified
        ),
        normalized_resolver_inputs=company.evidence.normalized_resolver_inputs,
    )

    with pytest.raises(
        ValueError,
        match="company-type evidence acquisition binding mismatch",
    ):
        FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=company.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=mismatched_evidence,
            temporal_decision=company.temporal_decision,
        )


@pytest.mark.unit
def test_fresh_baostock_lifecycle_uses_common_evaluator_and_fails_without_ann_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    evaluated_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    company, _listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=evaluated_at,
    )
    transport = _FakeBaoStockLifecycleTransport()
    transactions: list[FinancialEligibilityTemporalEvaluationTransaction] = []
    acquired_listing: list[FinancialListingMetadata] = []

    def transaction_factory(**kwargs):
        transaction = FinancialEligibilityTemporalEvaluationTransaction(**kwargs)
        transactions.append(transaction)
        return transaction

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_id, service_name = upstream_service_identity_for_provider("baostock")
        coordinator.register_upstream_service(upstream_id, service_name)
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="ticket-02h-baostock-temporal",
        )
        adapter = BaoStockCapabilityAdapter(
            routing_plan=plan,
            subrequest_cache=cache,
            transport=transport,
            owner_id="ticket-02h-baostock-temporal",
            now=lambda: _RETRIEVED_AT,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

        def lifecycle_source(request) -> FinancialCapabilityProviderResponse:
            acquired = adapter.acquire_lifecycle(
                instrument_identity=request.instrument_identity,
                as_of_date=request.as_of_date,
            )
            assert acquired.listing_metadata is not None
            assert acquired.artifact is not None
            acquired_listing.append(acquired.listing_metadata)
            response = FinancialCapabilityProviderResponse.available(
                provider_id="baostock",
                capability=MainlandCapability.ISSUER_LIFECYCLE,
                retained_artifact_identities=(acquired.artifact.artifact_identity,),
                candidate_count=1,
                subrequest_keys=(acquired.subrequest_key,),
                physical_attempt_ids=tuple(
                    event.attempt_event_id for event in acquired.attempt_events
                ),
                listing_metadata=acquired.listing_metadata,
            )
            assert response.contract_version == "financial-capability-provider-response-v2"
            return response

        router = MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            capability_sources={
                MainlandCapability.ISSUER_LIFECYCLE: {
                    "baostock": lifecycle_source,
                }
            },
        )
        result = FinancialEligibilityMetadataAcquirer(
            routing_plan=plan,
            router=router,
            stop_signal=Event(),
            temporal_transaction_factory=transaction_factory,
        ).acquire(
            _temporal_request(asset, plan, evaluated_at=evaluated_at),
            retained_company_type_evidence=(company,),
        )

    assert result.context is None
    assert result.failure is not None
    assert transport.lifecycle_calls == 1
    assert len(acquired_listing) == 1
    assert len(transactions) == 1
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED
    )
    decision = result.failure.rejected_temporal_decision
    assert decision is not None
    assert decision.evidence_class.value == "baostock_issuer_lifecycle_listing"
    assert "ann_date_missing" in {reason.value for reason in decision.reasons}
    assert transactions[0].evaluation_count == 2
    proof = acquired_listing[0].temporal_evidence
    assert proof is not None
    assert proof.evidence_effective_as_of == date(2007, 5, 15)
    assert proof.ann_date is None
    assert proof.ann_date_derivation is None


@pytest.mark.unit
def test_fresh_baostock_evaluator_exception_preserves_the_exact_provider_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset = _asset()
    plan = prepare_mainland_temporal_capability_routing_plan(
        _qualified_plan(monkeypatch, asset)
    )
    evaluated_at = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
    company, _listing = _retained_temporal_projections(
        asset,
        plan,
        evaluated_at=evaluated_at,
    )
    transport = _FakeBaoStockLifecycleTransport()
    transactions: list[_FailingTemporalEvaluationTransaction] = []
    provider_attempt_ids: list[str] = []

    def transaction_factory(**kwargs):
        transaction = _FailingTemporalEvaluationTransaction(
            fail_on=2,
            **kwargs,
        )
        transactions.append(transaction)
        return transaction

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_id, service_name = upstream_service_identity_for_provider("baostock")
        coordinator.register_upstream_service(upstream_id, service_name)
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="ticket-02h-baostock-evaluator-exception",
        )
        adapter = BaoStockCapabilityAdapter(
            routing_plan=plan,
            subrequest_cache=cache,
            transport=transport,
            owner_id="ticket-02h-baostock-evaluator-exception",
            now=lambda: _RETRIEVED_AT,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

        def lifecycle_source(request) -> FinancialCapabilityProviderResponse:
            acquired = adapter.acquire_lifecycle(
                instrument_identity=request.instrument_identity,
                as_of_date=request.as_of_date,
            )
            assert acquired.listing_metadata is not None
            assert acquired.artifact is not None
            attempt_ids = tuple(
                event.attempt_event_id for event in acquired.attempt_events
            )
            provider_attempt_ids.extend(attempt_ids)
            return FinancialCapabilityProviderResponse.available(
                provider_id="baostock",
                capability=MainlandCapability.ISSUER_LIFECYCLE,
                retained_artifact_identities=(acquired.artifact.artifact_identity,),
                candidate_count=1,
                subrequest_keys=(acquired.subrequest_key,),
                physical_attempt_ids=attempt_ids,
                listing_metadata=acquired.listing_metadata,
            )

        result = FinancialEligibilityMetadataAcquirer(
            routing_plan=plan,
            router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
                capability_sources={
                    MainlandCapability.ISSUER_LIFECYCLE: {
                        "baostock": lifecycle_source,
                    }
                },
            ),
            stop_signal=Event(),
            temporal_transaction_factory=transaction_factory,
        ).acquire(
            _temporal_request(asset, plan, evaluated_at=evaluated_at),
            retained_company_type_evidence=(company,),
        )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_PROOF_UNAVAILABLE
    )
    assert "sensitive-evaluator-detail" not in repr(result)
    assert transport.lifecycle_calls == 1
    assert len(provider_attempt_ids) == 1
    assert len(transactions) == 1
    assert transactions[0].calls == 2
    assert transactions[0].evaluation_count == 1


@pytest.mark.unit
def test_tushare_qualified_declaration_resolves_once_and_stops_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    calls: list[str] = []
    resolver_calls = 0
    original_resolver = metadata_module.resolve_financial_company_type

    def spy_resolver(**kwargs: object):
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(**kwargs)

    monkeypatch.setattr(metadata_module, "resolve_financial_company_type", spy_resolver)

    def source(provider_id: str, response: FinancialCompanyTypeEvidenceProviderResponse):
        def invoke(_request: object) -> FinancialCompanyTypeEvidenceProviderResponse:
            calls.append(provider_id)
            return response

        return invoke

    tushare = FinancialCompanyTypeEvidenceProviderResponse.available(
        provider_id="tushare",
        metadata_binding=_metadata_binding(asset, plan),
        source_projection_kind="tushare_statement",
        evidence=_company_type_evidence(
            "tushare",
            instrument_identity=asset.instrument_identity,
            declared_type=FinancialCompanyType.BANK,
            declaration_qualified=True,
            inputs=(
                FinancialCompanyTypeClassifierInput(
                    key="metadata:industry",
                    value="bank",
                ),
                FinancialCompanyTypeClassifierInput(
                    key="present_field",
                    value="customer_deposits",
                ),
            ),
        ),
    )
    lower = FinancialCompanyTypeEvidenceProviderResponse.unavailable(
        provider_id="akshare_sina",
        metadata_binding=_metadata_binding(asset, plan),
        reason="not_configured",
    )
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={
            "tushare": source("tushare", tushare),
            "akshare_sina": source("akshare_sina", lower),
            "yfinance": source("yfinance", lower.model_copy(update={"provider_id": "yfinance"})),
        },
    )
    acquirer = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    )

    result = acquirer.acquire(
        _request(asset, plan),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.outcome == "success"
    assert result.context is not None
    assert result.context.company_type_resolution.company_type is FinancialCompanyType.BANK
    assert result.context.company_type_resolution.method.value == "qualified_provider"
    assert calls == ["tushare"]
    assert resolver_calls == 1


@pytest.mark.unit
def test_baostock_lifecycle_route_supplies_authoritative_listing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    calls: list[str] = []
    listing_metadata = FinancialListingMetadata(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        listing_date=date(1987, 4, 1),
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref="provider-artifact:v1:" + "3" * 64,
            observed_at=_OBSERVED_AT,
        ),
    )

    def lifecycle_source(_request: object) -> FinancialCapabilityProviderResponse:
        calls.append("baostock")
        return FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            retained_artifact_identities=(listing_metadata.listing_provenance.source_ref,),
            candidate_count=1,
            subrequest_keys=("provider-subrequest:v1:" + "4" * 64,),
            physical_attempt_ids=("provider-physical-attempt=sha256:" + "5" * 64,),
            listing_metadata=listing_metadata,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": lifecycle_source,
            }
        },
    )
    acquirer = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    )

    result = acquirer.acquire(
        _request(asset, plan),
        retained_company_type_evidence=(_company_type_projection(asset, plan),),
    )

    assert result.outcome == "success"
    assert result.context is not None
    assert result.context.listing_date == date(1987, 4, 1)
    assert result.context.listing_provenance == listing_metadata.listing_provenance
    assert calls == ["baostock"]


@pytest.mark.unit
def test_context_cross_binding_mismatch_fails_before_source_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    calls: list[str] = []

    def must_not_call(request) -> FinancialCompanyTypeEvidenceProviderResponse:
        calls.append("tushare")
        return FinancialCompanyTypeEvidenceProviderResponse.unavailable(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            reason="provider_error",
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={"tushare": must_not_call},
    )
    acquirer = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    )
    request = _request(asset, plan).model_copy(
        update={"routing_plan_signature": "mainland-routing-plan:v1:" + "0" * 64}
    )

    result = acquirer.acquire(request)

    assert result.outcome == "failure"
    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "context_binding_mismatch"
    assert calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("selected_provider", "unavailable_providers", "expected_calls"),
    [
        ("akshare_sina", ("tushare",), ("tushare", "akshare_sina")),
        (
            "yfinance",
            ("tushare", "akshare_sina"),
            ("tushare", "akshare_sina", "yfinance"),
        ),
    ],
)
def test_company_type_fallback_uses_only_the_plan_prefix(
    monkeypatch: pytest.MonkeyPatch,
    selected_provider: str,
    unavailable_providers: tuple[str, ...],
    expected_calls: tuple[str, ...],
) -> None:
    asset = _asset("600895.SS")
    plan = _qualified_plan(monkeypatch, asset)
    calls: list[str] = []

    def source(provider_id: str):
        def invoke(request) -> FinancialCompanyTypeEvidenceProviderResponse:
            calls.append(provider_id)
            if provider_id in unavailable_providers:
                return FinancialCompanyTypeEvidenceProviderResponse.unavailable(
                    provider_id=provider_id,
                    metadata_binding=request.metadata_binding,
                    reason="provider_error",
                )
            return FinancialCompanyTypeEvidenceProviderResponse.available(
                provider_id=provider_id,
                metadata_binding=request.metadata_binding,
                source_projection_kind={
                    "tushare": "tushare_statement",
                    "akshare_sina": "akshare_sina_statement",
                    "yfinance": "yfinance_current_profile",
                }[provider_id],
                evidence=_company_type_evidence(
                    provider_id,
                    instrument_identity=asset.instrument_identity,
                    declared_type=None,
                    declaration_qualified=False,
                    inputs=(
                        FinancialCompanyTypeClassifierInput(
                            key="metadata:industry",
                            value="industrial",
                        ),
                        FinancialCompanyTypeClassifierInput(
                            key="present_field",
                            value="inventory",
                        ),
                    ),
                ),
            )

        return invoke

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={
            provider_id: source(provider_id)
            for provider_id in plan.route_for(MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE)
        },
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.outcome == "success"
    assert result.context is not None
    assert result.context.company_type_evidence.provider_id == selected_provider
    assert result.context.company_type_resolution.company_type is (
        FinancialCompanyType.INDUSTRIAL_NON_BANK
    )
    assert tuple(calls) == expected_calls
    assert (
        tuple(calls)
        == plan.route_for(MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE)[: len(calls)]
    )


@pytest.mark.unit
def test_yahoo_cannot_claim_qualified_provider_declaration_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    evidence = _company_type_evidence(
        "yfinance",
        instrument_identity=asset.instrument_identity,
        declared_type=FinancialCompanyType.BANK,
        declaration_qualified=True,
        inputs=(),
    )

    with pytest.raises(ValueError, match="classifier-only"):
        FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="yfinance",
            metadata_binding=_metadata_binding(asset, plan),
            source_projection_kind="yfinance_current_profile",
            evidence=evidence,
        )


@pytest.mark.unit
def test_company_type_source_projection_must_match_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)

    with pytest.raises(ValueError, match="inconsistent"):
        FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=_metadata_binding(asset, plan),
            source_projection_kind="yfinance_current_profile",
            evidence=_company_type_evidence(
                "tushare",
                instrument_identity=asset.instrument_identity,
                declared_type=FinancialCompanyType.BANK,
                declaration_qualified=True,
                inputs=(),
            ),
        )


@pytest.mark.unit
def test_unwhitelisted_company_type_classifier_input_fails_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)

    def source(request) -> FinancialCompanyTypeEvidenceProviderResponse:
        return FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=_company_type_evidence(
                "tushare",
                instrument_identity=asset.instrument_identity,
                declared_type=None,
                declaration_qualified=False,
                inputs=(
                    FinancialCompanyTypeClassifierInput(
                        key="metadata:unapproved_hint",
                        value="bank",
                    ),
                ),
            ),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={"tushare": source},
    )

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "company_type_evidence_unavailable"


@pytest.mark.unit
def test_unknown_company_type_returns_typed_unresolved_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    projection = FinancialCompanyTypeEvidenceProjection(
        metadata_binding=_metadata_binding(asset, plan),
        evidence=_company_type_evidence(
            "tushare",
            instrument_identity=asset.instrument_identity,
            declared_type=None,
            declaration_qualified=False,
            inputs=(),
        ),
    )
    router = MainlandFinancialCapabilityRouter(routing_plan=plan, statement_sources={})

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(projection,),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "company_type_unresolved"
    assert result.failure.company_type_resolution is not None
    assert result.failure.company_type_resolution.company_type is FinancialCompanyType.UNKNOWN


@pytest.mark.unit
def test_ambiguous_company_type_returns_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset("600895.SS")
    plan = _qualified_plan(monkeypatch, asset)
    projection = FinancialCompanyTypeEvidenceProjection(
        metadata_binding=_metadata_binding(asset, plan),
        evidence=_company_type_evidence(
            "akshare_sina",
            instrument_identity=asset.instrument_identity,
            declared_type=None,
            declaration_qualified=False,
            inputs=(
                FinancialCompanyTypeClassifierInput(
                    key="present_field",
                    value="cash",
                ),
            ),
        ),
    )
    router = MainlandFinancialCapabilityRouter(routing_plan=plan, statement_sources={})

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(projection,),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "company_type_ambiguous"
    assert result.failure.company_type_resolution is not None
    assert result.failure.company_type_resolution.ambiguous is True


@pytest.mark.unit
def test_contradictory_company_type_returns_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    projection = FinancialCompanyTypeEvidenceProjection(
        metadata_binding=_metadata_binding(asset, plan),
        evidence=_company_type_evidence(
            "tushare",
            instrument_identity=asset.instrument_identity,
            declared_type=FinancialCompanyType.BANK,
            declaration_qualified=True,
            inputs=(
                FinancialCompanyTypeClassifierInput(
                    key="metadata:industry",
                    value="industrial",
                ),
                FinancialCompanyTypeClassifierInput(
                    key="present_field",
                    value="inventory",
                ),
            ),
        ),
    )
    router = MainlandFinancialCapabilityRouter(routing_plan=plan, statement_sources={})

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(projection,),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "company_type_contradictory"
    assert result.failure.company_type_resolution is not None
    assert result.failure.company_type_resolution.contradictory is True


@pytest.mark.unit
def test_missing_company_type_evidence_returns_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    router = MainlandFinancialCapabilityRouter(routing_plan=plan, statement_sources={})

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(_request(asset, plan))

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "company_type_evidence_unavailable"


@pytest.mark.unit
def test_missing_listing_provenance_returns_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    router = MainlandFinancialCapabilityRouter(routing_plan=plan, statement_sources={})

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(_company_type_projection(asset, plan),),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "listing_provenance_unavailable"


@pytest.mark.unit
def test_non_authoritative_listing_projection_returns_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    provenance = FinancialListingProvenance.model_construct(
        authoritative=False,
        provider_id="baostock",
        source_ref="provider-artifact:v1:" + "6" * 64,
        observed_at=_OBSERVED_AT,
    )
    unsafe_metadata = FinancialListingMetadata.model_construct(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        listing_date=date(1987, 4, 1),
        listing_provenance=provenance,
    )
    valid = FinancialCapabilityProviderResponse.available(
        provider_id="baostock",
        capability=MainlandCapability.ISSUER_LIFECYCLE,
        retained_artifact_identities=("provider-artifact:v1:" + "7" * 64,),
        candidate_count=1,
        subrequest_keys=(),
        physical_attempt_ids=(),
        listing_metadata=FinancialListingMetadata(
            instrument_identity=asset.instrument_identity,
            as_of_date=_AS_OF,
            listing_date=date(1987, 4, 1),
            listing_provenance=FinancialListingProvenance(
                provider_id="baostock",
                source_ref="provider-artifact:v1:" + "7" * 64,
                observed_at=_OBSERVED_AT,
            ),
        ),
    )
    unsafe = valid.model_copy(
        update={
            "retained_artifact_identities": (provenance.source_ref,),
            "listing_metadata": unsafe_metadata,
        }
    )
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": lambda _request: unsafe,
            }
        },
    )

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(_company_type_projection(asset, plan),),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "listing_provenance_unavailable"


@pytest.mark.unit
def test_interruption_returns_typed_failure_without_partial_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    stop_signal = Event()
    stop_signal.set()
    router = MainlandFinancialCapabilityRouter(routing_plan=plan, statement_sources={})

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=stop_signal,
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(_company_type_projection(asset, plan),),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "interrupted_acquisition"


@pytest.mark.unit
def test_interruption_after_company_type_publishes_no_partial_context_or_listing_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    stop_signal = Event()
    listing_calls = 0

    def company_source(request) -> FinancialCompanyTypeEvidenceProviderResponse:
        stop_signal.set()
        return FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=_company_type_projection(asset, plan).evidence,
        )

    def lifecycle_source(_request: object) -> FinancialCapabilityProviderResponse:
        nonlocal listing_calls
        listing_calls += 1
        raise AssertionError("listing must not start after interruption")

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={"tushare": company_source},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": lifecycle_source,
            }
        },
    )

    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=stop_signal,
    ).acquire(_request(asset, plan))

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "interrupted_acquisition"
    assert listing_calls == 0


@pytest.mark.unit
def test_interruption_during_company_type_fallback_stops_lower_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    stop_signal = Event()
    calls: list[str] = []

    def tushare_source(request) -> FinancialCompanyTypeEvidenceProviderResponse:
        calls.append("tushare")
        stop_signal.set()
        return FinancialCompanyTypeEvidenceProviderResponse.unavailable(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            reason="provider_error",
        )

    def lower_source(request) -> FinancialCompanyTypeEvidenceProviderResponse:
        calls.append("akshare_sina")
        return FinancialCompanyTypeEvidenceProviderResponse.unavailable(
            provider_id="akshare_sina",
            metadata_binding=request.metadata_binding,
            reason="provider_error",
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={
            "tushare": tushare_source,
            "akshare_sina": lower_source,
        },
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=stop_signal,
    ).acquire(_request(asset, plan))

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason.value == "interrupted_acquisition"
    assert calls == ["tushare"]


@pytest.mark.unit
def test_retained_projection_and_resolver_input_order_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    inputs = (
        FinancialCompanyTypeClassifierInput(
            key="present_field",
            value="customer_deposits",
        ),
        FinancialCompanyTypeClassifierInput(
            key="metadata:industry",
            value="bank",
        ),
    )
    tushare = FinancialCompanyTypeEvidenceProjection(
        metadata_binding=_metadata_binding(asset, plan),
        evidence=_company_type_evidence(
            "tushare",
            instrument_identity=asset.instrument_identity,
            declared_type=FinancialCompanyType.BANK,
            declaration_qualified=True,
            inputs=inputs,
        ),
    )
    akshare = FinancialCompanyTypeEvidenceProjection(
        metadata_binding=_metadata_binding(asset, plan),
        evidence=_company_type_evidence(
            "akshare_sina",
            instrument_identity=asset.instrument_identity,
            declared_type=None,
            declaration_qualified=False,
            inputs=tuple(reversed(inputs)),
        ),
    )
    router = MainlandFinancialCapabilityRouter(routing_plan=plan, statement_sources={})
    acquirer = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    )
    request = _request(asset, plan)
    listing = (_listing_projection(asset, plan),)

    first = acquirer.acquire(
        request,
        retained_company_type_evidence=(akshare, tushare),
        retained_listing_metadata=listing,
    )
    second = acquirer.acquire(
        request,
        retained_company_type_evidence=(tushare, akshare),
        retained_listing_metadata=listing,
    )

    assert first.outcome == second.outcome == "success"
    assert first.context is not None and second.context is not None
    assert first.context.context_identity == second.context.context_identity
    assert first.context.company_type_evidence.provider_id == "tushare"
    assert first.context.company_type_resolution.classifier_inputs == tuple(
        sorted(inputs, key=lambda item: (item.key, item.value))
    )


@pytest.mark.unit
@pytest.mark.parametrize("binding_kind", ("expired", "wrong_asset", "wrong_plan"))
def test_incompatible_retained_company_type_projection_is_reacquired(
    monkeypatch: pytest.MonkeyPatch,
    binding_kind: str,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    incompatible_binding = _metadata_binding(asset, plan)
    if binding_kind == "expired":
        incompatible_binding = incompatible_binding.model_copy(
            update={"as_of_date": _AS_OF - timedelta(days=1)}
        )
    elif binding_kind == "wrong_asset":
        incompatible_binding = incompatible_binding.model_copy(
            update={"instrument_identity": _asset("600895.SS").instrument_identity}
        )
    else:
        incompatible_binding = incompatible_binding.model_copy(
            update={"routing_plan_signature": "mainland-routing-plan:v1:" + "b" * 64}
        )
    retained = FinancialCompanyTypeEvidenceProjection.model_construct(
        metadata_binding=incompatible_binding,
        evidence=_company_type_projection(asset, plan).evidence,
    )
    calls = 0

    def source(request) -> FinancialCompanyTypeEvidenceProviderResponse:
        nonlocal calls
        calls += 1
        return FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=_company_type_projection(asset, plan).evidence,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={"tushare": source},
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(retained,),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.outcome == "success"
    assert calls == 1


@pytest.mark.unit
def test_expired_company_type_observation_is_reacquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    expired_as_of = _AS_OF - timedelta(days=1)
    expired_evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=asset.instrument_identity,
        as_of_date=expired_as_of,
        provider_id="tushare",
        artifact_identity="provider-artifact:v1:" + "d" * 64,
        source_ref="acq.v1:financial.company-type:tushare",
        observed_at=_OBSERVED_AT - timedelta(days=1),
        retrieved_at=_RETRIEVED_AT - timedelta(days=1),
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        normalized_resolver_inputs=(),
    )
    retained = FinancialCompanyTypeEvidenceProjection.model_construct(
        metadata_binding=_metadata_binding(asset, plan),
        evidence=expired_evidence,
    )
    calls = 0

    def source(request) -> FinancialCompanyTypeEvidenceProviderResponse:
        nonlocal calls
        calls += 1
        return FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=request.metadata_binding,
            source_projection_kind="tushare_statement",
            evidence=_company_type_projection(asset, plan).evidence,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={"tushare": source},
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(retained,),
        retained_listing_metadata=(_listing_projection(asset, plan),),
    )

    assert result.outcome == "success"
    assert calls == 1


@pytest.mark.unit
def test_expired_retained_listing_projection_is_reacquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    expired_as_of = _AS_OF - timedelta(days=1)
    expired_binding = _metadata_binding(asset, plan).model_copy(
        update={"as_of_date": expired_as_of}
    )
    expired = FinancialListingMetadataProjection(
        metadata_binding=expired_binding,
        metadata=FinancialListingMetadata(
            instrument_identity=asset.instrument_identity,
            as_of_date=expired_as_of,
            listing_date=date(1987, 4, 1),
            listing_provenance=FinancialListingProvenance(
                provider_id="baostock",
                source_ref="provider-artifact:v1:" + "c" * 64,
                observed_at=_OBSERVED_AT,
            ),
        ),
    )
    calls = 0
    current_metadata = _listing_projection(asset, plan).metadata

    def lifecycle_source(
        _request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        nonlocal calls
        calls += 1
        return FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            retained_artifact_identities=(current_metadata.listing_provenance.source_ref,),
            candidate_count=1,
            subrequest_keys=(),
            physical_attempt_ids=(),
            listing_metadata=current_metadata,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": lifecycle_source,
            }
        },
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(_company_type_projection(asset, plan),),
        retained_listing_metadata=(expired,),
    )

    assert result.outcome == "success"
    assert calls == 1


@pytest.mark.unit
def test_listing_observation_local_date_is_not_a_retention_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    expired = FinancialListingMetadataProjection(
        metadata_binding=_metadata_binding(asset, plan),
        metadata=FinancialListingMetadata(
            instrument_identity=asset.instrument_identity,
            as_of_date=_AS_OF,
            listing_date=date(1987, 4, 1),
            listing_provenance=FinancialListingProvenance(
                provider_id="baostock",
                source_ref="provider-artifact:v1:" + "e" * 64,
                observed_at=_OBSERVED_AT - timedelta(days=1),
            ),
        ),
    )
    calls = 0
    current_metadata = _listing_projection(asset, plan).metadata

    def lifecycle_source(
        _request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        nonlocal calls
        calls += 1
        return FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            retained_artifact_identities=(current_metadata.listing_provenance.source_ref,),
            candidate_count=1,
            subrequest_keys=(),
            physical_attempt_ids=(),
            listing_metadata=current_metadata,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": lifecycle_source,
            }
        },
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    ).acquire(
        _request(asset, plan),
        retained_company_type_evidence=(_company_type_projection(asset, plan),),
        retained_listing_metadata=(expired,),
    )

    assert result.outcome == "success"
    assert calls == 0


@pytest.mark.unit
def test_raw_source_exception_text_does_not_enter_failure_or_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    canary = "token=must-not-escape https://provider.invalid/path?secret=yes"

    def unsafe_source(_request: object) -> FinancialCompanyTypeEvidenceProviderResponse:
        raise RuntimeError(canary)

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={"tushare": unsafe_source},
    )
    acquirer = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=router,
        stop_signal=Event(),
    )

    result = acquirer.acquire(_request(asset, plan))

    assert result.failure is not None
    assert result.failure.reason.value == "company_type_evidence_unavailable"
    assert canary not in repr(result)
    assert canary not in result.model_dump_json()
    assert canary not in repr(acquirer)


@pytest.mark.unit
def test_physical_prerequisite_calls_match_attempts_and_cache_reuse_adds_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    tushare_physical_calls = 0
    company_responses: list[FinancialCompanyTypeEvidenceProviderResponse] = []
    lifecycle_responses: list[FinancialCapabilityProviderResponse] = []
    baostock_transport = _FakeBaoStockLifecycleTransport()

    with MarketHistoryStore.open(_history_config(tmp_path)) as store:
        coordinator = ProviderRequestCoordinator(store)
        tushare_upstream, tushare_name = upstream_service_identity_for_provider(
            "tushare",
            account_scope=plan.account_scope_label,
        )
        baostock_upstream, baostock_name = upstream_service_identity_for_provider("baostock")
        coordinator.register_upstream_service(
            tushare_upstream,
            tushare_name,
            account_scope=plan.account_scope_label,
        )
        coordinator.register_upstream_service(baostock_upstream, baostock_name)
        cache = ProviderSubrequestCache(
            coordinator=coordinator,
            artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
            run_scope_id="ticket-02b-physical-prerequisites",
        )

        def company_source(request) -> FinancialCompanyTypeEvidenceProviderResponse:
            nonlocal tushare_physical_calls
            key = ProviderSubrequestKey.create(
                provider_id="tushare",
                upstream_service_id=tushare_upstream,
                account_scope=plan.account_scope_label,
                capacity_scope="financial_company_type_evidence",
                instrument_identity=request.metadata_binding.instrument_identity,
                requested_range_start=request.metadata_binding.as_of_date.isoformat(),
                requested_range_end=request.metadata_binding.as_of_date.isoformat(),
                requested_fields=("comp_type", "industry", "customer_deposits"),
                as_of_date=request.metadata_binding.as_of_date,
                qualification_profile=plan.qualification_profile,
                normalizer_version="financial-company-type-evidence-v1",
                data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
            )

            def physical_request() -> bytes:
                nonlocal tushare_physical_calls
                tushare_physical_calls += 1
                return json.dumps(
                    {
                        "provider_declared_type": "bank",
                        "industry": "bank",
                        "present_fields": ["customer_deposits"],
                    },
                    sort_keys=True,
                ).encode("utf-8")

            acquired = cache.execute(
                key,
                owner_id="ticket-02b-owner",
                priority=RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: _RETRIEVED_AT,
                sleep=lambda _seconds: None,
                lease_duration=timedelta(seconds=30),
                operation="financial_company_type_evidence:tushare",
                media_type="application/json",
                physical_request=physical_request,
            )
            attempt_ids = tuple(event.attempt_event_id for event in acquired.attempt_events)
            if acquired.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
                response = FinancialCompanyTypeEvidenceProviderResponse.unavailable(
                    provider_id="tushare",
                    metadata_binding=request.metadata_binding,
                    reason="provider_error",
                    subrequest_keys=(key.subrequest_key,),
                    physical_attempt_ids=attempt_ids,
                )
            else:
                assert acquired.value is not None and acquired.artifact is not None
                payload = json.loads(acquired.value)
                evidence = _company_type_evidence(
                    "tushare",
                    instrument_identity=request.metadata_binding.instrument_identity,
                    declared_type=FinancialCompanyType(payload["provider_declared_type"]),
                    declaration_qualified=True,
                    inputs=(
                        FinancialCompanyTypeClassifierInput(
                            key="metadata:industry",
                            value=payload["industry"],
                        ),
                        *(
                            FinancialCompanyTypeClassifierInput(
                                key="present_field",
                                value=value,
                            )
                            for value in payload["present_fields"]
                        ),
                    ),
                )
                evidence = evidence.model_copy(
                    update={
                        "artifact_identity": (
                            "provider-artifact:v1:" + acquired.artifact.artifact_sha256
                        )
                    }
                )
                evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
                    instrument_identity=evidence.instrument_identity,
                    as_of_date=evidence.as_of_date,
                    provider_id=evidence.provider_id,
                    artifact_identity=evidence.artifact_identity,
                    source_ref=evidence.source_ref,
                    observed_at=evidence.observed_at,
                    retrieved_at=evidence.retrieved_at,
                    provider_declared_type=evidence.provider_declared_type,
                    provider_declaration_qualified=(evidence.provider_declaration_qualified),
                    normalized_resolver_inputs=evidence.normalized_resolver_inputs,
                )
                response = FinancialCompanyTypeEvidenceProviderResponse.available(
                    provider_id="tushare",
                    metadata_binding=request.metadata_binding,
                    source_projection_kind="tushare_statement",
                    evidence=evidence,
                    subrequest_keys=(key.subrequest_key,),
                    physical_attempt_ids=attempt_ids,
                )
            company_responses.append(response)
            return response

        baostock_adapter = BaoStockCapabilityAdapter(
            routing_plan=plan,
            subrequest_cache=cache,
            transport=baostock_transport,
            owner_id="ticket-02b-owner",
            now=lambda: _RETRIEVED_AT,
            sleep=lambda _seconds: None,
            lease_duration=timedelta(seconds=30),
        )

        def lifecycle_source(request) -> FinancialCapabilityProviderResponse:
            acquired = baostock_adapter.acquire_lifecycle(
                instrument_identity=request.instrument_identity,
                as_of_date=request.as_of_date,
            )
            attempt_ids = tuple(event.attempt_event_id for event in acquired.attempt_events)
            if acquired.listing_metadata is None or acquired.artifact is None:
                response = FinancialCapabilityProviderResponse.unavailable(
                    provider_id="baostock",
                    capability=MainlandCapability.ISSUER_LIFECYCLE,
                    reason="no_data",
                    subrequest_keys=(acquired.subrequest_key,),
                    physical_attempt_ids=attempt_ids,
                )
            else:
                response = FinancialCapabilityProviderResponse.available(
                    provider_id="baostock",
                    capability=MainlandCapability.ISSUER_LIFECYCLE,
                    retained_artifact_identities=(acquired.artifact.artifact_identity,),
                    candidate_count=1,
                    subrequest_keys=(acquired.subrequest_key,),
                    physical_attempt_ids=attempt_ids,
                    listing_metadata=acquired.listing_metadata,
                )
            lifecycle_responses.append(response)
            return response

        router = MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            capability_sources={
                MainlandCapability.ISSUER_LIFECYCLE: {
                    "baostock": lifecycle_source,
                }
            },
            company_type_evidence_sources={"tushare": company_source},
        )
        acquirer = FinancialEligibilityMetadataAcquirer(
            routing_plan=plan,
            router=router,
            stop_signal=Event(),
        )
        request = _request(asset, plan)

        first = acquirer.acquire(request)
        second = acquirer.acquire(request)

    assert first.outcome == second.outcome == "success"
    assert first.context is not None and second.context is not None
    assert first.context.context_identity == second.context.context_identity
    assert tushare_physical_calls == 1
    assert baostock_transport.lifecycle_calls == 1
    assert len(company_responses[0].physical_attempt_ids) == 1
    assert len(lifecycle_responses[0].physical_attempt_ids) == 1
    assert company_responses[1].physical_attempt_ids == (company_responses[0].physical_attempt_ids)
    assert lifecycle_responses[1].physical_attempt_ids == (
        lifecycle_responses[0].physical_attempt_ids
    )


@pytest.mark.unit
def test_company_type_routing_result_rejects_fabricated_attempt_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    evidence = _company_type_evidence(
        "tushare",
        instrument_identity=asset.instrument_identity,
        declared_type=FinancialCompanyType.BANK,
        declaration_qualified=True,
        inputs=(),
    )
    response = FinancialCompanyTypeEvidenceProviderResponse.available(
        provider_id="tushare",
        metadata_binding=_metadata_binding(asset, plan),
        source_projection_kind="tushare_statement",
        evidence=evidence,
        subrequest_keys=("provider-subrequest:v1:" + "8" * 64,),
        physical_attempt_ids=("provider-physical-attempt=sha256:" + "9" * 64,),
    )

    with pytest.raises(ValueError, match="lineage"):
        FinancialCompanyTypeEvidenceRoutingResult(
            route=("tushare",),
            provider_calls=(response,),
            selected_provider="tushare",
            selected_evidence=evidence,
            subrequest_keys=("fabricated",),
            physical_attempt_ids=(),
        )


@pytest.mark.unit
def test_company_type_routing_result_rejects_duplicate_cross_provider_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    plan = _qualified_plan(monkeypatch, asset)
    binding = _metadata_binding(asset, plan)
    duplicate_attempt = "provider-physical-attempt=sha256:" + "f" * 64
    unavailable = FinancialCompanyTypeEvidenceProviderResponse.unavailable(
        provider_id="tushare",
        metadata_binding=binding,
        reason="provider_error",
        physical_attempt_ids=(duplicate_attempt,),
    )
    evidence = _company_type_evidence(
        "akshare_sina",
        instrument_identity=asset.instrument_identity,
        declared_type=None,
        declaration_qualified=False,
        inputs=(
            FinancialCompanyTypeClassifierInput(
                key="metadata:industry",
                value="bank",
            ),
        ),
    )
    available = FinancialCompanyTypeEvidenceProviderResponse.available(
        provider_id="akshare_sina",
        metadata_binding=binding,
        source_projection_kind="akshare_sina_statement",
        evidence=evidence,
        physical_attempt_ids=(duplicate_attempt,),
    )

    with pytest.raises(ValueError, match="duplicate"):
        FinancialCompanyTypeEvidenceRoutingResult(
            route=("tushare", "akshare_sina", "yfinance"),
            provider_calls=(unavailable, available),
            selected_provider="akshare_sina",
            selected_evidence=evidence,
            physical_attempt_ids=(duplicate_attempt,),
        )


@pytest.mark.unit
def test_company_type_route_rejects_fresh_cross_instrument_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    other_asset = _asset("600895.SS")
    plan = _qualified_plan(monkeypatch, asset)

    def cross_instrument_source(
        _request: FinancialCompanyTypeEvidenceRoutingRequest,
    ) -> FinancialCompanyTypeEvidenceProviderResponse:
        return FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=_metadata_binding(other_asset, plan),
            source_projection_kind="tushare_statement",
            evidence=_company_type_evidence(
                "tushare",
                instrument_identity=other_asset.instrument_identity,
                declared_type=FinancialCompanyType.BANK,
                declaration_qualified=True,
                inputs=(),
            ),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        company_type_evidence_sources={"tushare": cross_instrument_source},
    )

    with pytest.raises(ValueError, match="request binding"):
        router.route_company_type_evidence(
            FinancialCompanyTypeEvidenceRoutingRequest(
                metadata_binding=_metadata_binding(asset, plan),
            )
        )


@pytest.mark.unit
def test_acquired_company_type_evidence_is_intrinsically_instrument_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    other_asset = _asset("600895.SS")
    plan = _qualified_plan(monkeypatch, asset)
    evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=other_asset.instrument_identity,
        as_of_date=_AS_OF,
        provider_id="tushare",
        artifact_identity="provider-artifact:v1:" + "a" * 64,
        source_ref="acq.v1:financial.company-type:tushare",
        observed_at=_OBSERVED_AT,
        retrieved_at=_RETRIEVED_AT,
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        normalized_resolver_inputs=(),
    )

    with pytest.raises(ValueError, match="acquisition binding"):
        FinancialCompanyTypeEvidenceProviderResponse.available(
            provider_id="tushare",
            metadata_binding=_metadata_binding(asset, plan),
            source_projection_kind="tushare_statement",
            evidence=evidence,
        )


@pytest.mark.unit
def test_lifecycle_route_rejects_fresh_cross_instrument_listing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    other_asset = _asset("600895.SS")
    plan = _qualified_plan(monkeypatch, asset)
    metadata = FinancialListingMetadata(
        instrument_identity=other_asset.instrument_identity,
        as_of_date=_AS_OF,
        listing_date=date(1993, 12, 6),
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref="provider-artifact:v1:" + "a" * 64,
            observed_at=_OBSERVED_AT,
        ),
    )

    def cross_instrument_source(
        _request: FinancialCapabilityRoutingRequest,
    ) -> FinancialCapabilityProviderResponse:
        return FinancialCapabilityProviderResponse.available(
            provider_id="baostock",
            capability=MainlandCapability.ISSUER_LIFECYCLE,
            retained_artifact_identities=(metadata.listing_provenance.source_ref,),
            candidate_count=1,
            subrequest_keys=(),
            physical_attempt_ids=(),
            listing_metadata=metadata,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        capability_sources={
            MainlandCapability.ISSUER_LIFECYCLE: {
                "baostock": cross_instrument_source,
            }
        },
    )

    with pytest.raises(ValueError, match="request binding"):
        router.route_capability(
            FinancialCapabilityRoutingRequest(
                instrument_identity=asset.instrument_identity,
                capability=MainlandCapability.ISSUER_LIFECYCLE,
                range_start=_AS_OF,
                as_of_date=_AS_OF,
            )
        )

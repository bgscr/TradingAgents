from __future__ import annotations

import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from threading import Event

import pytest
from pydantic import ValidationError

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingCheckpointAction,
    MainlandCapabilityRoutingCheckpointError,
    MainlandCapabilityRoutingCheckpointFailureReason,
    MainlandCapabilityRoutingPlan,
    preflight_mainland_capability_routing,
    prepare_mainland_temporal_capability_routing_plan_v1,
    start_new_mainland_capability_routing_run,
    validate_mainland_capability_routing_checkpoint,
)
from tradingagents.dataflows.financial_capability_routing import (
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialCompanyTypeClassifierInput,
    FinancialConsolidationScope,
    FinancialIndicatorEvidenceRequirements,
    FinancialListingProvenance,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialIndicatorRequestArguments,
    FinancialToolDispatcher,
    FinancialToolMessageManifestEnvelope,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialCompanyTypeEvidenceProjection,
    FinancialEligibilityAcquisitionContext,
    FinancialEligibilityAcquisitionFailureReason,
    FinancialEligibilityAcquisitionResult,
    FinancialEligibilityMetadataBinding,
    FinancialEligibilityRequestBinding,
    FinancialListingMetadata,
    FinancialListingMetadataProjection,
)
from tradingagents.dataflows.financial_eligibility_metadata import (
    FinancialEligibilityMetadataAcquirer,
    FinancialEligibilityMetadataAcquisitionRequest,
)
from tradingagents.dataflows.financial_request_factory import (
    build_financial_indicator_routing_request,
    build_financial_statement_routing_request,
)
from tradingagents.dataflows.financial_temporal_eligibility import (
    EvidenceEffectiveAsOfDerivation,
    EvidenceEffectiveAsOfSemantics,
    ExactRevisionAvailabilityProof,
    ExactRevisionAvailabilityScope,
    FinancialMetadataTemporalEligibilityDecision,
    FinancialMetadataTemporalEvidence,
    MarketSessionEligibilityBinding,
    ProviderPublicationDateDerivation,
    ProviderPublicationDateSemantics,
    PublicationDateKind,
    TemporalEligibilityMode,
    TemporalEvidenceArtifactBinding,
    TemporalEvidenceClass,
    build_market_session_eligibility_binding,
    evaluate_financial_metadata_temporal_eligibility,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
    ProviderSubrequestKey,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.market_history import (
    CoordinatedRequestResult,
    DataUsageMode,
    MarketSession,
    MarketSessionStatus,
    PhysicalAttemptOutcome,
    ProvenanceClass,
    ProviderPhysicalAttemptEvent,
    RequestPriority,
    StoredSessionCalendar,
    upstream_service_identity_for_provider,
)
from tradingagents.qualified_routing_eligibility_lifecycle import (
    FinancialEligibilityAcquisitionContextOwner,
    QualifiedFinancialRoutingComposition,
)

_REQUESTED_SESSION = date(2026, 8, 10)
_EVALUATED_AT = datetime(2026, 8, 11, 0, 59, tzinfo=timezone.utc)
_RETRIEVED_AT = datetime(2026, 8, 10, 16, 48, 7, tzinfo=timezone.utc)
_CHECKPOINT_THREAD_ID = "05c0a1b2c3d4e5f6"


def _run_id() -> str:
    return "run:" + sha256(
        f"checkpoint:{_CHECKPOINT_THREAD_ID}".encode()
    ).hexdigest()


def _qualified_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, MainlandCapabilityRoutingPlan]:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": ["statements", "financial_indicators"],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    asset = resolve_run_asset_configuration("601328.SS", config=config)
    preflight = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert preflight.plan is not None
    return asset, preflight.plan


def _temporal_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, MainlandCapabilityRoutingPlan]:
    asset, plan = _qualified_plan(monkeypatch)
    return asset, prepare_mainland_temporal_capability_routing_plan_v1(plan)


def _stored_calendar() -> StoredSessionCalendar:
    return StoredSessionCalendar(
        calendar_revision_id="market-session-calendar:v1:" + "7" * 64,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provider_dataset_id="provider-dataset:05c-calendar-v1",
        observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        sessions=(
            MarketSession(_REQUESTED_SESSION, MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 11), MarketSessionStatus.OPEN),
        ),
    )


def _calendar_binding() -> MarketSessionEligibilityBinding:
    return build_market_session_eligibility_binding(
        _stored_calendar(),
        requested_session=_REQUESTED_SESSION,
        eligibility_evaluated_at=_EVALUATED_AT,
    )


def _temporal_evidence(
    *,
    evidence_class: TemporalEvidenceClass,
    artifact_identity: str,
    row_identity: str,
    effective_as_of: date,
    effective_semantics: EvidenceEffectiveAsOfSemantics,
    effective_source_field: str,
    publication_semantics: ProviderPublicationDateSemantics,
    ann_source_field: str,
    f_ann_source_field: str,
) -> FinancialMetadataTemporalEvidence:
    artifact = TemporalEvidenceArtifactBinding(
        artifact_identity=artifact_identity,
        revision_identity=artifact_identity,
        row_identity=row_identity,
    )
    return FinancialMetadataTemporalEvidence(
        evidence_class=evidence_class,
        artifact=artifact,
        evidence_effective_as_of=effective_as_of,
        evidence_effective_as_of_derivation=EvidenceEffectiveAsOfDerivation(
            semantics=effective_semantics,
            source_field=effective_source_field,
            artifact=artifact,
        ),
        ann_date=_REQUESTED_SESSION,
        ann_date_derivation=ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.ANN_DATE,
            semantics=publication_semantics,
            source_field=ann_source_field,
            artifact=artifact,
        ),
        f_ann_date=_REQUESTED_SESSION,
        f_ann_date_derivation=ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.F_ANN_DATE,
            semantics=publication_semantics,
            source_field=f_ann_source_field,
            artifact=artifact,
        ),
        observed_at=_RETRIEVED_AT,
        retrieved_at=_RETRIEVED_AT,
    )


def _temporal_context(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, MainlandCapabilityRoutingPlan, FinancialEligibilityAcquisitionContext]:
    asset, plan = _temporal_plan(monkeypatch)
    calendar = _calendar_binding()
    assert plan.financial_eligibility_binding is not None
    metadata_binding = FinancialEligibilityMetadataBinding(
        contract_version="financial-eligibility-metadata-binding-v2",
        instrument_identity=asset.instrument_identity,
        requested_session=_REQUESTED_SESSION,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        temporal_mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        eligibility_evaluated_at=_EVALUATED_AT,
        calendar_binding=calendar,
    )
    company_artifact = "financial-provider-artifact:v1:" + "4" * 64
    company_proof = _temporal_evidence(
        evidence_class=TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
        artifact_identity=company_artifact,
        row_identity="financial-period-candidate:v1:" + "5" * 64,
        effective_as_of=date(2026, 6, 30),
        effective_semantics=EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END,
        effective_source_field="end_date",
        publication_semantics=(
            ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
        ),
        ann_source_field="ann_date",
        f_ann_source_field="f_ann_date",
    )
    company_evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=asset.instrument_identity,
        requested_session=_REQUESTED_SESSION,
        temporal_evidence=company_proof,
        provider_id="tushare",
        artifact_identity=company_artifact,
        source_ref=company_artifact,
        observed_at=_RETRIEVED_AT,
        retrieved_at=_RETRIEVED_AT,
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        normalized_resolver_inputs=(
            FinancialCompanyTypeClassifierInput(key="metadata:industry", value="bank"),
            FinancialCompanyTypeClassifierInput(
                key="present_field",
                value="customer_deposits",
            ),
        ),
    )
    company_decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=company_proof,
        calendar_binding=calendar,
    )
    company_projection = FinancialCompanyTypeEvidenceProjection(
        contract_version="financial-company-type-evidence-projection-v2",
        metadata_binding=metadata_binding,
        evidence=company_evidence,
        temporal_decision=company_decision,
    )
    listing_artifact = "provider-artifact:v1:" + "6" * 64
    listing_proof = _temporal_evidence(
        evidence_class=TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING,
        artifact_identity=listing_artifact,
        row_identity="provider-row-artifact:v1:" + "7" * 64,
        effective_as_of=date(2010, 1, 4),
        effective_semantics=EvidenceEffectiveAsOfSemantics.BAOSTOCK_LIFECYCLE_LISTING_DATE,
        effective_source_field="ipoDate",
        publication_semantics=(
            ProviderPublicationDateSemantics.BAOSTOCK_LIFECYCLE_PUBLICATION_FIELDS
        ),
        ann_source_field="announcement_date",
        f_ann_source_field="first_publication_date",
    )
    listing_decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=listing_proof,
        calendar_binding=calendar,
    )
    listing_metadata = FinancialListingMetadata(
        contract_version="financial-listing-metadata-v2",
        instrument_identity=asset.instrument_identity,
        requested_session=_REQUESTED_SESSION,
        listing_date=date(2010, 1, 4),
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref=listing_artifact,
            observed_at=_RETRIEVED_AT,
        ),
        temporal_evidence=listing_proof,
    )
    listing_projection = FinancialListingMetadataProjection(
        contract_version="financial-listing-metadata-projection-v2",
        metadata_binding=metadata_binding,
        metadata=listing_metadata,
        temporal_decision=listing_decision,
    )
    request = FinancialEligibilityMetadataAcquisitionRequest(
        contract_version="financial-eligibility-metadata-acquisition-request-v2",
        canonical_run_id=_run_id(),
        run_id=_run_id(),
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        requested_session=_REQUESTED_SESSION,
        authoritative_availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        temporal_mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        eligibility_evaluated_at=_EVALUATED_AT,
        calendar_binding=calendar,
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        stop_signal=Event(),
    ).acquire(
        request,
        retained_company_type_evidence=(company_projection,),
        retained_listing_metadata=(listing_projection,),
    )
    assert result.context is not None, result.failure
    return asset, plan, result.context


def _strict_temporal_context(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, MainlandCapabilityRoutingPlan, FinancialEligibilityAcquisitionContext]:
    asset, plan = _temporal_plan(monkeypatch)
    calendar = _calendar_binding()
    strict_cutoff = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
    post_cutoff = datetime(2026, 8, 10, 16, 30, tzinfo=timezone.utc)
    assert plan.financial_eligibility_binding is not None
    metadata_binding = FinancialEligibilityMetadataBinding(
        contract_version="financial-eligibility-metadata-binding-v2",
        instrument_identity=asset.instrument_identity,
        requested_session=_REQUESTED_SESSION,
        availability_scope=FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        temporal_mode=TemporalEligibilityMode.STRICT_HISTORICAL_PIT,
        eligibility_evaluated_at=_EVALUATED_AT,
        calendar_binding=calendar,
        strict_cutoff=strict_cutoff,
    )

    def strict_proof(
        *,
        evidence_class: TemporalEvidenceClass,
        artifact_identity: str,
        row_identity: str,
        effective_as_of: date,
        effective_semantics: EvidenceEffectiveAsOfSemantics,
        effective_source_field: str,
        publication_semantics: ProviderPublicationDateSemantics,
        ann_source_field: str,
        f_ann_source_field: str,
    ) -> FinancialMetadataTemporalEvidence:
        evidence = _temporal_evidence(
            evidence_class=evidence_class,
            artifact_identity=artifact_identity,
            row_identity=row_identity,
            effective_as_of=effective_as_of,
            effective_semantics=effective_semantics,
            effective_source_field=effective_source_field,
            publication_semantics=publication_semantics,
            ann_source_field=ann_source_field,
            f_ann_source_field=f_ann_source_field,
        )
        return evidence.model_copy(
            update={
                "ann_date": date(2026, 8, 8),
                "f_ann_date": date(2026, 8, 9),
                "observed_at": post_cutoff,
                "retrieved_at": post_cutoff,
                "exact_revision_availability_proof": (
                    ExactRevisionAvailabilityProof(
                        exact_revision_authoritative_available_at=(
                            datetime(2026, 8, 10, 14, 45, tzinfo=timezone.utc)
                        ),
                        authoritative=True,
                        scope=(
                            ExactRevisionAvailabilityScope.EXACT_IMMUTABLE_REVISION
                        ),
                        proof_identity="exact-revision-proof:v1:" + "d" * 64,
                        artifact=evidence.artifact,
                    )
                ),
            }
        )

    company_artifact = "financial-provider-artifact:v1:" + "a" * 64
    company_proof = strict_proof(
        evidence_class=TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
        artifact_identity=company_artifact,
        row_identity="financial-period-candidate:v1:" + "b" * 64,
        effective_as_of=date(2026, 6, 30),
        effective_semantics=EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END,
        effective_source_field="end_date",
        publication_semantics=(
            ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
        ),
        ann_source_field="ann_date",
        f_ann_source_field="f_ann_date",
    )
    company_decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.STRICT_HISTORICAL_PIT,
        evidence=company_proof,
        calendar_binding=calendar,
        strict_cutoff=strict_cutoff,
    )
    company_projection = FinancialCompanyTypeEvidenceProjection(
        contract_version="financial-company-type-evidence-projection-v2",
        metadata_binding=metadata_binding,
        evidence=FinancialCompanyTypeAcquisitionEvidenceBinding.create(
            instrument_identity=asset.instrument_identity,
            requested_session=_REQUESTED_SESSION,
            temporal_evidence=company_proof,
            provider_id="tushare",
            artifact_identity=company_artifact,
            source_ref=company_artifact,
            observed_at=post_cutoff,
            retrieved_at=post_cutoff,
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
        ),
        temporal_decision=company_decision,
    )
    listing_artifact = "provider-artifact:v1:" + "c" * 64
    listing_proof = strict_proof(
        evidence_class=TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING,
        artifact_identity=listing_artifact,
        row_identity="provider-row-artifact:v1:" + "e" * 64,
        effective_as_of=date(2010, 1, 4),
        effective_semantics=(
            EvidenceEffectiveAsOfSemantics.BAOSTOCK_LIFECYCLE_LISTING_DATE
        ),
        effective_source_field="ipoDate",
        publication_semantics=(
            ProviderPublicationDateSemantics.BAOSTOCK_LIFECYCLE_PUBLICATION_FIELDS
        ),
        ann_source_field="announcement_date",
        f_ann_source_field="first_publication_date",
    )
    listing_decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.STRICT_HISTORICAL_PIT,
        evidence=listing_proof,
        calendar_binding=calendar,
        strict_cutoff=strict_cutoff,
    )
    listing_metadata = FinancialListingMetadata(
        contract_version="financial-listing-metadata-v2",
        instrument_identity=asset.instrument_identity,
        requested_session=_REQUESTED_SESSION,
        listing_date=date(2010, 1, 4),
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref=listing_artifact,
            observed_at=post_cutoff,
        ),
        temporal_evidence=listing_proof,
    )
    listing_projection = FinancialListingMetadataProjection(
        contract_version="financial-listing-metadata-projection-v2",
        metadata_binding=metadata_binding,
        metadata=listing_metadata,
        temporal_decision=listing_decision,
    )
    request = FinancialEligibilityMetadataAcquisitionRequest(
        contract_version="financial-eligibility-metadata-acquisition-request-v2",
        canonical_run_id=_run_id(),
        run_id=_run_id(),
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        requested_session=_REQUESTED_SESSION,
        authoritative_availability_scope=(
            FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
        ),
        availability_scope=FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        temporal_mode=TemporalEligibilityMode.STRICT_HISTORICAL_PIT,
        eligibility_evaluated_at=_EVALUATED_AT,
        calendar_binding=calendar,
        strict_cutoff=strict_cutoff,
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        stop_signal=Event(),
    ).acquire(
        request,
        retained_company_type_evidence=(company_projection,),
        retained_listing_metadata=(listing_projection,),
    )
    assert result.context is not None, result.failure
    return asset, plan, result.context


@pytest.mark.unit
def test_context_v2_cache_checkpoint_1_2_round_trips_temporal_decisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _asset, plan, context = _temporal_context(monkeypatch)
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
        run_scope_id=context.run_id,
    )

    cache.bind_financial_eligibility_context(
        context_contract_version=context.contract_version,
        context_identity=context.context_identity,
        context=context,
        capability_routing_plan=plan,
    )
    checkpoint = cache.checkpoint()

    temporal = context.temporal_eligibility_binding
    assert temporal is not None
    assert checkpoint["contract_version"] == "1.2"
    assert checkpoint["capability_routing_plan_version"] == "1.3"
    assert checkpoint["capability_routing_plan_signature"] == plan.plan_signature
    assert checkpoint["financial_eligibility_context_contract_version"] == (
        "financial-eligibility-acquisition-context-v2"
    )
    assert checkpoint["financial_eligibility_context_identity"] == context.context_identity
    assert checkpoint["financial_eligibility_metadata_binding"]["contract_version"] == (
        "financial-eligibility-metadata-binding-v2"
    )
    assert checkpoint["calendar_eligibility_binding"] == (
        temporal.calendar_binding.model_dump(mode="json")
    )
    assert checkpoint["temporal_decisions"] == [
        temporal.company_type_temporal_decision.model_dump(mode="json"),
        temporal.listing_temporal_decision.model_dump(mode="json"),
    ]
    assert checkpoint["temporal_artifact_identities"] == sorted(
        decision.immutable_artifact_identity
        for decision in (
            temporal.company_type_temporal_decision,
            temporal.listing_temporal_decision,
        )
    )

    restored = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "restored"),
        run_scope_id=context.run_id,
    )
    restored.bind_financial_eligibility_context(
        context_contract_version=context.contract_version,
        context_identity=context.context_identity,
        context=context,
        capability_routing_plan=plan,
    )
    restored.restore_checkpoint(checkpoint)

    assert restored.checkpoint() == checkpoint


@pytest.mark.unit
def test_temporal_cache_1_2_restores_artifact_and_attempt_without_fresh_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset, plan, context = _temporal_context(monkeypatch)
    upstream_service_id, _ = upstream_service_identity_for_provider(
        "tushare",
        account_scope="personal-research-primary",
    )
    key = ProviderSubrequestKey.create(
        provider_id="tushare",
        upstream_service_id=upstream_service_id,
        account_scope="personal-research-primary",
        capacity_scope="balancesheet",
        instrument_identity=asset.instrument_identity,
        requested_range_start="2026-06-30",
        requested_range_end="2026-06-30",
        requested_fields=("ann_date", "end_date", "total_assets"),
        as_of_date=_REQUESTED_SESSION,
        qualification_profile="cn-a-2000-20260729-v1",
        normalizer_version="tushare-statement-normalizer-v2",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    )
    attempted_at = datetime(2026, 8, 10, 16, 48, 8, tzinfo=timezone.utc)
    sequence_id = "provider-request-sequence=sha256:" + "a" * 64

    class DeterministicCoordinator:
        calls = 0

        def execute_retry_sequence(self, **kwargs):
            self.calls += 1
            value = kwargs["physical_attempt"](1)
            digest = sha256()
            for component in ("provider-physical-attempt", sequence_id, "1"):
                encoded = component.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            event = ProviderPhysicalAttemptEvent(
                attempt_event_id=(
                    "provider-physical-attempt=sha256:" + digest.hexdigest()
                ),
                sequence_id=sequence_id,
                request_key=key.subrequest_key,
                upstream_service_id=key.upstream_service_id,
                upstream_service_name="Tushare Pro account",
                capacity_scope=key.capacity_scope,
                attempt_index=1,
                attempted_at=attempted_at,
                pacing_event="permit_acquired",
                pacing_wait_seconds=0.0,
                outcome=PhysicalAttemptOutcome.AVAILABLE,
                retryable=False,
                status_code=None,
                error_code=None,
                retry_after_seconds=None,
                cooldown_changed=False,
                cooldown_until=None,
                final_physical_attempt_count=1,
            )
            return CoordinatedRequestResult(
                value=value,
                sequence_id=sequence_id,
                physical_attempt_count=1,
                attempt_events=(event,),
            )

    artifact_store = ProviderSubrequestArtifactStore(tmp_path / "artifacts")
    coordinator = DeterministicCoordinator()
    cache = ProviderSubrequestCache(
        coordinator=coordinator,
        artifact_store=artifact_store,
        run_scope_id=context.run_id,
    )
    cache.bind_financial_eligibility_context(
        context_contract_version=context.contract_version,
        context_identity=context.context_identity,
        context=context,
        capability_routing_plan=plan,
    )
    original = cache.execute(
        key,
        owner_id="ticket-05c-original",
        priority=RequestPriority.INTERACTIVE_MAINLAND,
        now=lambda: attempted_at,
        sleep=lambda _seconds: None,
        lease_duration=timedelta(seconds=30),
        operation="financial-statement:balancesheet",
        media_type="application/json",
        physical_request=lambda: b'{"safe_fixture":"temporal-cache-1.2"}',
    )
    checkpoint = cache.checkpoint()

    class ForbiddenCoordinator:
        def execute_retry_sequence(self, **_kwargs):
            pytest.fail("restored temporal cache requested a coordinator permit")

    def forbidden(*_args, **_kwargs):
        pytest.fail("restored temporal cache performed fresh work")

    restored_cache = ProviderSubrequestCache(
        coordinator=ForbiddenCoordinator(),
        artifact_store=artifact_store,
        run_scope_id=context.run_id,
    )
    restored_cache.bind_financial_eligibility_context(
        context_contract_version=context.contract_version,
        context_identity=context.context_identity,
        context=context,
        capability_routing_plan=plan,
    )
    restored_cache.restore_checkpoint(checkpoint)
    restored = restored_cache.execute(
        key,
        owner_id="ticket-05c-replay",
        priority=RequestPriority.INTERACTIVE_MAINLAND,
        now=forbidden,
        sleep=forbidden,
        lease_duration=timedelta(seconds=30),
        operation="financial-statement:balancesheet",
        media_type="application/json",
        physical_request=forbidden,
    )

    temporal = context.temporal_eligibility_binding
    assert temporal is not None
    assert coordinator.calls == 1
    assert checkpoint["contract_version"] == "1.2"
    assert len(checkpoint["entries"]) == 1
    assert len(checkpoint["entries"][0]["attempt_events"]) == 1
    assert restored.value == original.value
    assert restored.artifact == original.artifact
    assert restored.attempt_events == original.attempt_events
    assert restored_cache.checkpoint() == checkpoint
    restored_decisions = tuple(
        FinancialMetadataTemporalEligibilityDecision.model_validate(item)
        for item in checkpoint["temporal_decisions"]
    )
    assert restored_decisions == (
        temporal.company_type_temporal_decision,
        temporal.listing_temporal_decision,
    )


@pytest.mark.unit
def test_temporal_statement_dispatch_writes_forward_checkpoint_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset, plan, context = _temporal_context(monkeypatch)
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
        run_scope_id=context.run_id,
    )
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
    )
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_REQUESTED_SESSION,
        tool_call_id="ticket-05c-statement",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
    )
    selection_request = build_financial_statement_routing_request(request, context)
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        provider_subrequest_cache=cache,
        qualified_statement_router=router,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )

    dispatch = dispatcher.dispatch_statement_selection(request, selection_request)
    temporal = context.temporal_eligibility_binding
    assert temporal is not None
    assert dispatch.contract_version == "financial-statement-selection-dispatch-v2"
    assert dispatch.routing_result.contract_version == (
        "financial-statement-routing-result-v2"
    )
    assert dispatch.routing_result.manifest.contract_version == "financial-manifest-v4"
    assert dispatch.routing_result.manifest.manifest_identity.startswith(
        "financial-manifest:v4:"
    )
    assert dispatch.routing_result.temporal_eligibility_binding.model_dump(
        mode="json"
    ) == temporal.model_dump(mode="json")
    assert dispatch.routing_result.manifest.temporal_eligibility_binding.model_dump(
        mode="json"
    ) == temporal.model_dump(mode="json")
    assert dispatch.routing_result.manifest.financial_eligibility_binding == (
        plan.financial_eligibility_binding
    )
    assert dispatch.selection_request.financial_eligibility_context_binding == (
        FinancialEligibilityRequestBinding.from_context(context)
    )
    ledger = dispatcher.checkpoint_ledger()
    assert ledger["contract_version"] == "1.6"
    assert ledger["financial_manifest_version"] == "financial-manifest-v4"
    assert ledger["capability_routing_plan_version"] == "1.3"
    assert ledger["provider_subrequest_cache"]["contract_version"] == "1.2"
    assert ledger["temporal_eligibility_binding"] == temporal.model_dump(mode="json")
    assert ledger["qualified_statement_selections"][0]["contract_version"] == (
        "financial-statement-selection-checkpoint-v2"
    )


@pytest.mark.unit
def test_temporal_indicator_dispatch_writes_v4_and_v3_selection_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset, plan, context = _temporal_context(monkeypatch)
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
        run_scope_id=context.run_id,
    )
    families = (FinancialRatioFamily.PROFIT,)
    requirements = FinancialIndicatorEvidenceRequirements.create(
        requested_families=families,
        canonical_evidence_contract_id="canonical-evidence-contract.v1",
        canonical_evidence_contract_required_families=families,
        capability_profile_id="equity.v1",
        strategy_rules_id="mainland-equity-strategy-rules.v1",
    )
    request = FinancialToolRequest(
        tool_name="get_fundamentals",
        statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=FinancialReportingFrequency.NOT_APPLICABLE,
        as_of_date=_REQUESTED_SESSION,
        tool_call_id="ticket-05c-indicator",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
        indicator_request_arguments=FinancialIndicatorRequestArguments(
            requested_families=families,
            family_evidence_requirements=requirements,
        ),
    )
    selection_request = build_financial_indicator_routing_request(request, context)
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        indicator_sources={},
    )
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=router,
        provider_subrequest_cache=cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )

    dispatch = dispatcher.dispatch_indicator_selection(request, selection_request)
    ledger = dispatcher.checkpoint_ledger()

    assert dispatch.contract_version == "financial-indicator-selection-dispatch-v3"
    assert dispatch.routing_result.contract_version == (
        "financial-indicator-routing-result-v4"
    )
    assert {item.contract_version for item in dispatch.routing_result.family_manifests} == {
        "financial-manifest-v4"
    }
    assert ledger["contract_version"] == "1.6"
    assert ledger["qualified_indicator_selections"][0]["contract_version"] == (
        "financial-indicator-selection-checkpoint-v3"
    )
    envelope = dispatch.to_tool_message().artifact
    assert envelope["contract_version"] == "financial-tool-message-envelope-v4"
    assert {item["contract_version"] for item in envelope["manifests"]} == {
        "financial-tool-manifest-v4"
    }
    assert envelope["indicator_field_semantics_version"] == (
        "financial-indicator-field-semantics-v1"
    )
    assert envelope["indicator_metadata_compatibility_version"] == (
        "financial-indicator-metadata-compatibility-v1"
    )
    temporal = context.temporal_eligibility_binding
    assert temporal is not None
    assert envelope["temporal_eligibility_binding"] == temporal.model_dump(
        mode="json"
    )
    assert all(
        item["contract_version"] == "financial-completeness-row-v5"
        for item in envelope["completeness_rows"]
    )
    audit = project_financial_dispatch_ledger(
        ledger,
        run_asset_configuration=asset,
        config=config,
        capability_routing_plan=plan,
        run_scope_id=context.run_id,
    )
    assert audit is not None
    audit_payload = audit.model_dump(mode="json")
    assert audit_payload["contract_version"] == "4.0"
    assert audit_payload["indicator_field_semantics_version"] == (
        "financial-indicator-field-semantics-v1"
    )
    assert audit_payload["indicator_metadata_compatibility_version"] == (
        "financial-indicator-metadata-compatibility-v1"
    )
    assert audit_payload["temporal_eligibility_binding"] == temporal.model_dump(
        mode="json"
    )


@pytest.mark.unit
def test_temporal_statement_ledger_restores_and_replays_without_fresh_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset, plan, context = _temporal_context(monkeypatch)
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "fresh-artifacts"),
        run_scope_id=context.run_id,
    )
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_REQUESTED_SESSION,
        tool_call_id="ticket-05c-original",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
    )
    selection_request = build_financial_statement_routing_request(request, context)
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    fresh = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        provider_subrequest_cache=cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    original = fresh.dispatch_statement_selection(request, selection_request)
    ledger = fresh.checkpoint_ledger()
    cache_checkpoint = cache.checkpoint()

    restored_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "restored-artifacts"),
        run_scope_id=context.run_id,
    )
    restored_cache.bind_financial_eligibility_context(
        context_contract_version=context.contract_version,
        context_identity=context.context_identity,
        context=context,
        capability_routing_plan=plan,
    )
    restored_cache.restore_checkpoint(cache_checkpoint)

    def forbidden(*_args, **_kwargs):
        pytest.fail("exact temporal replay performed fresh work")

    monkeypatch.setattr(
        MainlandFinancialCapabilityRouter,
        "route_statement",
        forbidden,
    )
    restored = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        clock=forbidden,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        checkpoint_ledger=ledger,
        provider_subrequest_cache=restored_cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    replay_request = request.model_copy(
        update={"tool_call_id": "ticket-05c-replay"}
    )
    replayed = restored.dispatch_statement_selection(
        replay_request,
        selection_request,
    )

    assert replayed.disposition == "duplicate_suppressed"
    assert replayed.routing_result == original.routing_result
    assert replayed.selection_request == original.selection_request
    assert restored_cache.checkpoint() == cache_checkpoint


@pytest.mark.unit
def test_temporal_operational_projections_write_exact_v4_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset, plan, context = _temporal_context(monkeypatch)
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "artifacts"),
        run_scope_id=context.run_id,
    )
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_REQUESTED_SESSION,
        tool_call_id="ticket-05c-operational",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
    )
    selection_request = build_financial_statement_routing_request(request, context)
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    dispatcher = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        provider_subrequest_cache=cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    dispatch = dispatcher.dispatch_statement_selection(request, selection_request)
    tool_envelope = dispatch.to_tool_message().artifact
    audit = project_financial_dispatch_ledger(
        dispatcher.checkpoint_ledger(),
        run_asset_configuration=asset,
        config=config,
        capability_routing_plan=plan,
        run_scope_id=context.run_id,
    )

    temporal = context.temporal_eligibility_binding
    assert temporal is not None
    assert tool_envelope["contract_version"] == "financial-tool-message-envelope-v4"
    assert tool_envelope["financial_manifest_version"] == "financial-manifest-v4"
    assert {item["contract_version"] for item in tool_envelope["manifests"]} == {
        "financial-tool-manifest-v4"
    }
    assert tool_envelope["temporal_eligibility_binding"] == temporal.model_dump(
        mode="json"
    )
    assert all(
        item["contract_version"] == "financial-completeness-row-v5"
        for item in tool_envelope["completeness_rows"]
    )
    assert audit is not None
    audit_payload = audit.model_dump(mode="json")
    assert audit_payload["contract_version"] == "4.0"
    assert audit_payload["temporal_eligibility_binding"] == temporal.model_dump(
        mode="json"
    )
    assert {item["contract_version"] for item in audit_payload["manifest_requests"]} == {
        "financial-manifest-request-audit-v4"
    }
    assert {
        manifest["contract_version"]
        for item in audit_payload["manifest_requests"]
        for manifest in item["manifests"]
    } == {"financial-manifest-audit-v4"}
    assert {
        manifest["aggregate_completeness"]["contract_version"]
        for item in audit_payload["manifest_requests"]
        for manifest in item["manifests"]
    } == {"financial-aggregate-completeness-v4"}
    assert all(
        item["contract_version"] == "financial-completeness-row-v5"
        for item in audit_payload["completeness_rows"]
    )

    safe_surfaces = json.dumps(
        {
            "ledger": dispatcher.checkpoint_ledger(),
            "audit": audit_payload,
            "tool": tool_envelope,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    assert not {
        "token",
        "token_digest",
        "credential",
        "raw_text",
        "provider_payload",
        "raw_provider_value",
        "local_path",
        "query_url",
    }.intersection(json.loads(safe_surfaces)["ledger"])
    for forbidden_name in (
        '"token"',
        '"token_digest"',
        '"credential"',
        '"raw_text"',
        '"provider_payload"',
        '"raw_provider_value"',
        '"local_path"',
        '"query_url"',
    ):
        assert forbidden_name not in safe_surfaces

    canary = "https://provider.invalid/private?token=05c-secret"
    unsafe_tool = copy.deepcopy(tool_envelope)
    unsafe_tool["tool_call_id"] = canary

    with pytest.raises(ValueError) as failure:
        FinancialToolMessageManifestEnvelope.model_validate(unsafe_tool)
    assert canary not in str(failure.value)
    assert canary not in repr(failure.value)


@pytest.mark.unit
def test_rejected_temporal_decision_and_typed_reasons_round_trip_closed() -> None:
    calendar = _calendar_binding()
    evidence = _temporal_evidence(
        evidence_class=TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
        artifact_identity="financial-provider-artifact:v1:" + "8" * 64,
        row_identity="financial-period-candidate:v1:" + "9" * 64,
        effective_as_of=date(2026, 6, 30),
        effective_semantics=(
            EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END
        ),
        effective_source_field="end_date",
        publication_semantics=(
            ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
        ),
        ann_source_field="ann_date",
        f_ann_source_field="f_ann_date",
    ).model_copy(
        update={
            "observed_at": datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc),
            "retrieved_at": datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc),
        }
    )
    decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=evidence,
        calendar_binding=calendar,
    )
    assert decision.result == "rejected"
    result = FinancialEligibilityAcquisitionResult.failed(
        FinancialEligibilityAcquisitionFailureReason.TEMPORAL_ELIGIBILITY_REJECTED,
        rejected_temporal_decision=decision,
    )
    payload = result.model_dump(mode="json")

    restored = FinancialEligibilityAcquisitionResult.model_validate(payload)
    assert restored == result
    assert restored.failure is not None
    assert restored.failure.rejected_temporal_decision == decision
    assert restored.failure.temporal_rejection_reasons == decision.reasons

    unknown_reason = copy.deepcopy(payload)
    unknown_reason["failure"]["rejected_temporal_decision"]["reasons"] = [
        "future_temporal_reason"
    ]
    with pytest.raises(ValidationError):
        FinancialEligibilityAcquisitionResult.model_validate(unknown_reason)

    forged_identity = copy.deepcopy(payload)
    forged_identity["failure"]["rejected_temporal_decision"][
        "decision_identity"
    ] = "financial-metadata-temporal-eligibility-decision:v1:" + "0" * 64
    with pytest.raises(ValidationError):
        FinancialEligibilityAcquisitionResult.model_validate(forged_identity)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        (
            lambda payload: payload.update(
                {"capability_routing_plan_version": "1.2"}
            ),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload.pop("temporal_eligibility_binding"),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload["temporal_eligibility_binding"].update(
                {"temporal_policy_version": "unknown-temporal-policy-v9"}
            ),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload["temporal_eligibility_binding"].update(
                {"requested_session": "2026-08-09"}
            ),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload["temporal_eligibility_binding"].update(
                {"eligibility_evaluated_at": "2026-08-11T01:00:00Z"}
            ),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload["temporal_eligibility_binding"].update(
                {"latest_completed_session": "2026-08-09"}
            ),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload["temporal_eligibility_binding"][
                "company_type_temporal_decision"
            ].update(
                {
                    "decision_identity": (
                        "financial-metadata-temporal-eligibility-decision:v1:"
                        + "0" * 64
                    )
                }
            ),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload["provider_subrequest_cache"][
                "temporal_decisions"
            ][0].update(
                {
                    "decision_identity": (
                        "financial-metadata-temporal-eligibility-decision:v1:"
                        + "0" * 64
                    )
                }
            ),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload["qualified_statement_selections"][0][
                "terminal"
            ]["routing_result"].update(
                {"contract_version": "financial-statement-routing-result-v1"}
            ),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
        (
            lambda payload: payload.update({"run_scope_id": "run:" + "f" * 64}),
            FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        ),
    ),
    ids=(
        "plan-1.2",
        "missing-temporal-binding",
        "unknown-policy",
        "wrong-session",
        "wrong-evaluation",
        "wrong-frontier",
        "forged-decision",
        "cache-decision-mismatch",
        "routing-decision-mismatch",
        "cross-run",
    ),
)
def test_temporal_ledger_corruption_fails_typed_without_restore_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    mutation,
    expected_reason: FinancialDispatchCheckpointFailureReason,
) -> None:
    asset, plan, context = _temporal_context(monkeypatch)
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "fresh-artifacts"),
        run_scope_id=context.run_id,
    )
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_REQUESTED_SESSION,
        tool_call_id="ticket-05c-corruption",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
    )
    selection_request = build_financial_statement_routing_request(request, context)
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    fresh = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        provider_subrequest_cache=cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    fresh.dispatch_statement_selection(request, selection_request)
    ledger = fresh.checkpoint_ledger()
    cache_checkpoint = cache.checkpoint()
    mutation(ledger)

    restored_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "restored-artifacts"),
        run_scope_id=context.run_id,
    )
    restored_cache.bind_financial_eligibility_context(
        context_contract_version=context.contract_version,
        context_identity=context.context_identity,
        context=context,
        capability_routing_plan=plan,
    )
    restored_cache.restore_checkpoint(cache_checkpoint)

    def forbidden(*_args, **_kwargs):
        pytest.fail("corrupt temporal restore performed fresh work")

    monkeypatch.setattr(
        MainlandFinancialCapabilityRouter,
        "route_statement",
        forbidden,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.financial_temporal_eligibility."
        "evaluate_financial_metadata_temporal_eligibility",
        forbidden,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.financial_temporal_eligibility."
        "latest_completed_session",
        forbidden,
    )
    with pytest.raises(FinancialDispatchCheckpointError) as captured:
        FinancialToolDispatcher.from_configured_vendors(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            config=config,
            vendor_methods=vendor_methods,
            clock=forbidden,
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
            ),
            checkpoint_ledger=ledger,
            provider_subrequest_cache=restored_cache,
            run_scope_id=context.run_id,
            financial_eligibility_context=context,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
        )
    assert captured.value.reason is expected_reason


@pytest.mark.unit
def test_strict_pit_checkpoint_replay_preserves_exact_revision_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset, plan, context = _strict_temporal_context(monkeypatch)
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "fresh-artifacts"),
        run_scope_id=context.run_id,
    )
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_REQUESTED_SESSION,
        tool_call_id="ticket-05c-strict",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
    )
    selection_request = build_financial_statement_routing_request(request, context)
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    fresh = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        provider_subrequest_cache=cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    original = fresh.dispatch_statement_selection(request, selection_request)
    ledger = fresh.checkpoint_ledger()
    cache_checkpoint = cache.checkpoint()
    temporal = context.temporal_eligibility_binding
    assert temporal is not None
    assert temporal.strict_cutoff is not None
    for decision in (
        temporal.company_type_temporal_decision,
        temporal.listing_temporal_decision,
    ):
        assert decision.result == "accepted"
        assert decision.ann_date == date(2026, 8, 8)
        assert decision.f_ann_date == date(2026, 8, 9)
        assert decision.ann_date != decision.f_ann_date
        assert decision.observed_at == datetime(
            2026, 8, 10, 16, 30, tzinfo=timezone.utc
        )
        assert decision.retrieved_at == decision.observed_at
        assert decision.exact_revision_availability_proof is not None
        assert decision.exact_revision_authoritative_available_at == datetime(
            2026, 8, 10, 14, 45, tzinfo=timezone.utc
        )

    restored_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "restored-artifacts"),
        run_scope_id=context.run_id,
    )
    restored_cache.bind_financial_eligibility_context(
        context_contract_version=context.contract_version,
        context_identity=context.context_identity,
        context=context,
        capability_routing_plan=plan,
    )
    restored_cache.restore_checkpoint(cache_checkpoint)

    def forbidden(*_args, **_kwargs):
        pytest.fail("strict temporal replay performed fresh work")

    monkeypatch.setattr(
        MainlandFinancialCapabilityRouter,
        "route_statement",
        forbidden,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.financial_temporal_eligibility."
        "evaluate_financial_metadata_temporal_eligibility",
        forbidden,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.financial_temporal_eligibility."
        "latest_completed_session",
        forbidden,
    )
    restored = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        clock=forbidden,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        checkpoint_ledger=ledger,
        provider_subrequest_cache=restored_cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    replay = restored.dispatch_statement_selection(
        request.model_copy(update={"tool_call_id": "ticket-05c-strict-replay"}),
        selection_request,
    )

    assert replay.disposition == "duplicate_suppressed"
    assert replay.routing_result == original.routing_result
    assert replay.routing_result.temporal_eligibility_binding.model_dump(
        mode="json"
    ) == temporal.model_dump(mode="json")
    assert restored_cache.checkpoint() == cache_checkpoint


@pytest.mark.unit
def test_concurrent_restored_temporal_duplicates_share_state_without_fresh_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asset, plan, context = _temporal_context(monkeypatch)
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "fresh-artifacts"),
        run_scope_id=context.run_id,
    )
    request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_REQUESTED_SESSION,
        tool_call_id="ticket-05c-concurrent-original",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
    )
    selection_request = build_financial_statement_routing_request(request, context)
    unused = lambda *_args, **_kwargs: "unused"  # noqa: E731
    vendor_methods = {
        "get_fundamentals": {"scripted": unused},
        "get_balance_sheet": {"scripted": unused},
        "get_cashflow": {"scripted": unused},
        "get_income_statement": {"scripted": unused},
    }
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "mainland_capability_routing_plan_signature": plan.plan_signature,
            "asset_configuration_signature": asset.asset_configuration_signature,
            "tool_vendors": dict.fromkeys(vendor_methods, "scripted"),
        }
    )
    fresh = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        provider_subrequest_cache=cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    original = fresh.dispatch_statement_selection(request, selection_request)
    ledger = fresh.checkpoint_ledger()
    cache_checkpoint = cache.checkpoint()
    restored_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "restored-artifacts"),
        run_scope_id=context.run_id,
    )
    restored_cache.bind_financial_eligibility_context(
        context_contract_version=context.contract_version,
        context_identity=context.context_identity,
        context=context,
        capability_routing_plan=plan,
    )
    restored_cache.restore_checkpoint(cache_checkpoint)

    def forbidden(*_args, **_kwargs):
        pytest.fail("concurrent temporal replay performed fresh work")

    monkeypatch.setattr(
        MainlandFinancialCapabilityRouter,
        "route_statement",
        forbidden,
    )
    restored = FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        config=config,
        vendor_methods=vendor_methods,
        clock=forbidden,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        checkpoint_ledger=ledger,
        provider_subrequest_cache=restored_cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )

    def repeat(index: int):
        return restored.dispatch_statement_selection(
            request.model_copy(update={"tool_call_id": f"ticket-05c-repeat-{index}"}),
            selection_request,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(repeat, range(16)))

    assert all(item.disposition == "duplicate_suppressed" for item in results)
    assert all(item.routing_result == original.routing_result for item in results)
    assert restored_cache.checkpoint() == cache_checkpoint
    restored_ledger = restored.checkpoint_ledger()
    assert restored_ledger["qualified_statement_selections"][0]["reuse_count"] == 16


@pytest.mark.unit
def test_plan_1_2_requires_typed_start_new_under_temporal_plan_1_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": [
                "statements",
                "financial_indicators",
            ],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    asset = resolve_run_asset_configuration("601328.SS", config=config)
    preflight = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert preflight.plan is not None
    historical_plan = preflight.plan
    temporal_plan = prepare_mainland_temporal_capability_routing_plan_v1(
        historical_plan
    )

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as captured:
        validate_mainland_capability_routing_checkpoint(
            active_plan=temporal_plan,
            checkpoint_plan=historical_plan,
        )
    assert captured.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
    )

    compatibility = start_new_mainland_capability_routing_run(
        active_plan=temporal_plan,
        checkpoint_plan=historical_plan,
        previous_run_id="run:" + "1" * 64,
        new_run_id="run:" + "2" * 64,
    )
    assert compatibility.action is MainlandCapabilityRoutingCheckpointAction.START_NEW
    assert compatibility.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
    )
    assert compatibility.previous_run_id != compatibility.new_run_id
    assert compatibility.active.plan_version == "1.3"
    assert compatibility.checkpoint is not None
    assert compatibility.checkpoint.plan_version == "1.2"
    assert compatibility.provider_io_performed is False
    assert compatibility.checkpoint_rewrite_permitted is False
    assert compatibility.historical_state_preserved is True


@pytest.mark.unit
def test_temporal_start_new_acquires_fresh_context_for_distinct_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, historical_plan = _qualified_plan(monkeypatch)
    temporal_plan = prepare_mainland_temporal_capability_routing_plan_v1(
        historical_plan
    )
    _fixture_asset, _fixture_plan, fixture_context = _temporal_context(
        monkeypatch
    )
    retained_company = FinancialCompanyTypeEvidenceProjection(
        contract_version="financial-company-type-evidence-projection-v2",
        metadata_binding=FinancialEligibilityMetadataBinding(
            contract_version="financial-eligibility-metadata-binding-v2",
            instrument_identity=asset.instrument_identity,
            requested_session=_REQUESTED_SESSION,
            availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
            financial_eligibility_binding=(
                temporal_plan.financial_eligibility_binding
            ),
            routing_plan_signature=temporal_plan.plan_signature,
            temporal_mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
            eligibility_evaluated_at=_EVALUATED_AT,
            calendar_binding=_calendar_binding(),
        ),
        evidence=fixture_context.company_type_evidence,
        temporal_decision=(
            fixture_context.temporal_eligibility_binding.company_type_temporal_decision
        ),
    )
    retained_listing = FinancialListingMetadataProjection(
        contract_version="financial-listing-metadata-projection-v2",
        metadata_binding=retained_company.metadata_binding,
        metadata=FinancialListingMetadata(
            contract_version="financial-listing-metadata-v2",
            instrument_identity=asset.instrument_identity,
            requested_session=_REQUESTED_SESSION,
            listing_date=fixture_context.listing_date,
            listing_provenance=fixture_context.listing_provenance,
            temporal_evidence=fixture_context.listing_temporal_evidence,
        ),
        temporal_decision=(
            fixture_context.temporal_eligibility_binding.listing_temporal_decision
        ),
    )
    new_run_id = "run:" + "2" * 64
    compatibility = start_new_mainland_capability_routing_run(
        active_plan=temporal_plan,
        checkpoint_plan=historical_plan,
        previous_run_id="run:" + "1" * 64,
        new_run_id=new_run_id,
    )
    counts = {"clock": 0, "calendar": 0, "provider": 0}

    def eligibility_clock() -> datetime:
        counts["clock"] += 1
        return _EVALUATED_AT

    def calendar_provider() -> StoredSessionCalendar:
        counts["calendar"] += 1
        return _stored_calendar()

    def provider_must_not_run(_request):
        counts["provider"] += 1
        pytest.fail("START_NEW fixture unexpectedly acquired provider metadata")

    router = MainlandFinancialCapabilityRouter(
        routing_plan=temporal_plan,
        statement_sources={},
        capability_sources={},
        company_type_evidence_sources={"tushare": provider_must_not_run},
    )
    owner = FinancialEligibilityAcquisitionContextOwner(
        run_id=new_run_id,
        asset_configuration=asset,
        routing_plan=temporal_plan,
        metadata_acquirer=FinancialEligibilityMetadataAcquirer(
            routing_plan=temporal_plan,
            router=router,
            stop_signal=Event(),
        ),
        unbound_financial_routing=QualifiedFinancialRoutingComposition(
            router=router,
            statement_request_factory=lambda *_args: None,
        ),
        expected_as_of_date=_REQUESTED_SESSION,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        retained_company_type_evidence=(retained_company,),
        retained_listing_metadata=(retained_listing,),
        eligibility_clock=eligibility_clock,
        calendar_provider=calendar_provider,
        interrupt_runtime=lambda: None,
        close_runtime=lambda: None,
    )

    acquisition = owner.acquire_context(as_of_date=_REQUESTED_SESSION)

    assert compatibility.new_run_id == new_run_id
    assert acquisition.context is not None, acquisition.failure
    assert acquisition.context.contract_version == (
        "financial-eligibility-acquisition-context-v2"
    )
    assert acquisition.context.run_id == new_run_id
    assert acquisition.context.routing_plan_signature == temporal_plan.plan_signature
    assert acquisition.context.temporal_eligibility_binding is not None
    assert acquisition.context.temporal_eligibility_binding.eligibility_evaluated_at == (
        _EVALUATED_AT
    )
    assert counts == {"clock": 1, "calendar": 1, "provider": 0}

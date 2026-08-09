from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from hashlib import sha256
from types import MappingProxyType, SimpleNamespace

import pytest

from tests.test_financial_capability_routing_selection import (
    _candidate,
    _identity,
    _indicator_response_with_compatibility,
    _qualified_plan,
    _qualified_ratio_candidate,
    _shadow_plan,
    _with_raw_indicator_metadata,
)
from tests.test_financial_period_availability import _indicator_fixture
from tests.test_financial_version_persistence import _provider_chain
from tests.test_qualified_routing_checkpoint_context_restore import (
    _AS_OF as _CHECKPOINT_AS_OF,
    _CHECKPOINT_THREAD_ID,
    _context_fixture,
    _fresh_context_owner_fixture,
    _OwnedResource,
)
from tests.test_reporting import _outcome_state
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingCheckpointAction,
    MainlandCapabilityRoutingCheckpointError,
    MainlandCapabilityRoutingCheckpointFailureReason,
    MainlandCapabilityRoutingDisposition,
    MainlandCapabilityRoutingPlan,
    MainlandCapabilityRoutingRunProjection,
    start_new_mainland_capability_routing_run,
    validate_mainland_capability_routing_checkpoint,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialIndicatorRoutingResult,
    FinancialProviderPeriodResponse,
    MainlandFinancialCapabilityRouter,
    render_indicator_routing_result,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_V1,
    FINANCIAL_RATIO_DECLARATION_V2,
    FinancialAcquisitionManifest,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialIndicatorEvidenceRequirements,
    FinancialIndicatorMetadataDimensionBinding,
    FinancialPeriodCandidate,
    FinancialPeriodIdentity,
    FinancialRatioFamily,
    FinancialRenderedArtifactIdentity,
    FinancialReportingFrequency,
    FinancialStatementType,
    _canonical_digest,
    _manifest_identity_payload,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialCompletenessAuditRow,
    FinancialDispatchAuditProjectionV3,
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialDispatchCheckpointLedger,
    FinancialIndicatorRequestArguments,
    FinancialManifestAuditProjection,
    FinancialToolDispatcher,
    FinancialToolManifestOperationalProjection,
    FinancialToolMessageManifestEnvelope,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
    project_financial_selection_evidence,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeEvidenceBinding,
    FinancialEligibilityRequestBinding,
    build_financial_eligibility_acquisition_context,
)
from tradingagents.dataflows.financial_indicator_metadata import (
    evaluate_financial_indicator_candidates,
)
from tradingagents.dataflows.financial_request_factory import (
    build_financial_indicator_routing_request,
    build_financial_statement_routing_request,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactStore,
    ProviderSubrequestCache,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import AnalysisOutcome, EvidenceState
from tradingagents.qualified_routing_composition import (
    PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION,
    ProductionQualifiedRoutingRunBinding,
    ProductionQualifiedRoutingRuntimeAssembly,
    ProductionQualifiedRoutingRuntimeFactories,
    build_production_qualified_routing_composition_blueprint,
    build_registered_production_qualified_routing_composition,
)
from tradingagents.qualified_routing_eligibility_lifecycle import (
    FinancialEligibilityAcquisitionContextOwner,
    QualifiedFinancialRoutingComposition,
)
from tradingagents.reporting import render_analysis_outcome_report
from tradingagents.terminal_contract import build_terminal_contract

_AS_OF = date(2026, 8, 1)


def _plan_1_1_projection(
    plan: MainlandCapabilityRoutingPlan,
) -> dict[str, object]:
    historical = plan.model_dump(mode="json")
    historical["plan_version"] = "1.1"
    for field in (
        "indicator_field_semantics_version",
        "indicator_metadata_compatibility_version",
        "indicator_manifest_contract_version",
        "indicator_selection_contract_version",
    ):
        historical.pop(field, None)
    signature_payload = {
        key: value for key, value in historical.items() if key != "plan_signature"
    }
    historical["plan_signature"] = (
        "mainland-routing-plan:v1:"
        + sha256(
            json.dumps(
                signature_payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    return historical


def _reidentified_manifest(
    manifest: FinancialAcquisitionManifest,
    **updates: object,
) -> FinancialAcquisitionManifest:
    mutated = manifest.model_copy(update=updates)
    identity_payload = _manifest_identity_payload(
        contract_version=mutated.contract_version,
        capability_routing_plan_signature=(
            mutated.capability_routing_plan_signature
        ),
        instrument_identity=mutated.instrument_identity,
        requested_capability=mutated.requested_capability,
        requested_statement_type=mutated.requested_statement_type,
        requested_ratio_family=mutated.requested_ratio_family,
        artifacts=mutated.artifacts,
        acquisition_outcomes=mutated.acquisition_outcomes,
        provider_candidates=mutated.provider_candidates,
        selections=mutated.selections,
        rejected_periods=mutated.rejected_periods,
        overlaps=mutated.overlaps,
        conflicts=mutated.conflicts,
        revision_selections=mutated.revision_selections,
        aggregate_completeness=mutated.aggregate_completeness,
        publication_target_construction=(
            mutated.publication_target_construction
        ),
        physical_attempt_events=mutated.physical_attempt_events,
        disposition=mutated.disposition,
        final_rendered_artifact=mutated.final_rendered_artifact,
        requested_analysis_scope=mutated.requested_analysis_scope,
        availability_scope=mutated.availability_scope,
        availability_basis=mutated.availability_basis,
        eligibility_policy_version=mutated.eligibility_policy_version,
        financial_eligibility_binding=mutated.financial_eligibility_binding,
        availability_reason=mutated.availability_reason,
        indicator_field_semantics_version=(
            mutated.indicator_field_semantics_version
        ),
        indicator_metadata_compatibility_version=(
            mutated.indicator_metadata_compatibility_version
        ),
        metadata_compatibility_assessments=(
            mutated.metadata_compatibility_assessments
        ),
        metadata_incompatible_rejections=(
            mutated.metadata_incompatible_rejections
        ),
        selected_families=mutated.selected_families,
        unselected_families=mutated.unselected_families,
        unresolved_families=mutated.unresolved_families,
    )
    version = mutated.contract_version.rsplit("-v", 1)[1]
    payload = mutated.model_dump(mode="json")
    payload["manifest_identity"] = (
        f"financial-manifest:v{version}:" + _canonical_digest(identity_payload)
    )
    return FinancialAcquisitionManifest.model_validate(payload)


def _with_instrument_identity(
    candidate: FinancialPeriodCandidate,
    instrument_identity,
) -> FinancialPeriodCandidate:
    identity = candidate.period_identity
    return FinancialPeriodCandidate.create(
        artifact=candidate.artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=instrument_identity,
            capability=identity.capability,
            statement_type=identity.statement_type,
            ratio_family=identity.ratio_family,
            period_end=identity.period_end,
            frequency=identity.frequency,
            company_type=identity.company_type,
            consolidation_scope=identity.consolidation_scope,
            currency=identity.currency,
            provider_revision_binding=identity.provider_revision_binding,
        ),
        filing_metadata=candidate.filing_metadata,
        company_type_resolution=candidate.company_type_resolution,
        fields=candidate.fields,
    )


def _with_incompatible_original_unit(
    candidate: FinancialPeriodCandidate,
) -> FinancialPeriodCandidate:
    incompatible_field = candidate.fields[0]
    fields = (
        FinancialFieldValue(
            **{
                **incompatible_field.model_dump(),
                "original_unit": "CURRENCY_BASE_UNIT",
            }
        ),
        *candidate.fields[1:],
    )
    return FinancialPeriodCandidate.create(
        artifact=candidate.artifact,
        period_identity=candidate.period_identity,
        filing_metadata=candidate.filing_metadata,
        company_type_resolution=candidate.company_type_resolution,
        fields=fields,
    )


def _compatible_indicator_result(monkeypatch: pytest.MonkeyPatch):
    request, response, plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="current_only",
        f_ann_date=None,
        as_of_date=_AS_OF,
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        indicator_sources={"tushare": lambda _request: response},
    ).route_indicators(request)
    return request, result


def _mixed_compatibility_result(monkeypatch: pytest.MonkeyPatch):
    period = date(2026, 3, 31)
    tushare_growth = _qualified_ratio_candidate(
        provider_id="tushare",
        ratio_family=FinancialRatioFamily.GROWTH,
        period_end=period,
        declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
        currency="XXX",
    )
    tushare_balance = _with_raw_indicator_metadata(
        _qualified_ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.BALANCE,
            period_end=period,
            declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
            currency="XXX",
        ),
        currency="XXX",
        scope=FinancialConsolidationScope.CONSOLIDATED,
    )
    akshare_growth = _with_raw_indicator_metadata(
        _qualified_ratio_candidate(
            provider_id="akshare",
            ratio_family=FinancialRatioFamily.GROWTH,
            period_end=period,
            declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
            currency="XXX",
        ),
        currency="CNY",
        scope=FinancialConsolidationScope.CONSOLIDATED,
    )
    router = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            "tushare": lambda _request: _indicator_response_with_compatibility(
                "tushare",
                (tushare_growth, tushare_balance),
                declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
            ),
            "akshare": lambda _request: _indicator_response_with_compatibility(
                "akshare",
                (akshare_growth,),
                declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
            ),
        },
    )
    result = router.route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(
                FinancialRatioFamily.GROWTH,
                FinancialRatioFamily.BALANCE,
            ),
            eligible_reporting_period_ends=(period,),
        )
    )
    return result, tushare_growth.candidate_identity


def _context_bound_indicator_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    include_statement: bool = False,
    indicator_available: bool = True,
) -> SimpleNamespace:
    _config, asset, _context_plan, context = _context_fixture(monkeypatch, tmp_path)
    _fixture_request, response, plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="current_only",
        f_ann_date=None,
        as_of_date=context.as_of_date,
        instrument_identity=asset.instrument_identity,
    )
    resolution = response.candidates[0].company_type_resolution
    retained_evidence = context.company_type_evidence
    assert retained_evidence is not None
    company_type_evidence = FinancialCompanyTypeEvidenceBinding.create(
        provider_id=retained_evidence.provider_id,
        artifact_identity=retained_evidence.artifact_identity,
        source_ref=retained_evidence.source_ref,
        observed_at=retained_evidence.observed_at,
        retrieved_at=retained_evidence.retrieved_at,
        provider_declared_type=resolution.provider_declared_type,
        provider_declaration_qualified=resolution.provider_declaration_qualified,
        normalized_resolver_inputs=resolution.classifier_inputs,
    )
    rebuilt_context = build_financial_eligibility_acquisition_context(
        canonical_run_id=context.run_id,
        run_id=context.run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        authoritative_as_of_date=context.as_of_date,
        as_of_date=context.as_of_date,
        authoritative_availability_scope=context.availability_scope,
        availability_scope=context.availability_scope,
        acquisition_purpose=context.acquisition_purpose,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan=plan,
        routing_plan_signature=plan.plan_signature,
        company_type_resolution=resolution,
        company_type_evidence=company_type_evidence,
        listing_date=context.listing_date,
        listing_provenance=context.listing_provenance,
        currency=context.currency,
        consolidation_scope=context.consolidation_scope,
        acquisition_annual_period_ends=context.acquisition_annual_period_ends,
        acquisition_reporting_period_ends=(
            context.acquisition_reporting_period_ends
        ),
    )
    assert rebuilt_context.context is not None, rebuilt_context
    context = rebuilt_context.context
    requirements = FinancialIndicatorEvidenceRequirements.create(
        requested_families=(FinancialRatioFamily.PROFIT,),
        canonical_evidence_contract_id="canonical-evidence-contract.v1",
        canonical_evidence_contract_required_families=(FinancialRatioFamily.PROFIT,),
        capability_profile_id="equity.v1",
        capability_profile_required_families=(),
        strategy_rules_id="mainland-equity-strategy-rules.v1",
    )
    tool_request = FinancialToolRequest(
        tool_name="get_fundamentals",
        statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=FinancialReportingFrequency.NOT_APPLICABLE,
        as_of_date=context.as_of_date,
        material_arguments={"projection": "ratios"},
        tool_call_id="ticket-05b-case",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
        indicator_request_arguments=FinancialIndicatorRequestArguments(
            requested_families=(FinancialRatioFamily.PROFIT,),
            family_evidence_requirements=requirements,
        ),
    )
    selection_request = build_financial_indicator_routing_request(
        tool_request,
        context,
    )
    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "05b-case-store"),
        run_scope_id=context.run_id,
    )
    statement_sources = {}
    provider_chains = {"get_fundamentals": _provider_chain()}
    if include_statement:
        statement_candidate = _candidate(
            provider_id="tushare",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            period_end=context.acquisition_reporting_period_ends[-1],
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=context.company_type_resolution.company_type,
            currency=context.currency,
            consolidation_scope=context.consolidation_scope,
            ann_date=context.as_of_date,
            f_ann_date=context.as_of_date,
            instrument_identity=asset.instrument_identity,
        )
        statement_response = FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=statement_candidate.artifact,
            retained_artifacts=(statement_candidate.artifact,),
            candidates=(statement_candidate,),
            subrequest_keys=(),
            physical_attempt_ids=(),
        )
        statement_sources = {
            "tushare": lambda _request: statement_response,
        }
        provider_chains["get_balance_sheet"] = _provider_chain()
    provider_calls = 0

    def source(_request):
        nonlocal provider_calls
        provider_calls += 1
        return response

    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains=provider_chains,
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources=statement_sources,
            indicator_sources=(
                {"tushare": source} if indicator_available else {}
            ),
        ),
        provider_subrequest_cache=cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    result = dispatcher.dispatch_indicator_selection(tool_request, selection_request)
    return SimpleNamespace(
        asset=asset,
        context=context,
        plan=plan,
        tool_request=tool_request,
        selection_request=selection_request,
        result=result,
        dispatcher=dispatcher,
        checkpoint=dispatcher.checkpoint_ledger(),
        provider_calls=lambda: provider_calls,
    )


def _mixed_context_bound_indicator_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> SimpleNamespace:
    _config, _seed_asset, _seed_plan, seed_context = _context_fixture(
        monkeypatch,
        tmp_path,
    )
    plan = _shadow_plan(monkeypatch)
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    periods = seed_context.acquisition_reporting_period_ends
    tushare_growth = tuple(
        _with_incompatible_original_unit(
            _with_raw_indicator_metadata(
                _with_instrument_identity(
                    _qualified_ratio_candidate(
                        provider_id="tushare",
                        ratio_family=FinancialRatioFamily.GROWTH,
                        period_end=period,
                        declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
                        currency="CNY",
                    ),
                    asset.instrument_identity,
                ),
                currency="CNY",
                scope=FinancialConsolidationScope.CONSOLIDATED,
            ),
        )
        for period in periods
    )
    akshare_growth = tuple(
        _with_raw_indicator_metadata(
            _with_instrument_identity(
                _qualified_ratio_candidate(
                    provider_id="akshare",
                    ratio_family=FinancialRatioFamily.GROWTH,
                    period_end=period,
                    declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
                    currency="XXX",
                ),
                asset.instrument_identity,
            ),
            currency="CNY",
            scope=FinancialConsolidationScope.CONSOLIDATED,
        )
        for period in periods
    )

    def response(
        provider_id: str,
        candidates,
        declaration_version: str,
    ) -> FinancialProviderPeriodResponse:
        return FinancialProviderPeriodResponse.available(
            provider_id=provider_id,
            response_artifact=candidates[0].artifact,
            retained_artifacts=tuple(item.artifact for item in candidates),
            candidates=tuple(candidates),
            subrequest_keys=(),
            physical_attempt_ids=(),
            indicator_metadata_compatibility=evaluate_financial_indicator_candidates(
                candidates=tuple(candidates),
                instrument_identity=asset.instrument_identity,
                requested_value_currency="CNY",
                requested_filing_consolidation_scope=(
                    FinancialConsolidationScope.CONSOLIDATED
                ),
                pit_as_of_date=seed_context.as_of_date,
                declaration_version=declaration_version,
                normalizer_contract_version=(
                    f"{provider_id}-indicator-normalizer-v2"
                ),
            ),
        )

    resolution = tushare_growth[0].company_type_resolution
    retained_evidence = seed_context.company_type_evidence
    assert retained_evidence is not None
    company_type_evidence = FinancialCompanyTypeEvidenceBinding.create(
        provider_id=retained_evidence.provider_id,
        artifact_identity=retained_evidence.artifact_identity,
        source_ref=retained_evidence.source_ref,
        observed_at=retained_evidence.observed_at,
        retrieved_at=retained_evidence.retrieved_at,
        provider_declared_type=resolution.provider_declared_type,
        provider_declaration_qualified=resolution.provider_declaration_qualified,
        normalized_resolver_inputs=resolution.classifier_inputs,
    )
    rebuilt_context = build_financial_eligibility_acquisition_context(
        canonical_run_id=seed_context.run_id,
        run_id=seed_context.run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        authoritative_as_of_date=seed_context.as_of_date,
        as_of_date=seed_context.as_of_date,
        authoritative_availability_scope=seed_context.availability_scope,
        availability_scope=seed_context.availability_scope,
        acquisition_purpose=seed_context.acquisition_purpose,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan=plan,
        routing_plan_signature=plan.plan_signature,
        company_type_resolution=resolution,
        company_type_evidence=company_type_evidence,
        listing_date=seed_context.listing_date,
        listing_provenance=seed_context.listing_provenance,
        currency=seed_context.currency,
        consolidation_scope=seed_context.consolidation_scope,
        acquisition_annual_period_ends=seed_context.acquisition_annual_period_ends,
        acquisition_reporting_period_ends=periods,
    )
    assert rebuilt_context.context is not None, rebuilt_context
    context = rebuilt_context.context
    requirements = FinancialIndicatorEvidenceRequirements.create(
        requested_families=(FinancialRatioFamily.GROWTH,),
        canonical_evidence_contract_id="canonical-evidence-contract.v1",
        canonical_evidence_contract_required_families=(
            FinancialRatioFamily.GROWTH,
        ),
        capability_profile_id="equity.v1",
        capability_profile_required_families=(),
        strategy_rules_id="mainland-equity-strategy-rules.v1",
    )
    tool_request = FinancialToolRequest(
        tool_name="get_fundamentals",
        statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=FinancialReportingFrequency.NOT_APPLICABLE,
        as_of_date=context.as_of_date,
        material_arguments={"projection": "ratios"},
        tool_call_id="ticket-05b-mixed",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
        indicator_request_arguments=FinancialIndicatorRequestArguments(
            requested_families=(FinancialRatioFamily.GROWTH,),
            family_evidence_requirements=requirements,
        ),
    )
    selection_request = build_financial_indicator_routing_request(
        tool_request,
        context,
    )
    responses = {
        "tushare": response(
            "tushare",
            tushare_growth,
            FINANCIAL_RATIO_DECLARATION_V2,
        ),
        "akshare": response(
            "akshare",
            akshare_growth,
            FINANCIAL_RATIO_DECLARATION_V1,
        ),
    }
    provider_calls: list[str] = []

    def source(provider_id: str):
        def invoke(_request):
            provider_calls.append(provider_id)
            return responses[provider_id]

        return invoke

    cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "05b-mixed-store"),
        run_scope_id=context.run_id,
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_fundamentals": _provider_chain()},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={
                provider_id: source(provider_id) for provider_id in responses
            },
        ),
        provider_subrequest_cache=cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    dispatch = dispatcher.dispatch_indicator_selection(
        tool_request,
        selection_request,
    )
    assert (
        dispatch.routing_result.metadata_incompatible_rejections
    ), dispatch.routing_result
    return SimpleNamespace(
        asset=asset,
        context=context,
        plan=plan,
        tool_request=tool_request,
        selection_request=selection_request,
        dispatch=dispatch,
        checkpoint=dispatcher.checkpoint_ledger(),
        incompatible_candidate_identity=(
            dispatch.routing_result.metadata_incompatible_rejections[
                0
            ].candidate_identity
        ),
        provider_calls=provider_calls,
    )


@pytest.mark.unit
def test_plan_1_2_indicator_contracts_write_forward_versions_and_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request, result = _compatible_indicator_result(monkeypatch)

    assert result.contract_version == "financial-indicator-routing-result-v3"
    assert result.complete_families == (FinancialRatioFamily.PROFIT,)
    assert result.unresolved_families == ()
    assert result.indicator_field_semantics_version == (
        "financial-indicator-field-semantics-v1"
    )
    assert result.indicator_metadata_compatibility_version == (
        "financial-indicator-metadata-compatibility-v1"
    )

    manifest = result.family_manifests[0]
    assert manifest.contract_version == "financial-manifest-v3"
    assert manifest.manifest_identity.startswith("financial-manifest:v3:")
    assert manifest.indicator_field_semantics_version == (
        "financial-indicator-field-semantics-v1"
    )
    assert manifest.indicator_metadata_compatibility_version == (
        "financial-indicator-metadata-compatibility-v1"
    )
    assert manifest.selected_families == (FinancialRatioFamily.PROFIT,)
    assert manifest.unresolved_families == ()
    assert manifest.unselected_families == ()
    assert manifest.metadata_compatibility_assessments
    assert all(
        binding.compatibility_state == "compatible"
        for binding in manifest.metadata_compatibility_assessments
    )
    assert all(
        wrapper.contract_version == "financial-provider-candidate-v2"
        for wrapper in manifest.provider_candidates
    )
    assert all(
        selection.contract_version == "financial-period-selection-v3"
        for selection in manifest.selections
    )
    assert all(
        selection.selection_identity.startswith("financial-period-selection:v3:")
        for selection in manifest.selections
    )

    serialized = result.model_dump(mode="json")
    assert "metadata_compatibility_assessments" in serialized
    assert "unresolved_families" in serialized
    assert FinancialIndicatorRoutingResult.model_validate(serialized) == result

    assessment_identities = {
        assessment.assessment_identity
        for assessment in result.metadata_compatibility_assessments
    }
    assert assessment_identities == {
        binding.assessment_identity
        for binding in manifest.metadata_compatibility_assessments
    }
    assert {
        wrapper.metadata_compatibility_assessment_identity
        for wrapper in manifest.provider_candidates
    } == assessment_identities
    assert {
        selection.metadata_compatibility_assessment_identity
        for selection in manifest.selections
    } <= assessment_identities


@pytest.mark.unit
def test_manifest_metadata_diagnostics_reject_raw_values_and_unbounded_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request, result = _compatible_indicator_result(monkeypatch)
    dimension = result.family_manifests[0].metadata_compatibility_assessments[
        0
    ].dimensions[0]
    payload = dimension.model_dump(mode="json")

    with pytest.raises(ValueError):
        FinancialIndicatorMetadataDimensionBinding.model_validate(
            {**payload, "raw_provider_value": "CNY"}
        )
    with pytest.raises(ValueError):
        FinancialIndicatorMetadataDimensionBinding.model_validate(
            {**payload, "raw_provider_marker": "CNY"}
        )

    assert "raw_provider_value" not in json.dumps(
        result.family_manifests[0].model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    )


@pytest.mark.unit
def test_plan_1_1_requires_explicit_start_new_for_plan_1_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _qualified_plan(monkeypatch)
    historical = _plan_1_1_projection(plan)
    historical_bytes = json.dumps(
        historical,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as mismatch:
        validate_mainland_capability_routing_checkpoint(
            active_plan=plan,
            checkpoint_plan=historical,
        )
    assert mismatch.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
    )

    previous_run_id = "run:" + "1" * 64
    new_run_id = "run:" + "2" * 64
    compatibility = start_new_mainland_capability_routing_run(
        active_plan=plan,
        checkpoint_plan=historical,
        previous_run_id=previous_run_id,
        new_run_id=new_run_id,
    )

    assert compatibility.action is MainlandCapabilityRoutingCheckpointAction.START_NEW
    assert compatibility.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
    )
    assert compatibility.previous_run_id == previous_run_id
    assert compatibility.new_run_id == new_run_id
    assert compatibility.active.plan_version == "1.2"
    assert compatibility.checkpoint is not None
    assert compatibility.checkpoint.plan_version == "1.1"
    assert compatibility.provider_io_performed is False
    assert compatibility.checkpoint_rewrite_permitted is False
    assert compatibility.historical_state_preserved is True
    assert json.dumps(
        historical,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() == historical_bytes


@pytest.mark.unit
def test_plan_1_1_start_new_acquires_fresh_context_for_distinct_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _config, _asset, plan, owner, source_calls = _fresh_context_owner_fixture(
        monkeypatch,
        tmp_path,
        checkpoint_thread_id="05b1a2b3c4d5e6f7",
    )
    historical = _plan_1_1_projection(plan)
    historical_bytes = json.dumps(
        historical,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    previous_run_id = "run:" + "a" * 64

    compatibility = start_new_mainland_capability_routing_run(
        active_plan=plan,
        checkpoint_plan=historical,
        previous_run_id=previous_run_id,
        new_run_id=owner.run_id,
    )

    assert compatibility.action is MainlandCapabilityRoutingCheckpointAction.START_NEW
    assert compatibility.previous_run_id == previous_run_id
    assert compatibility.new_run_id == owner.run_id
    assert compatibility.active.plan_version == "1.2"
    assert source_calls == []

    acquisition = owner.acquire_context(as_of_date=_CHECKPOINT_AS_OF)

    assert acquisition.context is not None
    assert acquisition.context.run_id == compatibility.new_run_id
    assert acquisition.context.routing_plan_signature == plan.plan_signature
    assert source_calls == ["company_type:tushare", "listing:baostock"]
    assert json.dumps(
        historical,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() == historical_bytes


@pytest.mark.unit
def test_incompatible_candidate_and_lower_provider_selection_restore_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, incompatible_candidate_identity = _mixed_compatibility_result(monkeypatch)

    restored = FinancialIndicatorRoutingResult.model_validate(
        result.model_dump(mode="json")
    )
    assert restored == result
    rejection = next(
        item
        for item in restored.metadata_incompatible_rejections
        if item.candidate_identity == incompatible_candidate_identity
    )
    assert rejection.typed_reasons
    growth_manifest = next(
        item
        for item in restored.family_manifests
        if item.requested_ratio_family is FinancialRatioFamily.GROWTH
    )
    incompatible_wrapper = next(
        item
        for item in growth_manifest.provider_candidates
        if item.candidate.candidate_identity == incompatible_candidate_identity
    )
    assert incompatible_wrapper.metadata_compatibility_state == "incompatible"
    assert incompatible_wrapper.metadata_selection_disposition == "unselected"
    assert incompatible_wrapper.metadata_incompatibility_reasons
    assert {
        item.artifact.dataset.provider_id for item in restored.selected_candidates
    } == {"akshare", "tushare"}


@pytest.mark.unit
def test_plan_1_2_unresolved_family_without_candidates_writes_v3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = date(2026, 3, 31)

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={},
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.GROWTH,),
            eligible_reporting_period_ends=(period,),
        )
    )

    manifest = result.family_manifests[0]
    assert result.contract_version == "financial-indicator-routing-result-v3"
    assert manifest.contract_version == "financial-manifest-v3"
    assert manifest.metadata_compatibility_assessments == ()
    assert manifest.provider_candidates == ()
    assert result.selected_candidates == ()
    assert result.unresolved_families == (FinancialRatioFamily.GROWTH,)
    assert FinancialIndicatorRoutingResult.model_validate(
        result.model_dump(mode="json")
    ) == result


@pytest.mark.unit
def test_unresolved_family_and_cross_bindings_are_persisted_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = date(2026, 3, 31)
    incompatible = _qualified_ratio_candidate(
        provider_id="tushare",
        ratio_family=FinancialRatioFamily.GROWTH,
        period_end=period,
        declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
        currency="XXX",
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_qualified_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            "tushare": lambda _request: _indicator_response_with_compatibility(
                "tushare",
                (incompatible,),
                declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
            ),
            "akshare": lambda _request: FinancialProviderPeriodResponse.unavailable(
                provider_id="akshare",
                reason="no_data",
            ),
        },
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            instrument_identity=_identity(),
            as_of_date=date(2026, 7, 30),
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.GROWTH,),
            eligible_reporting_period_ends=(period,),
        )
    )

    assert result.complete_families == ()
    assert result.unresolved_families == (FinancialRatioFamily.GROWTH,)
    assert result.family_manifests[0].unresolved_families == (
        FinancialRatioFamily.GROWTH,
    )
    assert FinancialIndicatorRoutingResult.model_validate(
        result.model_dump(mode="json")
    ) == result

    mismatched = copy.deepcopy(result.model_dump(mode="json"))
    mismatched["family_manifests"][0]["metadata_compatibility_assessments"][0][
        "assessment_identity"
    ] = "financial-indicator-metadata-compatibility:v1:" + "0" * 64
    with pytest.raises(ValueError, match="assessment|manifest"):
        FinancialIndicatorRoutingResult.model_validate(mismatched)

    contradiction = copy.deepcopy(result.model_dump(mode="json"))
    contradiction["family_manifests"][0]["selected_families"] = ["growth"]
    contradiction["family_manifests"][0]["unresolved_families"] = []
    with pytest.raises(ValueError, match="family disposition|manifest"):
        FinancialIndicatorRoutingResult.model_validate(contradiction)


@pytest.mark.unit
def test_context_bound_indicator_dispatch_checkpoint_and_tool_use_05b_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _context_bound_indicator_case(monkeypatch, tmp_path)
    asset = case.asset
    context = case.context
    plan = case.plan
    tool_request = case.tool_request
    selection_request = case.selection_request
    result = case.result
    checkpoint = case.checkpoint
    ledger = FinancialDispatchCheckpointLedger.model_validate(checkpoint)
    entry = ledger.qualified_indicator_selections[0]
    envelope = result.to_tool_message().artifact
    audit = project_financial_dispatch_ledger(
        ledger,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id=context.run_id,
    )

    assert audit is not None
    assert result.contract_version == "financial-indicator-selection-dispatch-v2"
    assert result.routing_result.complete_families == (
        FinancialRatioFamily.PROFIT,
    )
    assert result.routing_result.unresolved_families == ()
    assert result.routing_result.selected_candidates
    assert entry.contract_version == "financial-indicator-selection-checkpoint-v2"
    assert ledger.contract_version == "1.5"
    assert ledger.financial_manifest_version == "financial-manifest-v2"
    assert ledger.financial_indicator_manifest_version == "financial-manifest-v3"
    assert ledger.indicator_field_semantics_version == (
        "financial-indicator-field-semantics-v1"
    )
    assert ledger.indicator_metadata_compatibility_version == (
        "financial-indicator-metadata-compatibility-v1"
    )
    assert envelope["contract_version"] == "financial-tool-message-envelope-v3"
    assert envelope["manifests"][0]["contract_version"] == (
        "financial-tool-manifest-v3"
    )
    assert audit.contract_version == "3.0"
    assert audit.manifest_requests[0].contract_version == (
        "financial-manifest-request-audit-v3"
    )
    assert audit.manifest_requests[0].manifests[0].contract_version == (
        "financial-manifest-audit-v3"
    )
    assert (
        audit.manifest_requests[0]
        .manifests[0]
        .aggregate_completeness.contract_version
        == "financial-aggregate-completeness-v3"
    )
    assert audit.completeness_rows
    assert all(
        row.contract_version == "financial-completeness-row-v4"
        for row in audit.completeness_rows
    )
    assert {
        row.metadata_compatibility_assessment_identity
        for row in audit.completeness_rows
        if row.metadata_compatibility_assessment_identity is not None
    } == set(entry.terminal.metadata_assessment_identities)

    restored_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(
            tmp_path / "05b-restored-artifacts"
        ),
        run_scope_id=context.run_id,
    )

    def provider_must_not_run(_request):
        pytest.fail("exact indicator restore must not invoke a provider")

    restored_dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_fundamentals": _provider_chain()},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={"tushare": provider_must_not_run},
        ),
        provider_subrequest_cache=restored_cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
        checkpoint_ledger=checkpoint,
    )
    assert restored_dispatcher.checkpoint_ledger() == checkpoint
    repeated = restored_dispatcher.dispatch_indicator_selection(
        tool_request.model_copy(update={"tool_call_id": "ticket-05b-restored"}),
        selection_request,
    )
    assert repeated.disposition == "duplicate_suppressed"
    assert repeated.routing_result == result.routing_result
    assert case.provider_calls() == 1
    assert (
        restored_dispatcher.checkpoint_ledger()["provider_subrequest_cache"]
        == checkpoint["provider_subrequest_cache"]
    )
    assert repeated.routing_result.physical_attempt_events == (
        result.routing_result.physical_attempt_events
    )


@pytest.mark.unit
def test_indicator_manifest_v3_does_not_enter_canonical_evidence_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _context_bound_indicator_case(monkeypatch, tmp_path)
    evidence = EvidenceState(
        instrument_identity=case.asset.instrument_identity,
    )

    projected = project_financial_selection_evidence(evidence, case.result)

    assert projected == evidence


@pytest.mark.unit
def test_plan_1_2_ledger_keeps_statement_manifest_v2_beside_indicator_v3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _context_bound_indicator_case(
        monkeypatch,
        tmp_path,
        include_statement=True,
    )
    statement_tool_request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=case.context.as_of_date,
        tool_call_id="ticket-05b-statement-v2",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(case.context)
        ),
    )
    statement_request = build_financial_statement_routing_request(
        statement_tool_request,
        case.context,
    )

    statement_dispatch = case.dispatcher.dispatch_statement_selection(
        statement_tool_request,
        statement_request,
    )
    ledger = FinancialDispatchCheckpointLedger.model_validate(
        case.dispatcher.checkpoint_ledger()
    )

    assert case.plan.plan_version == "1.2"
    assert statement_dispatch.routing_result.manifest.contract_version == (
        "financial-manifest-v2"
    )
    assert ledger.contract_version == "1.5"
    assert ledger.financial_manifest_version == "financial-manifest-v2"
    assert ledger.financial_indicator_manifest_version == "financial-manifest-v3"
    assert ledger.qualified_statement_selections[0].terminal.routing_result.manifest == (
        statement_dispatch.routing_result.manifest
    )
    assert (
        ledger.qualified_indicator_selections[0]
        .terminal.routing_result.family_manifests[0]
        .contract_version
        == "financial-manifest-v3"
    )


@pytest.mark.unit
def test_zero_candidate_unresolved_v3_projects_audit_report_and_tool_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _context_bound_indicator_case(
        monkeypatch,
        tmp_path,
        indicator_available=False,
    )
    audit = project_financial_dispatch_ledger(
        FinancialDispatchCheckpointLedger.model_validate(case.checkpoint),
        run_asset_configuration=case.asset,
        capability_routing_plan=case.plan,
        run_scope_id=case.context.run_id,
    )
    envelope = case.result.to_tool_message().artifact

    assert isinstance(audit, FinancialDispatchAuditProjectionV3)
    assert audit.metadata_assessment_identities == ()
    assert audit.metadata_compatibility_states == ()
    manifest_audit = audit.manifest_requests[0].manifests[0]
    assert manifest_audit.contract_version == "financial-manifest-audit-v3"
    assert manifest_audit.aggregate_completeness.contract_version == (
        "financial-aggregate-completeness-v3"
    )
    assert manifest_audit.aggregate_completeness.family_disposition == "unresolved"
    assert all(
        row.contract_version == "financial-completeness-row-v4"
        and row.family_disposition == "unresolved"
        for row in audit.completeness_rows
    )
    assert envelope["contract_version"] == "financial-tool-message-envelope-v3"
    assert envelope.get("metadata_assessment_identities", []) == []
    assert envelope["manifests"][0]["contract_version"] == (
        "financial-tool-manifest-v3"
    )

    outcome_state = _outcome_state()
    report = render_analysis_outcome_report(
        AnalysisOutcome.model_validate(
            outcome_state["analysis_outcome_contract"]
        ),
        build_terminal_contract(outcome_state),
        EvidenceState.model_validate(outcome_state["evidence_state"]),
        audit,
    )
    assert "financial-aggregate-completeness-v3" in report
    assert "unresolved" in report
    assert case.provider_calls() == 0


@pytest.mark.unit
def test_indicator_routing_result_v3_rejects_publication_target_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    result = _context_bound_indicator_case(
        monkeypatch,
        tmp_path,
    ).result.routing_result
    assert result.publication_target_construction is not None
    assert result.family_manifests[0].publication_target_construction is not None
    render_seed = result.model_copy(
        update={"publication_target_construction": None}
    )
    manifest = _reidentified_manifest(
        result.family_manifests[0],
        final_rendered_artifact=FinancialRenderedArtifactIdentity.create(
            render_indicator_routing_result(render_seed)
        ),
    )
    payload = result.model_dump(mode="json")
    payload["publication_target_construction"] = None
    payload["family_manifests"] = [manifest.model_dump(mode="json")]

    with pytest.raises(ValueError, match="publication targets"):
        FinancialIndicatorRoutingResult.model_validate(payload)


@pytest.mark.unit
def test_mixed_compatibility_checkpoint_restores_rejections_and_fallback_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _mixed_context_bound_indicator_case(monkeypatch, tmp_path)
    original = case.dispatch.routing_result
    assert case.plan.plan_version == "1.2"
    checkpoint_bytes = json.dumps(
        case.checkpoint,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    rejection = next(
        item
        for item in original.metadata_incompatible_rejections
        if item.candidate_identity == case.incompatible_candidate_identity
    )
    assert rejection.typed_reasons
    assert case.checkpoint["indicator_metadata_rejections"]
    assert original.unresolved_families == ()
    assert original.complete_families == (FinancialRatioFamily.GROWTH,)
    assert {
        (
            candidate.period_identity.ratio_family,
            candidate.artifact.dataset.provider_id,
        )
        for candidate in original.selected_candidates
    } == {
        (FinancialRatioFamily.GROWTH, "akshare"),
    }

    restore_provider_calls = 0

    def provider_must_not_run(_request):
        nonlocal restore_provider_calls
        restore_provider_calls += 1
        pytest.fail("mixed checkpoint restore must not invoke provider fallback")

    restored_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(
            tmp_path / "05b-mixed-restored-store"
        ),
        run_scope_id=case.context.run_id,
    )
    restored_dispatcher = FinancialToolDispatcher(
        instrument_identity=case.asset.instrument_identity,
        run_asset_configuration=case.asset,
        provider_chains={"get_fundamentals": _provider_chain()},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=case.plan,
            statement_sources={},
            indicator_sources=dict.fromkeys(
                case.plan.route_for(MainlandCapability.FINANCIAL_INDICATORS),
                provider_must_not_run,
            ),
        ),
        provider_subrequest_cache=restored_cache,
        run_scope_id=case.context.run_id,
        financial_eligibility_context=case.context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
        checkpoint_ledger=case.checkpoint,
    )
    restored = restored_dispatcher.dispatch_indicator_selection(
        case.tool_request.model_copy(
            update={"tool_call_id": "ticket-05b-mixed-restored"}
        ),
        case.selection_request,
    )

    assert restored.disposition == "duplicate_suppressed"
    assert restored.routing_result == original
    assert restored.routing_result.metadata_incompatible_rejections == (
        original.metadata_incompatible_rejections
    )
    assert restored.routing_result.metadata_compatibility_assessments == (
        original.metadata_compatibility_assessments
    )
    restored_manifest = next(
        manifest
        for manifest in restored.routing_result.family_manifests
        if manifest.requested_ratio_family is FinancialRatioFamily.GROWTH
    )
    retained_incompatible = next(
        wrapper
        for wrapper in restored_manifest.provider_candidates
        if wrapper.candidate.candidate_identity
        == case.incompatible_candidate_identity
    )
    assert retained_incompatible.metadata_compatibility_state == "incompatible"
    assert retained_incompatible.metadata_selection_disposition == "unselected"
    assert retained_incompatible.metadata_incompatibility_reasons
    assert restore_provider_calls == 0
    assert case.provider_calls == ["tushare", "akshare"]
    assert json.dumps(
        case.checkpoint,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() == checkpoint_bytes


@pytest.mark.unit
def test_indicator_audit_report_exposes_bounded_compatibility_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _mixed_context_bound_indicator_case(monkeypatch, tmp_path)
    ledger = FinancialDispatchCheckpointLedger.model_validate(case.checkpoint)
    audit = project_financial_dispatch_ledger(
        ledger,
        run_asset_configuration=case.asset,
        capability_routing_plan=case.plan,
        run_scope_id=case.context.run_id,
    )
    assert isinstance(audit, FinancialDispatchAuditProjectionV3)
    rejection = case.dispatch.routing_result.metadata_incompatible_rejections[0]

    outcome_state = _outcome_state()
    report = render_analysis_outcome_report(
        AnalysisOutcome.model_validate(
            outcome_state["analysis_outcome_contract"]
        ),
        build_terminal_contract(outcome_state),
        EvidenceState.model_validate(outcome_state["evidence_state"]),
        audit,
    )

    assert "financial-manifest-v3" in report
    assert "financial-completeness-row-v4" in report
    assert "financial-indicator-field-semantics-v1" in report
    assert "financial-indicator-metadata-compatibility-v1" in report
    assert rejection.compatibility_assessment.assessment_identity in report
    assert all(reason.reason.value in report for reason in rejection.typed_reasons)
    assert "incompatible" in report
    assert "unselected" in report
    assert "current_only" in report
    assert "normalized_value" not in report
    assert "original_value" not in report
    assert "raw_provider_value" not in report


@pytest.mark.unit
def test_indicator_audit_and_tool_projections_reject_partial_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _mixed_context_bound_indicator_case(monkeypatch, tmp_path)
    audit = project_financial_dispatch_ledger(
        FinancialDispatchCheckpointLedger.model_validate(case.checkpoint),
        run_asset_configuration=case.asset,
        capability_routing_plan=case.plan,
        run_scope_id=case.context.run_id,
    )
    assert isinstance(audit, FinancialDispatchAuditProjectionV3)
    envelope = case.dispatch.to_tool_message().artifact

    manifest_audit = next(
        manifest
        for request in audit.manifest_requests
        for manifest in request.manifests
        if manifest.metadata_compatibility_assessments
    ).model_dump(
        mode="json",
    )
    manifest_audit.pop("metadata_compatibility_assessments")
    with pytest.raises(ValueError, match="binding|metadata"):
        FinancialManifestAuditProjection.model_validate(manifest_audit)

    completeness_row = next(
        row.model_dump(mode="json")
        for row in audit.completeness_rows
        if row.contract_version == "financial-completeness-row-v4"
    )
    completeness_row.pop("compatibility_state")
    with pytest.raises(ValueError, match="binding|metadata"):
        FinancialCompletenessAuditRow.model_validate(completeness_row)

    tool_manifest = copy.deepcopy(envelope["manifests"][0])
    tool_manifest.pop("indicator_metadata_compatibility_version")
    with pytest.raises(ValueError, match="binding|metadata"):
        FinancialToolManifestOperationalProjection.model_validate(tool_manifest)

    partial_envelope = copy.deepcopy(envelope)
    partial_envelope.pop("incompatibility_summary")
    with pytest.raises(ValueError, match="binding|incomplete"):
        FinancialToolMessageManifestEnvelope.model_validate(partial_envelope)


@pytest.mark.unit
def test_indicator_checkpoint_audit_report_tool_errors_and_repr_hide_canaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _mixed_context_bound_indicator_case(monkeypatch, tmp_path)
    ledger = FinancialDispatchCheckpointLedger.model_validate(case.checkpoint)
    audit = project_financial_dispatch_ledger(
        ledger,
        run_asset_configuration=case.asset,
        capability_routing_plan=case.plan,
        run_scope_id=case.context.run_id,
    )
    assert isinstance(audit, FinancialDispatchAuditProjectionV3)
    envelope = case.dispatch.to_tool_message().artifact
    checkpoint_projection = json.dumps(
        case.checkpoint,
        separators=(",", ":"),
        sort_keys=True,
    )
    operational_projection = json.dumps(
        {"audit": audit.model_dump(mode="json"), "tool": envelope},
        separators=(",", ":"),
        sort_keys=True,
    )
    safe_repr = repr((ledger, audit, case.dispatch))
    forbidden_names = (
        "token_digest",
        "raw_text",
        "provider_payload",
        "local_path",
    )
    leaked_projection_names = {
        name for name in forbidden_names if name in checkpoint_projection
        or name in operational_projection
    }
    leaked_repr_names = {name for name in forbidden_names if name in safe_repr}
    assert not leaked_projection_names, leaked_projection_names
    assert not leaked_repr_names, leaked_repr_names
    assert "raw_provider_value" not in operational_projection

    canaries = (
        "token=05b-secret-digest-canary",
        "ProviderRuntimeError: 05b raw payload canary",
        "https://provider.invalid/ratios?token=05b-canary",
        str((tmp_path / "private" / "payload.json").resolve()),
    )
    for canary in canaries:
        unsafe_envelope = copy.deepcopy(envelope)
        unsafe_envelope["tool_call_id"] = canary
        with pytest.raises(ValueError) as failure:
            FinancialToolMessageManifestEnvelope.model_validate(unsafe_envelope)
        assert canary not in str(failure.value)
        assert canary not in repr(failure.value)

    raw_text_envelope = copy.deepcopy(envelope)
    raw_text_envelope["raw_text"] = "opaque-provider-payload-canary"
    with pytest.raises(ValueError) as raw_text_failure:
        FinancialToolMessageManifestEnvelope.model_validate(raw_text_envelope)
    assert "opaque-provider-payload-canary" not in str(raw_text_failure.value)
    assert "opaque-provider-payload-canary" not in repr(raw_text_failure.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutation",
    (
        "assessment_identity",
        "assessment_state_without_identity",
        "field_semantics_version",
        "compatibility_version",
        "manifest_result",
        "selected_incompatible",
        "provider_artifact",
        "candidate",
        "context",
        "plan",
        "cache",
        "cross_run",
        "partial_projection",
    ),
)
def test_plan_1_2_checkpoint_cross_binding_mutations_fail_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    mutation: str,
) -> None:
    case = _context_bound_indicator_case(monkeypatch, tmp_path)
    checkpoint = copy.deepcopy(case.checkpoint)
    entry = checkpoint["qualified_indicator_selections"][0]
    routing_result = entry["terminal"]["routing_result"]
    manifest = routing_result["family_manifests"][0]

    if mutation == "assessment_identity":
        checkpoint["indicator_metadata_assessments"][0][
            "assessment_identity"
        ] = "financial-indicator-metadata-compatibility:v1:" + "f" * 64
    elif mutation == "assessment_state_without_identity":
        checkpoint["indicator_metadata_assessments"][0]["dimensions"][0][
            "state"
        ] = "unproven"
    elif mutation == "field_semantics_version":
        checkpoint["indicator_field_semantics_version"] = (
            "financial-indicator-field-semantics-v0"
        )
    elif mutation == "compatibility_version":
        checkpoint["indicator_metadata_compatibility_version"] = (
            "financial-indicator-metadata-compatibility-v0"
        )
    elif mutation == "manifest_result":
        manifest["manifest_identity"] = "financial-manifest:v3:" + "f" * 64
    elif mutation == "selected_incompatible":
        selected = manifest["provider_candidates"][0]
        selected["metadata_compatibility_state"] = "incompatible"
        selected["metadata_selection_disposition"] = "selected"
    elif mutation == "provider_artifact":
        checkpoint["indicator_provider_artifact_identities"][0] = (
            "financial-provider-artifact:v1:" + "f" * 64
        )
    elif mutation == "candidate":
        checkpoint["indicator_candidate_identities"][0] = (
            "financial-period-candidate:v1:" + "f" * 64
        )
    elif mutation == "context":
        context_identity = checkpoint["financial_eligibility_context"][
            "context_identity"
        ]
        checkpoint["financial_eligibility_context"]["context_identity"] = (
            context_identity.rsplit(":", 1)[0] + ":" + "f" * 64
        )
    elif mutation == "plan":
        checkpoint["capability_routing_plan_signature"] = (
            "mainland-routing-plan:v1:" + "f" * 64
        )
    elif mutation == "cache":
        cache_identity = checkpoint["provider_subrequest_cache"][
            "financial_eligibility_context_identity"
        ]
        checkpoint["provider_subrequest_cache"][
            "financial_eligibility_context_identity"
        ] = cache_identity.rsplit(":", 1)[0] + ":" + "f" * 64
    elif mutation == "cross_run":
        checkpoint["run_scope_id"] = "run:" + "f" * 64
    elif mutation == "partial_projection":
        checkpoint.pop("indicator_metadata_assessments")
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)

    provider_calls = 0

    def provider_must_not_run(_request):
        nonlocal provider_calls
        provider_calls += 1
        pytest.fail("invalid restore must not repair through provider fallback")

    restored_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(
            tmp_path / f"05b-invalid-{mutation}"
        ),
        run_scope_id=case.context.run_id,
    )
    with pytest.raises(FinancialDispatchCheckpointError) as failure:
        FinancialToolDispatcher(
            instrument_identity=case.asset.instrument_identity,
            run_asset_configuration=case.asset,
            provider_chains={"get_fundamentals": _provider_chain()},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=case.plan,
                statement_sources={},
                indicator_sources={"tushare": provider_must_not_run},
            ),
            provider_subrequest_cache=restored_cache,
            run_scope_id=case.context.run_id,
            financial_eligibility_context=case.context,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
            checkpoint_ledger=checkpoint,
        )
    assert failure.value.reason is (
        FinancialDispatchCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
    )
    assert provider_calls == 0
    assert case.provider_calls() == 1


@pytest.mark.unit
def test_plan_1_2_restore_rejects_reidentified_manifest_attempt_event_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    case = _context_bound_indicator_case(monkeypatch, tmp_path)
    original_manifest = case.result.routing_result.family_manifests[0]
    assert original_manifest.physical_attempt_events
    original_event = original_manifest.physical_attempt_events[0]
    mutated_event = original_event.model_copy(
        update={
            "attempted_at": original_event.attempted_at + timedelta(seconds=1),
        }
    )
    mutated_manifest = _reidentified_manifest(
        original_manifest,
        physical_attempt_events=(
            mutated_event,
            *original_manifest.physical_attempt_events[1:],
        ),
    )
    checkpoint = copy.deepcopy(case.checkpoint)
    entry = checkpoint["qualified_indicator_selections"][0]
    entry["terminal"]["routing_result"]["family_manifests"][0] = (
        mutated_manifest.model_dump(mode="json")
    )
    entry["terminal"]["manifest_identities"][0] = (
        mutated_manifest.manifest_identity
    )
    FinancialDispatchCheckpointLedger.model_validate(checkpoint)

    provider_calls = 0

    def provider_must_not_run(_request):
        nonlocal provider_calls
        provider_calls += 1
        pytest.fail("invalid attempt lineage must not invoke provider repair")

    restored_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(
            tmp_path / "05b-invalid-attempt-event"
        ),
        run_scope_id=case.context.run_id,
    )
    with pytest.raises(FinancialDispatchCheckpointError) as failure:
        FinancialToolDispatcher(
            instrument_identity=case.asset.instrument_identity,
            run_asset_configuration=case.asset,
            provider_chains={"get_fundamentals": _provider_chain()},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=case.plan,
                statement_sources={},
                indicator_sources={"tushare": provider_must_not_run},
            ),
            provider_subrequest_cache=restored_cache,
            run_scope_id=case.context.run_id,
            financial_eligibility_context=case.context,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
            checkpoint_ledger=checkpoint,
        )
    assert failure.value.reason is (
        FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
    )
    assert provider_calls == 0
    assert case.provider_calls() == 1


@pytest.mark.unit
def test_plan_1_2_factory_restore_preserves_indicator_state_with_zero_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config, asset, _context_plan, context = _context_fixture(monkeypatch, tmp_path)
    _fixture_request, response, plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="current_only",
        f_ann_date=None,
        as_of_date=context.as_of_date,
        instrument_identity=asset.instrument_identity,
    )
    response = response.model_copy(
        update={
            "subrequest_keys": (),
            "physical_attempt_ids": (),
            "physical_attempt_events": (),
        }
    )
    qualified_config = copy.deepcopy(config)
    qualified_config.update(
        {
            "tushare_enabled_capabilities": [
                "statements",
                "financial_indicators",
                "adjustment_factors",
                "name_events",
            ],
            "asset_configuration_signature": (
                asset.asset_configuration_signature
            ),
            "mainland_capability_routing_plan_signature": plan.plan_signature,
        }
    )
    blueprint = build_production_qualified_routing_composition_blueprint(
        plan=plan,
        asset_configuration=asset,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        config=qualified_config,
        validation_workload=True,
        disposition=(
            MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
        ),
        environment={"TUSHARE_TOKEN": "fixture-only"},
        dependency_availability=dict.fromkeys(
            ("akshare", "baostock", "tushare", "yfinance"),
            True,
        ),
        provider_source_coverage=dict.fromkeys(
            (
                "akshare",
                "akshare_sina",
                "baostock",
                "baostock_qualified_families",
                "tushare",
                "yfinance",
                "yfinance_derived",
            ),
            True,
        ),
        factory_contract_version=(
            PRODUCTION_QUALIFIED_ROUTING_FACTORY_CONTRACT_VERSION
        ),
    )
    rebuilt = build_financial_eligibility_acquisition_context(
        canonical_run_id=context.run_id,
        run_id=context.run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        authoritative_as_of_date=context.as_of_date,
        as_of_date=context.as_of_date,
        authoritative_availability_scope=context.availability_scope,
        availability_scope=context.availability_scope,
        acquisition_purpose=context.acquisition_purpose,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan=plan,
        routing_plan_signature=plan.plan_signature,
        company_type_resolution=context.company_type_resolution,
        company_type_evidence=context.company_type_evidence,
        listing_date=context.listing_date,
        listing_provenance=context.listing_provenance,
        currency=context.currency,
        consolidation_scope=context.consolidation_scope,
        acquisition_annual_period_ends=context.acquisition_annual_period_ends,
        acquisition_reporting_period_ends=(
            context.acquisition_reporting_period_ends
        ),
    )
    assert rebuilt.context is not None
    context = rebuilt.context
    requirements = FinancialIndicatorEvidenceRequirements.create(
        requested_families=(FinancialRatioFamily.PROFIT,),
        canonical_evidence_contract_id="canonical-evidence-contract.v1",
        canonical_evidence_contract_required_families=(FinancialRatioFamily.PROFIT,),
        capability_profile_id="equity.v1",
        capability_profile_required_families=(),
        strategy_rules_id="mainland-equity-strategy-rules.v1",
    )
    tool_request = FinancialToolRequest(
        tool_name="get_fundamentals",
        statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=FinancialReportingFrequency.NOT_APPLICABLE,
        as_of_date=context.as_of_date,
        material_arguments={"projection": "ratios"},
        tool_call_id="ticket-05b-fresh",
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
        indicator_request_arguments=FinancialIndicatorRequestArguments(
            requested_families=(FinancialRatioFamily.PROFIT,),
            family_evidence_requirements=requirements,
        ),
    )
    selection_request = build_financial_indicator_routing_request(
        tool_request,
        context,
    )
    fresh_provider_calls: list[str] = []

    def fresh_source(_request):
        fresh_provider_calls.append("tushare")
        return response

    fresh_cache = ProviderSubrequestCache(
        coordinator=object(),
        artifact_store=ProviderSubrequestArtifactStore(tmp_path / "05b-fresh-store"),
        run_scope_id=context.run_id,
    )
    fresh_dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_fundamentals": _provider_chain()},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={"tushare": fresh_source},
        ),
        provider_subrequest_cache=fresh_cache,
        run_scope_id=context.run_id,
        financial_eligibility_context=context,
        checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
    )
    original = fresh_dispatcher.dispatch_indicator_selection(
        tool_request,
        selection_request,
    )
    shadow_ledger = fresh_dispatcher.checkpoint_ledger()
    authoritative_ledger = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_fundamentals": _provider_chain()},
    ).checkpoint_ledger()
    checkpoint_state = {
        "run_id": context.run_id,
        "trade_date": context.as_of_date.isoformat(),
        "asset_configuration": asset.model_dump(mode="json"),
        "capability_routing_plan": plan.model_dump(mode="json"),
        "capability_routing_rollout": (
            MainlandCapabilityRoutingRunProjection.from_plan(plan).model_dump(
                mode="json"
            )
        ),
        "financial_eligibility_binding": (
            plan.financial_eligibility_binding.model_dump(mode="json")
        ),
        "evidence_state": EvidenceState(
            instrument_identity=asset.instrument_identity,
            financial_eligibility_binding=plan.financial_eligibility_binding,
        ).model_dump(mode="json"),
        "financial_dispatch_ledger": authoritative_ledger,
        "financial_dispatch_shadow_ledger": shadow_ledger,
    }
    checkpoint_bytes = json.dumps(
        checkpoint_state,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    restore_events: list[str] = []
    metadata_calls = 0
    company_type_resolver_calls = 0
    coordinator_calls = 0
    artifact_writes = 0
    baostock_lifecycle_calls = 0
    provider_calls = 0
    model_calls = 0
    restored_owner: FinancialEligibilityAcquisitionContextOwner | None = None

    class NeverAcquire:
        def acquire(self, *_args, **_kwargs):
            nonlocal metadata_calls
            metadata_calls += 1
            pytest.fail("restore must not reacquire financial metadata")

    class NeverPermit:
        def __getattr__(self, _name):
            nonlocal coordinator_calls
            coordinator_calls += 1
            pytest.fail("restore must not acquire a coordinator permit")

    class NeverWriteStore(ProviderSubrequestArtifactStore):
        def install(self, *_args, **_kwargs):
            nonlocal artifact_writes
            artifact_writes += 1
            pytest.fail("restore must not write provider artifacts")

    class NeverBaoStockLifecycle(_OwnedResource):
        def __getattr__(self, _name):
            nonlocal baostock_lifecycle_calls
            baostock_lifecycle_calls += 1
            pytest.fail("restore must not invoke BaoStock lifecycle operations")

    def provider_must_not_run(_request):
        nonlocal provider_calls
        provider_calls += 1
        pytest.fail("restore must not invoke an indicator provider")

    def company_type_resolver_must_not_run(*_args, **_kwargs):
        nonlocal company_type_resolver_calls
        company_type_resolver_calls += 1
        pytest.fail("restore must not resolve company type")

    def model_must_not_run(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        pytest.fail("restore must not construct or call a model")

    monkeypatch.setattr(
        "tradingagents.dataflows.financial_eligibility_metadata.resolve_financial_company_type",
        company_type_resolver_must_not_run,
    )
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        model_must_not_run,
    )

    def build_cache(_coordinator, artifact_store, run_id, _stop_signal):
        restore_events.append("create:cache")
        return ProviderSubrequestCache(
            coordinator=NeverPermit(),
            artifact_store=artifact_store,
            run_scope_id=run_id,
        )

    def assemble_runtime(**kwargs):
        restore_events.append("phase:provider_adapters")
        restored_router = MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources=dict.fromkeys(
                plan.route_for(MainlandCapability.FINANCIAL_INDICATORS),
                provider_must_not_run,
            ),
        )
        return ProductionQualifiedRoutingRuntimeAssembly(
            financial_routing=QualifiedFinancialRoutingComposition(
                router=restored_router,
                statement_request_factory=build_financial_statement_routing_request,
                indicator_request_factory=build_financial_indicator_routing_request,
            ),
            dependencies=SimpleNamespace(
                subrequest_cache=kwargs["subrequest_cache"]
            ),
            eligibility_metadata_acquirer=NeverAcquire(),
        )

    factories = ProductionQualifiedRoutingRuntimeFactories(
        open_coordinator_store=lambda _blueprint: _OwnedResource(
            "coordinator_store", restore_events
        ),
        build_coordinator=lambda _store, _stop: _OwnedResource(
            "coordinator", restore_events
        ),
        build_artifact_store=lambda _blueprint: NeverWriteStore(
            tmp_path / "05b-restored-store"
        ),
        build_subrequest_cache=build_cache,
        build_tushare_client=lambda _blueprint, _stop: _OwnedResource(
            "tushare", restore_events
        ),
        build_akshare_facade=lambda _blueprint, _stop: _OwnedResource(
            "akshare", restore_events
        ),
        build_yahoo_transport=lambda _blueprint, _stop: _OwnedResource(
            "yfinance", restore_events
        ),
        build_baostock_transport=lambda _blueprint, _stop: NeverBaoStockLifecycle(
            "baostock", restore_events
        ),
        assemble_runtime=assemble_runtime,
        phase_hook=lambda phase, _value: restore_events.append(f"phase:{phase}"),
    )

    def restore_dispatcher(
        state,
        *,
        provider_subrequest_cache,
        routing_plan,
    ):
        restore_events.append("restore:dispatcher")
        return FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_fundamentals": _provider_chain()},
            checkpoint_ledger=state["financial_dispatch_shadow_ledger"],
            provider_subrequest_cache=provider_subrequest_cache,
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=routing_plan,
                statement_sources={},
            ),
            run_scope_id=context.run_id,
        )

    def restore_context(restored_context):
        nonlocal restored_owner
        restore_events.append("restore:context")
        restored_owner = FinancialEligibilityAcquisitionContextOwner.from_restored_context(
            run_id=context.run_id,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
            asset_configuration=asset,
            routing_plan=plan,
            context=restored_context,
            expected_as_of_date=context.as_of_date,
            availability_scope=context.availability_scope,
            consolidation_scope=context.consolidation_scope,
            interrupt_runtime=composition.interrupt,
            close_runtime=composition.close,
        )
        return restored_owner

    composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=factories,
    )
    runtime = composition.bind_run(
        ProductionQualifiedRoutingRunBinding(
            run_id=context.run_id,
            asset_configuration=asset,
            routing_plan=plan,
            checkpoint_compatibility_known=True,
            resume_from_checkpoint=True,
            checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
            checkpoint_state=MappingProxyType(checkpoint_state),
            restore_financial_dispatcher=restore_dispatcher,
            restore_financial_eligibility_context=restore_context,
        )
    )

    assert runtime.financial_routing is None
    assert restored_owner is not None
    restored_dispatcher = (
        restored_owner.financial_routing.restored_shadow_dispatcher
    )
    assert restored_dispatcher is not None
    repeated = restored_dispatcher.dispatch_indicator_selection(
        tool_request.model_copy(update={"tool_call_id": "ticket-05b-restored"}),
        selection_request,
    )
    assert repeated.disposition == "duplicate_suppressed"
    assert repeated.routing_result == original.routing_result
    assert repeated.routing_result.metadata_compatibility_assessments == (
        original.routing_result.metadata_compatibility_assessments
    )
    assert repeated.routing_result.metadata_incompatible_rejections == (
        original.routing_result.metadata_incompatible_rejections
    )
    assert fresh_provider_calls == ["tushare"]
    assert metadata_calls == 0
    assert company_type_resolver_calls == 0
    assert coordinator_calls == 0
    assert artifact_writes == 0
    assert baostock_lifecycle_calls == 0
    assert provider_calls == 0
    assert model_calls == 0
    assert restore_events.index("restore:context") < restore_events.index(
        "create:cache"
    )
    assert restore_events.index("restore:dispatcher") < restore_events.index(
        "phase:provider_adapters"
    )
    assert json.dumps(
        checkpoint_state,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() == checkpoint_bytes
    composition.close()

    old_checkpoint_state = copy.deepcopy(checkpoint_state)
    old_shadow_ledger = old_checkpoint_state["financial_dispatch_shadow_ledger"]
    old_shadow_ledger["contract_version"] = "1.4"
    for field in (
        "capability_routing_plan_version",
        "financial_indicator_manifest_version",
        "indicator_field_semantics_version",
        "indicator_metadata_compatibility_version",
        "indicator_metadata_assessments",
        "indicator_metadata_rejections",
        "indicator_provider_artifact_identities",
        "indicator_candidate_identities",
        "indicator_provider_topology",
    ):
        old_shadow_ledger.pop(field, None)
    old_checkpoint_bytes = json.dumps(
        old_checkpoint_state,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    restore_events.clear()
    old_composition = build_registered_production_qualified_routing_composition(
        blueprint=blueprint,
        runtime_factories=factories,
    )
    with pytest.raises(FinancialDispatchCheckpointError) as old_error:
        old_composition.bind_run(
            ProductionQualifiedRoutingRunBinding(
                run_id=context.run_id,
                asset_configuration=asset,
                routing_plan=plan,
                checkpoint_compatibility_known=True,
                resume_from_checkpoint=True,
                checkpoint_thread_id=_CHECKPOINT_THREAD_ID,
                checkpoint_state=MappingProxyType(old_checkpoint_state),
                restore_financial_dispatcher=restore_dispatcher,
                restore_financial_eligibility_context=restore_context,
            )
        )
    assert old_error.value.reason is (
        FinancialDispatchCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
    )
    assert restore_events == []
    assert fresh_provider_calls == ["tushare"]
    assert json.dumps(
        old_checkpoint_state,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() == old_checkpoint_bytes
    old_composition.close()

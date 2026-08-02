from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256

import pytest

from tests.test_financial_capability_routing_selection import _shadow_plan
from tests.test_financial_manifest_evidence_reporting import _attempt_event
from tests.test_financial_period_contracts import (
    _candidate as _statement_candidate,
    _declared_statement_fields,
    _instrument as _statement_instrument,
)
from tests.test_financial_period_targets import _REPORTING_TARGETS
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialIndicatorRoutingResult,
    FinancialProviderPeriodResponse,
    MainlandFinancialCapabilityRouter,
    assess_financial_indicator_family_outcomes,
    render_indicator_routing_result,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_V1,
    FINANCIAL_RATIO_DECLARATION_V2,
    FinancialAcquisitionManifest,
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialCriticalValueConflict,
    FinancialEvidenceDisposition,
    FinancialFamilyRequirementAuthority,
    FinancialFamilyRequirementStatus,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialIndicatorEvidenceRequirements,
    FinancialMaterialPremiseRequirement,
    FinancialPeriodCandidate,
    FinancialPeriodDisposition,
    FinancialPeriodIdentity,
    FinancialPeriodRejectionReason,
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    assess_financial_period_candidate,
    financial_ratio_field_declaration,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialIndicatorSelectionDispatchResult,
    FinancialProvider,
    FinancialProviderVariant,
    FinancialToolDispatcher,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
    project_financial_selection_evidence,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.reporting import _render_financial_dispatch_operations

_AS_OF = date(2026, 8, 1)
_POLICY_VERSION = "mainland-financial-period-eligibility-v2"
_ALL_FAMILIES = tuple(FinancialRatioFamily)
_OBSERVED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _identity(symbol: str):
    asset = resolve_run_asset_configuration(
        symbol,
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    return asset.instrument_identity


def _requirements(
    families: tuple[FinancialRatioFamily, ...],
    *,
    evidence_required: tuple[FinancialRatioFamily, ...] = (),
    profile_required: tuple[FinancialRatioFamily, ...] = (),
    strategy_required: tuple[FinancialRatioFamily, ...] = (),
    premises: tuple[FinancialMaterialPremiseRequirement, ...] = (),
) -> FinancialIndicatorEvidenceRequirements:
    return FinancialIndicatorEvidenceRequirements.create(
        requested_families=families,
        canonical_evidence_contract_id="canonical-evidence-contract.v1",
        canonical_evidence_contract_required_families=evidence_required,
        capability_profile_id="equity.v1",
        capability_profile_required_families=profile_required,
        strategy_rules_id="mainland-equity-strategy-rules.v1",
        strategy_rules_required_families=strategy_required,
        material_premises=premises,
    )


def _candidate(
    *,
    symbol: str,
    company_type: FinancialCompanyType,
    family: FinancialRatioFamily,
    period: date,
    core_field_limit: int | None = None,
    f_ann_date: date | None = None,
    value: str = "1",
    declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
) -> FinancialPeriodCandidate:
    identity = _identity(symbol)
    declaration = financial_ratio_field_declaration(
        company_type,
        family,
        declaration_version=declaration_version,
    )
    selected_fields = declaration.normalized_fields[:core_field_limit]
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id="tushare",
        endpoint_id="financial_indicators",
        dataset_id="single_stock_history",
        schema_identity=(
            f"ticket-04-{company_type.value.replace('/', '-')}-{family.value}-"
            f"{declaration_version}"
        ),
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": symbol,
            "period_end": period.isoformat(),
            "ratio_family": family.value,
        },
        provider_metadata={"ann_date": "20260701", "comp_type": "2" if company_type is FinancialCompanyType.BANK else "1"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload={
            "family": family.value,
            "period": period.isoformat(),
            "field_count": len(selected_fields),
            "value": value,
        },
    )
    fields = tuple(
        FinancialFieldValue(
            provider_field=field,
            original_value=value,
            original_unit=declaration.normalized_unit,
            normalized_field=field,
            normalized_value=value,
            normalized_unit=declaration.normalized_unit,
        )
        for field in selected_fields
    )
    resolution = resolve_financial_company_type(
        provider_declared_type=company_type,
        provider_declaration_qualified=True,
        classifier_metadata={},
        present_fields=selected_fields,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=identity,
            capability=FinancialCapability.RATIO_FAMILY,
            statement_type=None,
            ratio_family=family,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=company_type,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="XXX",
            provider_revision_binding=artifact.artifact_identity,
        ),
        filing_metadata=FinancialFilingMetadata(
            ann_date=date(2026, 7, 1),
            f_ann_date=f_ann_date,
            report_type="1",
            comp_type="2" if company_type is FinancialCompanyType.BANK else "1",
            update_flag="0",
            retrieved_at=_OBSERVED_AT,
            observed_at=_OBSERVED_AT,
            local_provider_revision_identity=artifact.artifact_identity,
        ),
        company_type_resolution=resolution,
        fields=fields,
    )


def _route(
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol: str,
    company_type: FinancialCompanyType,
    families: tuple[FinancialRatioFamily, ...],
    sparse_families: tuple[FinancialRatioFamily, ...] = (),
    empty_families: tuple[FinancialRatioFamily, ...] = (),
    requested_analysis_scope: str = "current_only",
    requirements: FinancialIndicatorEvidenceRequirements | None = None,
    reverse_rows: bool = False,
) -> tuple[FinancialIndicatorRoutingRequest, FinancialIndicatorRoutingResult, object]:
    candidates = []
    for family in families:
        if family in empty_families:
            continue
        declaration = financial_ratio_field_declaration(
            company_type,
            family,
            declaration_version=FINANCIAL_RATIO_DECLARATION_V2,
        )
        failing_limit = max(0, len(declaration.normalized_fields) - 1)
        for period in _REPORTING_TARGETS:
            candidates.append(
                _candidate(
                    symbol=symbol,
                    company_type=company_type,
                    family=family,
                    period=period,
                    core_field_limit=(
                        failing_limit
                        if family in sparse_families and period == _REPORTING_TARGETS[0]
                        else None
                    ),
                )
            )
    publication_family = next(
        family for family in FinancialRatioFamily if family in families
    )
    for year in (2021, 2022, 2023):
        candidates.append(
            _candidate(
                symbol=symbol,
                company_type=company_type,
                family=publication_family,
                period=date(year, 12, 31),
            )
        )
    if reverse_rows:
        candidates.reverse()
    assert candidates
    event = _attempt_event(ordinal=4)
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=candidates[0].artifact,
        retained_artifacts=tuple(candidate.artifact for candidate in candidates),
        candidates=tuple(candidates),
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    request = FinancialIndicatorRoutingRequest(
        contract_version="financial-indicator-routing-request-v4",
        eligibility_policy_version=_POLICY_VERSION,
        requested_analysis_scope=requested_analysis_scope,
        instrument_identity=_identity(symbol),
        as_of_date=_AS_OF,
        company_type=company_type,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        ratio_families=families,
        eligible_reporting_period_ends=(date(2026, 6, 30), *_REPORTING_TARGETS),
        family_evidence_requirements=requirements or _requirements(families),
    )
    plan = _shadow_plan(monkeypatch)
    result = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        indicator_sources={"tushare": lambda _request: response},
    ).route_indicators(request)
    return request, result, plan


def _family(result: FinancialIndicatorRoutingResult, family: FinancialRatioFamily):
    return next(item for item in result.family_assessments if item.family is family)


def test_requirement_contract_uses_all_authorities_and_premises_only_add_requirements() -> None:
    premise = FinancialMaterialPremiseRequirement(
        premise_id="premise.bank-profit-trend",
        required_families=(FinancialRatioFamily.PROFIT,),
    )
    requirements = _requirements(
        (FinancialRatioFamily.PROFIT, FinancialRatioFamily.GROWTH),
        profile_required=(FinancialRatioFamily.GROWTH,),
        premises=(premise,),
    )

    profit, growth = requirements.families
    assert profit.status is FinancialFamilyRequirementStatus.REQUIRED
    assert profit.material_premise_ids == (premise.premise_id,)
    assert growth.status is FinancialFamilyRequirementStatus.REQUIRED
    assert {binding.authority for binding in growth.authority_bindings} == set(
        FinancialFamilyRequirementAuthority
    )
    assert next(
        binding for binding in growth.authority_bindings
        if binding.authority is FinancialFamilyRequirementAuthority.CAPABILITY_PROFILE
    ).required

    payload = requirements.model_dump(mode="json")
    payload["families"][0]["status"] = "optional"
    with pytest.raises(ValueError, match="derived from every authority and premise"):
        FinancialIndicatorEvidenceRequirements.model_validate(payload)


def test_bank_growth_and_cash_flow_survive_sparse_siblings_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sparse = (
        FinancialRatioFamily.PROFIT,
        FinancialRatioFamily.OPERATION,
        FinancialRatioFamily.BALANCE,
        FinancialRatioFamily.DUPONT,
    )
    _, result, _ = _route(
        monkeypatch,
        symbol="601328.SS",
        company_type=FinancialCompanyType.BANK,
        families=_ALL_FAMILIES,
        sparse_families=sparse,
    )

    assert result.complete_families == (
        FinancialRatioFamily.GROWTH,
        FinancialRatioFamily.CASH_FLOW,
    )
    assert result.aggregate_family_assessment.disposition is (
        FinancialEvidenceDisposition.DEGRADED
    )
    for family in (FinancialRatioFamily.GROWTH, FinancialRatioFamily.CASH_FLOW):
        assessment = _family(result, family)
        assert assessment.complete
        assert assessment.selected_periods == _REPORTING_TARGETS
        assert assessment.current_only_periods == _REPORTING_TARGETS
        assert not assessment.missing_periods
    for family in sparse:
        assessment = _family(result, family)
        assert not assessment.complete
        assert assessment.missing_periods == (_REPORTING_TARGETS[0],)
        assert assessment.rejected_periods == (_REPORTING_TARGETS[0],)

    failed_profit_period = next(
        item
        for item in _family(result, FinancialRatioFamily.PROFIT).coverage
        if item.period == _REPORTING_TARGETS[0]
    )
    surviving_growth_period = next(
        item
        for item in _family(result, FinancialRatioFamily.GROWTH).coverage
        if item.period == _REPORTING_TARGETS[0]
    )
    assert failed_profit_period.critical_threshold == Decimal("1")
    assert failed_profit_period.core_threshold == Decimal("0.9")
    assert failed_profit_period.core_coverage < Decimal("0.9")
    assert surviving_growth_period.core_coverage >= Decimal("0.9")

    artifact_sets = {
        manifest.requested_ratio_family: {
            item.artifact.artifact_identity for item in manifest.artifacts
        }
        for manifest in result.family_manifests
    }
    assert all(artifact_sets.values())
    assert artifact_sets[FinancialRatioFamily.GROWTH].isdisjoint(
        artifact_sets[FinancialRatioFamily.PROFIT]
    )


def test_complete_non_bank_families_remain_selected_and_current_only_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result, _ = _route(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        families=_ALL_FAMILIES,
    )

    assert result.complete_families == _ALL_FAMILIES
    assert result.aggregate_family_assessment.disposition is (
        FinancialEvidenceDisposition.DEGRADED
    )
    assert result.aggregate_family_assessment.typed_reasons == (
        "current_only_lineage",
    )
    assert all(item.complete for item in result.family_assessments)
    assert all(
        item.disposition is FinancialEvidenceDisposition.DEGRADED
        and item.current_only_periods == _REPORTING_TARGETS
        for item in result.family_assessments
    )


@pytest.mark.parametrize("authority", ["evidence", "profile", "strategy", "premise"])
def test_required_or_material_sparse_family_is_insufficient_and_never_hold(
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    families = (FinancialRatioFamily.PROFIT, FinancialRatioFamily.GROWTH)
    kwargs: dict[str, object] = {}
    if authority == "evidence":
        kwargs["evidence_required"] = (FinancialRatioFamily.PROFIT,)
    elif authority == "profile":
        kwargs["profile_required"] = (FinancialRatioFamily.PROFIT,)
    elif authority == "strategy":
        kwargs["strategy_required"] = (FinancialRatioFamily.PROFIT,)
    else:
        kwargs["premises"] = (
            FinancialMaterialPremiseRequirement(
                premise_id="premise.profit-direction",
                required_families=(FinancialRatioFamily.PROFIT,),
            ),
        )
    requirements = _requirements(families, **kwargs)
    _, result, _ = _route(
        monkeypatch,
        symbol="601328.SS",
        company_type=FinancialCompanyType.BANK,
        families=families,
        sparse_families=(FinancialRatioFamily.PROFIT,),
        requirements=requirements,
    )

    aggregate = result.aggregate_family_assessment
    assert aggregate.disposition is FinancialEvidenceDisposition.INSUFFICIENT
    assert _family(result, FinancialRatioFamily.GROWTH).complete
    assert not _family(result, FinancialRatioFamily.PROFIT).complete
    if authority == "premise":
        assert aggregate.missing_material_premise_bindings[0].premise_id == (
            "premise.profit-direction"
        )
    rendered = render_indicator_routing_result(result)
    assert json.loads(rendered)["status"] == "insufficient_evidence"
    lowered = rendered.casefold()
    for directional_field in ('"rating"', '"signal"', '"position"', '"hold"'):
        assert directional_field not in lowered


def test_optional_sparse_family_with_complete_material_family_may_be_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    families = (FinancialRatioFamily.PROFIT, FinancialRatioFamily.GROWTH)
    requirements = _requirements(
        families,
        premises=(
            FinancialMaterialPremiseRequirement(
                premise_id="premise.growth-direction",
                required_families=(FinancialRatioFamily.GROWTH,),
            ),
        ),
    )
    _, result, _ = _route(
        monkeypatch,
        symbol="601328.SS",
        company_type=FinancialCompanyType.BANK,
        families=families,
        sparse_families=(FinancialRatioFamily.PROFIT,),
        requirements=requirements,
    )

    aggregate = result.aggregate_family_assessment
    assert aggregate.disposition is FinancialEvidenceDisposition.DEGRADED
    assert not aggregate.missing_material_premise_bindings
    assert "optional_family_incomplete" in aggregate.typed_reasons


def test_no_complete_family_and_strict_current_only_candidates_are_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    families = (FinancialRatioFamily.PROFIT,)
    _, sparse_result, _ = _route(
        monkeypatch,
        symbol="601328.SS",
        company_type=FinancialCompanyType.BANK,
        families=families,
        sparse_families=families,
    )
    assert sparse_result.aggregate_family_assessment.disposition is (
        FinancialEvidenceDisposition.INSUFFICIENT
    )
    assert "no_complete_family" in (
        sparse_result.aggregate_family_assessment.typed_reasons
    )

    _, strict_result, _ = _route(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        families=families,
        requested_analysis_scope="strict_pit_eligible",
    )
    assert strict_result.aggregate_family_assessment.disposition is (
        FinancialEvidenceDisposition.INSUFFICIENT
    )
    assert "strict_pit_current_only_candidates" in (
        strict_result.aggregate_family_assessment.typed_reasons
    )
    assert not strict_result.selected_candidates


def test_period_local_conflict_blocks_only_bound_premise_and_preserves_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    families = (FinancialRatioFamily.PROFIT, FinancialRatioFamily.GROWTH)
    request, result, _ = _route(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        families=families,
        requirements=_requirements(
            families,
            premises=(
                FinancialMaterialPremiseRequirement(
                    premise_id="premise.profit-conflicted",
                    required_families=(FinancialRatioFamily.PROFIT,),
                ),
            ),
        ),
    )
    profit_manifest = next(
        manifest for manifest in result.family_manifests
        if manifest.requested_ratio_family is FinancialRatioFamily.PROFIT
    )
    selected = profit_manifest.provider_candidates[0]
    duplicate = _candidate(
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        family=FinancialRatioFamily.PROFIT,
        period=selected.candidate.period_identity.period_end,
        value="2",
    )
    duplicate_assessment = selected.assessment.model_copy(
        update={
            "candidate_identity": duplicate.candidate_identity,
            "artifact_identity": duplicate.artifact.artifact_identity,
            "period_identity": duplicate.period_identity.period_identity,
        }
    )
    provider_candidates = (
        *profit_manifest.provider_candidates,
        type(selected)(candidate=duplicate, assessment=duplicate_assessment),
    )
    conflict = FinancialCriticalValueConflict.create(
        selected_candidate_identity=selected.candidate.candidate_identity,
        conflicting_candidate_identity=duplicate.candidate_identity,
        normalized_field="return_on_equity",
        selected_value=1,
        conflicting_value=2,
    )
    conflicted_manifest = FinancialAcquisitionManifest.create(
        capability_routing_plan_signature=(
            profit_manifest.capability_routing_plan_signature
        ),
        instrument_identity=profit_manifest.instrument_identity,
        requested_capability=profit_manifest.requested_capability,
        requested_statement_type=None,
        requested_ratio_family=FinancialRatioFamily.PROFIT,
        artifacts=(
            *profit_manifest.artifacts,
            type(profit_manifest.artifacts[0])(artifact=duplicate.artifact),
        ),
        acquisition_outcomes=profit_manifest.acquisition_outcomes,
        provider_candidates=provider_candidates,
        selections=profit_manifest.selections,
        rejected_periods=profit_manifest.rejected_periods,
        overlaps=profit_manifest.overlaps,
        conflicts=(conflict,),
        aggregate_completeness=profit_manifest.aggregate_completeness,
    )
    manifests = tuple(
        conflicted_manifest
        if manifest.requested_ratio_family is FinancialRatioFamily.PROFIT
        else manifest.model_copy(
            update={
                "contract_version": "financial-manifest-v1",
                "physical_attempt_events": (),
                "disposition": None,
                "final_rendered_artifact": None,
                "publication_target_construction": None,
                "requested_analysis_scope": None,
                "availability_scope": None,
                "availability_basis": None,
                "eligibility_policy_version": None,
                "availability_reason": None,
                "manifest_identity": manifest.manifest_identity.replace(
                    "financial-manifest:v2:", "financial-manifest:v1:"
                ),
            }
        )
        for manifest in result.family_manifests
    )
    family_assessments, aggregate = assess_financial_indicator_family_outcomes(
        request=request,
        family_manifests=manifests,
        publication_target_construction=result.publication_target_construction,
    )

    profit = next(item for item in family_assessments if item.family is FinancialRatioFamily.PROFIT)
    growth = next(item for item in family_assessments if item.family is FinancialRatioFamily.GROWTH)
    assert profit.conflicted_periods == (selected.candidate.period_identity.period_end,)
    assert profit.disposition is FinancialEvidenceDisposition.CONFLICTED
    assert growth.complete
    assert growth.selected_periods == _REPORTING_TARGETS
    assert aggregate.disposition is FinancialEvidenceDisposition.CONFLICTED
    assert aggregate.conflicted_material_premise_bindings[0].premise_id == (
        "premise.profit-conflicted"
    )


def test_family_and_provider_order_cannot_change_assessments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    families = (FinancialRatioFamily.PROFIT, FinancialRatioFamily.GROWTH)
    _, first, _ = _route(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        families=families,
    )
    _, shuffled, _ = _route(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        families=families,
        reverse_rows=True,
    )
    _, family_shuffled, _ = _route(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        families=tuple(reversed(families)),
    )

    assert first.aggregate_family_assessment.model_dump(mode="json") == (
        shuffled.aggregate_family_assessment.model_dump(mode="json")
    )
    assert [item.model_dump(mode="json") for item in first.family_assessments] == [
        item.model_dump(mode="json") for item in shuffled.family_assessments
    ]
    assert first.aggregate_family_assessment.model_dump(mode="json") == (
        family_shuffled.aggregate_family_assessment.model_dump(mode="json")
    )
    assert [item.model_dump(mode="json") for item in first.family_assessments] == [
        item.model_dump(mode="json")
        for item in family_shuffled.family_assessments
    ]


def test_checkpoint_audit_report_tool_and_evidence_preserve_same_family_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = "601328.SS"
    asset = resolve_run_asset_configuration(
        symbol,
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    families = (FinancialRatioFamily.PROFIT, FinancialRatioFamily.GROWTH)
    requirements = _requirements(families)
    request, routed, plan = _route(
        monkeypatch,
        symbol=symbol,
        company_type=FinancialCompanyType.BANK,
        families=families,
        sparse_families=(FinancialRatioFamily.PROFIT,),
        requirements=requirements,
    )
    response_candidates = tuple(
        item.candidate
        for manifest in routed.family_manifests
        for item in manifest.provider_candidates
    )
    event = _attempt_event(ordinal=4)
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=response_candidates[0].artifact,
        retained_artifacts=tuple(
            candidate.artifact for candidate in response_candidates
        ),
        candidates=response_candidates,
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_fundamentals": (
                FinancialProvider(
                    name="unused",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="unused",
                            invoke=lambda _request: "unused",
                        ),
                    ),
                ),
            ),
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={"tushare": lambda _request: response},
        ),
        run_scope_id="ticket-04-family-projections",
    )
    dispatch = dispatcher.dispatch_indicator_selection(
        FinancialToolRequest(
            tool_name="get_fundamentals",
            statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
            frequency=FinancialReportingFrequency.NOT_APPLICABLE,
            as_of_date=_AS_OF,
            tool_call_id="ticket-04-family-projections",
        ),
        request,
    )
    checkpoint = dispatcher.checkpoint_ledger()
    audit = project_financial_dispatch_ledger(
        checkpoint,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-04-family-projections",
    )
    assert audit is not None
    evidence = project_financial_selection_evidence(EvidenceState(), dispatch)

    expected = dispatch.routing_result.aggregate_family_assessment.model_dump(
        mode="json"
    )
    checkpoint_result = checkpoint["qualified_indicator_selections"][0]["terminal"][
        "routing_result"
    ]
    tool_message = dispatch.to_tool_message()
    tool_envelope = tool_message.artifact
    audit_request = audit.manifest_requests[0]
    assert checkpoint_result["aggregate_family_assessment"] == expected
    assert tool_envelope["aggregate_family_assessment"] == expected
    assert tool_message.status == "success"
    assert audit_request.aggregate_family_assessment.model_dump(mode="json") == expected
    assert [
        item.model_dump(mode="json") for item in evidence.financial_family_assessments
    ] == [
        item.model_dump(mode="json")
        for item in dispatch.routing_result.family_assessments
    ]
    assert [
        item.model_dump(mode="json")
        for item in evidence.financial_family_aggregate_assessments
    ] == [expected]
    forged_evidence = copy.deepcopy(evidence.model_dump(mode="json"))
    forged_evidence["financial_family_assessments"][0]["disposition"] = (
        "strict_pit_eligible"
    )
    with pytest.raises(ValueError, match="disposition is inconsistent"):
        EvidenceState.model_validate(forged_evidence)

    report = "\n".join(_render_financial_dispatch_operations(audit))
    for expected_text in (
        "Family-level financial evidence",
        "profit",
        "growth",
        "optional",
        "degraded",
        _POLICY_VERSION,
    ):
        assert expected_text in report
    bounded = (
        json.dumps(checkpoint_result, sort_keys=True).casefold()
        + json.dumps(tool_envelope, sort_keys=True).casefold()
        + json.dumps(evidence.model_dump(mode="json"), sort_keys=True).casefold()
        + report.casefold()
    )
    for forbidden in (
        "payload_body",
        "raw provider error",
        "traceback",
        "credential=fixture",
        "https://provider.invalid/query?secret=value",
    ):
        assert forbidden not in bounded

    required_request, required_routed, _ = _route(
        monkeypatch,
        symbol=symbol,
        company_type=FinancialCompanyType.BANK,
        families=families,
        sparse_families=(FinancialRatioFamily.PROFIT,),
        requirements=_requirements(
            families,
            evidence_required=(FinancialRatioFamily.PROFIT,),
        ),
    )
    insufficient_dispatch = FinancialIndicatorSelectionDispatchResult(
        tool_call_id="ticket-04-insufficient-outcome",
        disposition="executed",
        request_key=dispatch.request_key,
        selection_request=required_request,
        routing_result=required_routed,
        rendered_value=render_indicator_routing_result(required_routed),
    )
    insufficient_tool_message = insufficient_dispatch.to_tool_message()
    assert json.loads(insufficient_tool_message.content)["status"] == (
        "insufficient_evidence"
    )
    assert insufficient_tool_message.status == "success"


def test_threshold_boundaries_use_fixed_declarations_not_populated_fields() -> None:
    def assess_ratio(core_field_limit: int):
        candidate = _candidate(
            symbol="600895.SS",
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            family=FinancialRatioFamily.PROFIT,
            period=date(2025, 12, 31),
            core_field_limit=core_field_limit,
            declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
        )
        return assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.RATIO_FAMILY,
            requested_statement_type=None,
            requested_ratio_family=FinancialRatioFamily.PROFIT,
            expected_instrument_identity=candidate.period_identity.instrument_identity,
            expected_frequency=FinancialReportingFrequency.QUARTERLY,
            expected_currency="XXX",
            expected_consolidation_scope=(
                FinancialConsolidationScope.CONSOLIDATED
            ),
            pit_as_of_date=_AS_OF,
            ratio_declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
        )

    below = assess_ratio(8)
    at_threshold = assess_ratio(9)
    above = assess_ratio(10)
    assert len(below.declared_core_fields) == 10
    assert len(below.present_core_fields) == 8
    assert below.core_coverage == Decimal("0.8")
    assert below.disposition is FinancialPeriodDisposition.REJECTED
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        below.rejection_reasons
    )
    assert at_threshold.core_coverage == Decimal("0.9")
    assert at_threshold.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert above.core_coverage == Decimal("1")
    assert above.disposition is FinancialPeriodDisposition.CURRENT_ONLY

    statement_fields = _declared_statement_fields(
        FinancialCompanyType.INDUSTRIAL_NON_BANK
    )
    missing_critical = tuple(
        field
        for field in statement_fields
        if field.normalized_field != "total_assets"
    )
    critical_assessment = assess_financial_period_candidate(
        _statement_candidate(
            FinancialCompanyType.INDUSTRIAL_NON_BANK,
            missing_critical,
        ),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_statement_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=_AS_OF,
    )
    assert critical_assessment.critical_coverage < Decimal("1")
    assert critical_assessment.disposition is FinancialPeriodDisposition.REJECTED
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        critical_assessment.rejection_reasons
    )


def test_family_requirement_inputs_are_part_of_deterministic_assessment_identity() -> None:
    optional = _requirements((FinancialRatioFamily.PROFIT,))
    required = _requirements(
        (FinancialRatioFamily.PROFIT,),
        evidence_required=(FinancialRatioFamily.PROFIT,),
    )
    optional_digest = sha256(
        json.dumps(optional.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    required_digest = sha256(
        json.dumps(required.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    assert optional_digest != required_digest

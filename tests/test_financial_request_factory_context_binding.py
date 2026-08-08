from __future__ import annotations

import copy
import importlib.util
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from hashlib import sha256
from threading import Event

import pytest
from pydantic import ValidationError

from tradingagents.asset_configuration import (
    RunAssetConfiguration,
    resolve_run_asset_configuration,
)
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_capability_routing import (
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialIndicatorEvidenceRequirements,
    FinancialListingProvenance,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialIndicatorRequestArguments,
    FinancialProvider,
    FinancialProviderVariant,
    FinancialToolDispatcher,
    FinancialToolRequest,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FinancialCompanyTypeAcquisitionEvidenceBinding,
    FinancialCompanyTypeEvidenceProjection,
    FinancialEligibilityAcquisitionContext,
    FinancialEligibilityMetadataBinding,
    FinancialEligibilityRequestBinding,
    FinancialListingMetadata,
    FinancialListingMetadataProjection,
    build_financial_eligibility_acquisition_context,
    derive_mainland_financial_acquisition_period_supersets,
)
from tradingagents.dataflows.financial_eligibility_metadata import (
    FinancialEligibilityMetadataAcquirer,
    FinancialEligibilityMetadataAcquisitionRequest,
)
from tradingagents.dataflows.financial_request_factory import (
    FinancialRequestFactoryError,
    FinancialRequestFactoryFailureReason,
    build_financial_indicator_routing_request,
    build_financial_statement_routing_request,
)
from tradingagents.default_config import DEFAULT_CONFIG

_AS_OF = date(2026, 8, 8)
_OBSERVED_AT = datetime(2026, 8, 8, 1, 2, tzinfo=timezone.utc)
_RETRIEVED_AT = datetime(2026, 8, 8, 1, 3, tzinfo=timezone.utc)


def _run_id(label: str) -> str:
    return f"run:{sha256(label.encode()).hexdigest()}"


def _qualified_plan(
    monkeypatch: pytest.MonkeyPatch,
    asset: RunAssetConfiguration,
) -> MainlandCapabilityRoutingPlan:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": (
                "statements",
                "financial_indicators",
            ),
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    result = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert result.plan is not None
    return result.plan


def _context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol: str = "601328.SS",
    company_type: FinancialCompanyType = FinancialCompanyType.BANK,
    availability_scope: FinancialAvailabilityScope = FinancialAvailabilityScope.CURRENT_ONLY,
) -> FinancialEligibilityAcquisitionContext:
    asset = resolve_run_asset_configuration(
        symbol,
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    plan = _qualified_plan(monkeypatch, asset)
    resolution = resolve_financial_company_type(
        provider_declared_type=company_type,
        provider_declaration_qualified=True,
        classifier_metadata={"industry": "bank" if company_type is FinancialCompanyType.BANK else "industrial"},
        present_fields=(),
    )
    listing_date = date(1987, 4, 1) if company_type is FinancialCompanyType.BANK else date(1992, 5, 20)
    annual, reporting = derive_mainland_financial_acquisition_period_supersets(
        run_as_of_date=_AS_OF,
        listing_date=listing_date,
        policy=plan.financial_eligibility_policy,
    )
    evidence = FinancialCompanyTypeAcquisitionEvidenceBinding.create(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        provider_id="tushare",
        artifact_identity="provider-artifact:v1:" + sha256(symbol.encode()).hexdigest(),
        source_ref="acq.v1:financial.company-type:" + sha256(f"source-{symbol}".encode()).hexdigest(),
        observed_at=_OBSERVED_AT,
        retrieved_at=_RETRIEVED_AT,
        provider_declared_type=resolution.provider_declared_type,
        provider_declaration_qualified=resolution.provider_declaration_qualified,
        normalized_resolver_inputs=resolution.classifier_inputs,
    )
    run_id = _run_id(symbol)
    result = build_financial_eligibility_acquisition_context(
        canonical_run_id=run_id,
        run_id=run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        authoritative_as_of_date=_AS_OF,
        as_of_date=_AS_OF,
        authoritative_availability_scope=availability_scope,
        availability_scope=availability_scope,
        acquisition_purpose="foreground_current_analysis",
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan=plan,
        routing_plan_signature=plan.plan_signature,
        company_type_resolution=resolution,
        company_type_evidence=evidence,
        listing_date=listing_date,
        listing_provenance=FinancialListingProvenance(
            provider_id="baostock",
            source_ref="provider-artifact:v1:" + sha256(f"listing-{symbol}".encode()).hexdigest(),
            observed_at=_OBSERVED_AT,
        ),
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        acquisition_annual_period_ends=annual,
        acquisition_reporting_period_ends=reporting,
    )
    assert result.context is not None
    return result.context


def _statement_tool_request(
    context: FinancialEligibilityAcquisitionContext,
    *,
    statement_type: FinancialStatementType = FinancialStatementType.BALANCE_SHEET,
    tool_call_id: str = "statement-call",
) -> FinancialToolRequest:
    tool_names = {
        FinancialStatementType.BALANCE_SHEET: "get_balance_sheet",
        FinancialStatementType.INCOME_STATEMENT: "get_income_statement",
        FinancialStatementType.CASH_FLOW: "get_cashflow",
    }
    return FinancialToolRequest(
        tool_name=tool_names[statement_type],
        statement_type=statement_type,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_AS_OF,
        material_arguments={"format": "canonical"},
        tool_call_id=tool_call_id,
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
    )


def test_balance_sheet_request_projects_entirely_from_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    request = build_financial_statement_routing_request(
        _statement_tool_request(context),
        context,
    )

    assert request.contract_version == "financial-statement-routing-request-v3"
    assert request.requested_capability is MainlandCapability.BALANCE_SHEET
    assert request.instrument_identity == context.instrument_identity
    assert request.company_type_resolution == context.company_type_resolution
    assert request.company_type_evidence == context.company_type_evidence
    assert request.listing_date == context.listing_date
    assert request.listing_provenance == context.listing_provenance
    assert request.requested_analysis_scope is context.availability_scope
    assert request.eligible_annual_period_ends == context.acquisition_annual_period_ends
    assert (
        request.eligible_reporting_period_ends
        == context.acquisition_reporting_period_ends
    )
    assert request.financial_eligibility_context_binding == (
        FinancialEligibilityRequestBinding.from_context(context)
    )
    assert json.loads(request.material_arguments_json) == {"format": "canonical"}


@pytest.mark.parametrize(
    ("statement_type", "capability"),
    (
        (FinancialStatementType.INCOME_STATEMENT, MainlandCapability.INCOME_STATEMENT),
        (FinancialStatementType.CASH_FLOW, MainlandCapability.CASH_FLOW),
    ),
)
def test_other_full_statement_requests_project_entirely_from_context(
    monkeypatch: pytest.MonkeyPatch,
    statement_type: FinancialStatementType,
    capability: MainlandCapability,
) -> None:
    context = _context(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
    )

    request = build_financial_statement_routing_request(
        _statement_tool_request(context, statement_type=statement_type),
        context,
    )

    assert request.statement_type is statement_type
    assert request.requested_capability is capability
    assert request.company_type_resolution == context.company_type_resolution
    assert request.company_type_evidence == context.company_type_evidence


def _indicator_tool_request(
    context: FinancialEligibilityAcquisitionContext,
    *,
    tool_call_id: str = "indicator-call",
) -> FinancialToolRequest:
    families = (FinancialRatioFamily.PROFIT, FinancialRatioFamily.OPERATION)
    requirements = FinancialIndicatorEvidenceRequirements.create(
        requested_families=families,
        canonical_evidence_contract_id="canonical-evidence-contract.v1",
        canonical_evidence_contract_required_families=(FinancialRatioFamily.PROFIT,),
        capability_profile_id="equity.v1",
        capability_profile_required_families=(FinancialRatioFamily.OPERATION,),
        strategy_rules_id="mainland-equity-strategy-rules.v1",
    )
    return FinancialToolRequest(
        tool_name="get_fundamentals",
        statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=FinancialReportingFrequency.NOT_APPLICABLE,
        as_of_date=_AS_OF,
        material_arguments={"projection": "ratios"},
        tool_call_id=tool_call_id,
        financial_eligibility_context_binding=(
            FinancialEligibilityRequestBinding.from_context(context)
        ),
        indicator_request_arguments=FinancialIndicatorRequestArguments(
            requested_families=families,
            family_evidence_requirements=requirements,
        ),
    )


def test_indicator_request_projects_entirely_from_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    tool_request = _indicator_tool_request(context)

    request = build_financial_indicator_routing_request(tool_request, context)

    assert request.contract_version == "financial-indicator-routing-request-v6"
    assert request.requested_capability is MainlandCapability.FINANCIAL_INDICATORS
    assert request.company_type_resolution == context.company_type_resolution
    assert request.company_type_evidence == context.company_type_evidence
    assert request.listing_date == context.listing_date
    assert request.listing_provenance == context.listing_provenance
    assert request.requested_analysis_scope is context.availability_scope
    assert request.eligible_annual_period_ends == context.acquisition_annual_period_ends
    assert (
        request.eligible_reporting_period_ends
        == context.acquisition_reporting_period_ends
    )
    assert request.ratio_families == (
        FinancialRatioFamily.PROFIT,
        FinancialRatioFamily.OPERATION,
    )
    assert request.family_evidence_requirements == (
        tool_request.indicator_request_arguments.family_evidence_requirements
    )


def test_non_bank_indicator_preserves_full_resolution_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
    )

    request = build_financial_indicator_routing_request(
        _indicator_tool_request(context),
        context,
    )

    assert request.company_type is FinancialCompanyType.INDUSTRIAL_NON_BANK
    assert request.company_type_resolution == context.company_type_resolution
    assert request.company_type_evidence == context.company_type_evidence
    assert request.financial_eligibility_context_binding.company_type_resolution == (
        context.company_type_resolution
    )


def test_ticket_02b_acquired_context_constructs_fresh_statement_and_indicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "601328.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    plan = _qualified_plan(monkeypatch, asset)
    seed = _context(monkeypatch)
    metadata_binding = FinancialEligibilityMetadataBinding(
        instrument_identity=asset.instrument_identity,
        as_of_date=_AS_OF,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
    )
    acquisition_request = FinancialEligibilityMetadataAcquisitionRequest(
        canonical_run_id=seed.run_id,
        run_id=seed.run_id,
        asset_configuration=asset,
        asset_configuration_signature=asset.asset_configuration_signature,
        instrument_identity=asset.instrument_identity,
        authoritative_as_of_date=_AS_OF,
        as_of_date=_AS_OF,
        authoritative_availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        availability_scope=FinancialAvailabilityScope.CURRENT_ONLY,
        financial_eligibility_binding=plan.financial_eligibility_binding,
        routing_plan_signature=plan.plan_signature,
        currency="CNY",
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
    )
    result = FinancialEligibilityMetadataAcquirer(
        routing_plan=plan,
        router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
        ),
        stop_signal=Event(),
    ).acquire(
        acquisition_request,
        retained_company_type_evidence=(
            FinancialCompanyTypeEvidenceProjection(
                metadata_binding=metadata_binding,
                evidence=seed.company_type_evidence,
            ),
        ),
        retained_listing_metadata=(
            FinancialListingMetadataProjection(
                metadata_binding=metadata_binding,
                metadata=FinancialListingMetadata(
                    instrument_identity=asset.instrument_identity,
                    as_of_date=_AS_OF,
                    listing_date=seed.listing_date,
                    listing_provenance=seed.listing_provenance,
                ),
            ),
        ),
    )
    assert result.context is not None
    acquired = result.context

    statement = build_financial_statement_routing_request(
        _statement_tool_request(acquired),
        acquired,
    )
    indicator = build_financial_indicator_routing_request(
        _indicator_tool_request(acquired),
        acquired,
    )

    assert statement.company_type_resolution == acquired.company_type_resolution
    assert statement.listing_provenance == acquired.listing_provenance
    assert indicator.company_type_evidence == acquired.company_type_evidence
    assert indicator.listing_provenance == acquired.listing_provenance


@pytest.mark.parametrize(
    "availability_scope",
    (
        FinancialAvailabilityScope.CURRENT_ONLY,
        FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE,
    ),
)
def test_availability_scope_survives_both_request_projections(
    monkeypatch: pytest.MonkeyPatch,
    availability_scope: FinancialAvailabilityScope,
) -> None:
    context = _context(monkeypatch, availability_scope=availability_scope)

    statement = build_financial_statement_routing_request(
        _statement_tool_request(context),
        context,
    )
    indicator = build_financial_indicator_routing_request(
        _indicator_tool_request(context),
        context,
    )

    assert statement.requested_analysis_scope is availability_scope
    assert indicator.requested_analysis_scope is availability_scope
    assert (
        statement.financial_eligibility_context_binding.availability_scope
        is availability_scope
    )
    assert (
        indicator.financial_eligibility_context_binding.availability_scope
        is availability_scope
    )


def test_same_inputs_and_mapping_order_produce_identical_canonical_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    first_tool = _statement_tool_request(context)
    first_tool = first_tool.model_copy(
        update={"material_arguments": {"alpha": 1, "beta": {"x": 2, "y": 3}}}
    )
    reordered_tool = first_tool.model_copy(
        update={"material_arguments": {"beta": {"y": 3, "x": 2}, "alpha": 1}}
    )

    first = build_financial_statement_routing_request(first_tool, context)
    repeated = build_financial_statement_routing_request(first_tool, context)
    reordered = build_financial_statement_routing_request(reordered_tool, context)

    assert first == repeated == reordered
    assert first.model_dump_json() == repeated.model_dump_json()
    assert first.request_identity == reordered.request_identity


def test_factories_have_exact_two_input_public_seams_and_are_concurrently_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    tool_request = _statement_tool_request(context)

    for factory in (
        build_financial_statement_routing_request,
        build_financial_indicator_routing_request,
    ):
        assert tuple(inspect.signature(factory).parameters) == (
            "tool_request",
            "context",
        )
    with ThreadPoolExecutor(max_workers=4) as executor:
        projected = tuple(
            executor.map(
                lambda _index: build_financial_statement_routing_request(
                    tool_request,
                    context,
                ),
                range(12),
            )
        )

    assert len({item.request_identity for item in projected}) == 1
    assert all(item == projected[0] for item in projected)


def test_request_safe_projection_is_round_trip_checkpoint_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    statement = build_financial_statement_routing_request(
        _statement_tool_request(context),
        context,
    )
    indicator = build_financial_indicator_routing_request(
        _indicator_tool_request(context),
        context,
    )

    from tradingagents.dataflows.financial_capability_routing import (
        FinancialIndicatorRoutingRequest,
        FinancialStatementRoutingRequest,
    )

    for request_type, request in (
        (FinancialStatementRoutingRequest, statement),
        (FinancialIndicatorRoutingRequest, indicator),
    ):
        projection = request.safe_projection()
        encoded = json.dumps(projection, separators=(",", ":"), sort_keys=True)
        restored = request_type.model_validate(json.loads(encoded))
        assert restored == request
        assert restored.request_identity == request.request_identity
        assert "fixture-only" not in encoded
        assert "raw_text" not in encoded


def test_canonical_request_identity_snapshots_are_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    statement = build_financial_statement_routing_request(
        _statement_tool_request(context),
        context,
    )
    indicator = build_financial_indicator_routing_request(
        _indicator_tool_request(context),
        context,
    )

    assert statement.request_identity == (
        "financial-statement-routing-request:v3:"
        "43b230feb64c35f447de58ad00fddba5b2479ba6098fab83f94e3a2c7b4e66cb"
    )
    assert indicator.request_identity == (
        "financial-indicator-routing-request:v6:"
        "9924c96fe031280e67eab0709f39a4929f23e76a31378723bd4fbb24e0ad1641"
    )
    assert sha256(
        json.dumps(
            statement.safe_projection(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest() == "cf0ffd899595eedab20c9dba24d84c05915d83df52967e78b137898ae863cffd"
    assert sha256(
        json.dumps(
            indicator.safe_projection(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest() == "ad800eb9ab6942d82e41ac7f274b8a90544181710665c209e9d2600d3be72414"


def test_correlation_ids_are_nonmaterial_but_material_arguments_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    first = build_financial_statement_routing_request(
        _statement_tool_request(context, tool_call_id="call-one"),
        context,
    )
    correlation_changed = build_financial_statement_routing_request(
        _statement_tool_request(context, tool_call_id="call-two").model_copy(
            update={"graph_message_id": "message-two", "process_id": 999}
        ),
        context,
    )
    material_changed = build_financial_statement_routing_request(
        _statement_tool_request(context).model_copy(
            update={"material_arguments": {"format": "different"}}
        ),
        context,
    )

    assert first == correlation_changed
    assert first.request_identity == correlation_changed.request_identity
    assert first.request_identity != material_changed.request_identity


def _with_binding(
    request: FinancialToolRequest,
    **updates: object,
) -> FinancialToolRequest:
    binding = request.financial_eligibility_context_binding
    assert binding is not None
    return request.model_copy(
        update={
            "financial_eligibility_context_binding": binding.model_copy(
                update=updates
            )
        }
    )


def _assert_factory_failure(
    expected: FinancialRequestFactoryFailureReason,
    request: FinancialToolRequest,
    context: FinancialEligibilityAcquisitionContext | None,
) -> None:
    with pytest.raises(FinancialRequestFactoryError) as caught:
        build_financial_statement_routing_request(request, context)  # type: ignore[arg-type]
    assert caught.value.reason is expected
    assert caught.value.diagnostic_code == expected.value
    assert str(caught.value) == expected.value


def test_missing_context_and_context_version_or_identity_mismatch_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    request = _statement_tool_request(context)

    _assert_factory_failure(
        FinancialRequestFactoryFailureReason.REQUEST_BINDING_MISSING,
        request.model_copy(update={"financial_eligibility_context_binding": None}),
        context,
    )
    _assert_factory_failure(
        FinancialRequestFactoryFailureReason.MISSING_ELIGIBILITY_CONTEXT,
        request,
        None,
    )
    _assert_factory_failure(
        FinancialRequestFactoryFailureReason.CONTEXT_VERSION_MISMATCH,
        _with_binding(
            request,
            context_contract_version="financial-eligibility-acquisition-context-v2",
        ),
        context,
    )
    _assert_factory_failure(
        FinancialRequestFactoryFailureReason.CONTEXT_IDENTITY_MISMATCH,
        _with_binding(
            request,
            context_identity=(
                "financial-eligibility-acquisition-context:v1:" + "0" * 64
            ),
        ),
        context,
    )


def test_invalid_tool_request_fails_typed_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)

    with pytest.raises(FinancialRequestFactoryError) as caught:
        build_financial_statement_routing_request(object(), context)  # type: ignore[arg-type]

    assert caught.value.reason is FinancialRequestFactoryFailureReason.INVALID_TOOL_REQUEST
    assert str(caught.value) == "invalid_tool_request"


def test_run_asset_instrument_and_symbol_mismatches_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    other = _context(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
    )
    request = _statement_tool_request(context)
    cases = (
        (
            FinancialRequestFactoryFailureReason.RUN_MISMATCH,
            {"canonical_run_id": other.run_id},
        ),
        (
            FinancialRequestFactoryFailureReason.ASSET_MISMATCH,
            {"asset_configuration_signature": other.asset_configuration_signature},
        ),
        (
            FinancialRequestFactoryFailureReason.INSTRUMENT_IDENTITY_MISMATCH,
            {"instrument_identity": other.instrument_identity},
        ),
        (
            FinancialRequestFactoryFailureReason.SYMBOL_MISMATCH,
            {"canonical_symbol": other.instrument_identity.symbol},
        ),
    )
    for reason, updates in cases:
        _assert_factory_failure(reason, _with_binding(request, **updates), context)


def test_as_of_availability_plan_and_policy_mismatches_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    request = _statement_tool_request(context)
    wrong_policy = context.financial_eligibility_binding.model_copy(
        update={"policy_signature": "mainland-financial-eligibility-policy:v2:" + "0" * 64}
    )
    cases = (
        (
            FinancialRequestFactoryFailureReason.AS_OF_MISMATCH,
            _with_binding(request, as_of_date=date(2026, 8, 7)),
        ),
        (
            FinancialRequestFactoryFailureReason.AVAILABILITY_SCOPE_MISMATCH,
            _with_binding(
                request,
                availability_scope=FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE,
            ),
        ),
        (
            FinancialRequestFactoryFailureReason.ROUTING_PLAN_MISMATCH,
            _with_binding(
                request,
                routing_plan_signature="mainland-routing-plan:v1:" + "0" * 64,
            ),
        ),
        (
            FinancialRequestFactoryFailureReason.ELIGIBILITY_POLICY_MISMATCH,
            _with_binding(request, financial_eligibility_binding=wrong_policy),
        ),
    )
    for reason, changed in cases:
        _assert_factory_failure(reason, changed, context)


def test_resolution_listing_and_period_mismatches_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    other = _context(
        monkeypatch,
        symbol="600895.SS",
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
    )
    request = _statement_tool_request(context)
    cases = (
        (
            FinancialRequestFactoryFailureReason.COMPANY_TYPE_RESOLUTION_MISMATCH,
            _with_binding(request, company_type_resolution=other.company_type_resolution),
        ),
        (
            FinancialRequestFactoryFailureReason.COMPANY_TYPE_RESOLUTION_MISMATCH,
            _with_binding(request, company_type_evidence=other.company_type_evidence),
        ),
        (
            FinancialRequestFactoryFailureReason.LISTING_PROVENANCE_MISMATCH,
            _with_binding(request, listing_provenance=other.listing_provenance),
        ),
        (
            FinancialRequestFactoryFailureReason.LISTING_PROVENANCE_MISMATCH,
            _with_binding(request, listing_date=other.listing_date),
        ),
        (
            FinancialRequestFactoryFailureReason.ACQUISITION_PERIOD_MISMATCH,
            _with_binding(
                request,
                acquisition_annual_period_ends=(date(2024, 12, 31),),
            ),
        ),
        (
            FinancialRequestFactoryFailureReason.ACQUISITION_PERIOD_MISMATCH,
            _with_binding(
                request,
                acquisition_reporting_period_ends=(date(2024, 12, 31),),
            ),
        ),
    )
    for reason, changed in cases:
        _assert_factory_failure(reason, changed, context)


def test_remaining_context_binding_mismatches_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    request = _statement_tool_request(context)
    cases = (
        _with_binding(request, acquisition_purpose="historical_replay"),
        _with_binding(request, currency="USD"),
        _with_binding(
            request,
            consolidation_scope=FinancialConsolidationScope.PARENT,
        ),
    )

    for changed in cases:
        _assert_factory_failure(
            FinancialRequestFactoryFailureReason.CONTEXT_BINDING_MISMATCH,
            changed,
            context,
        )


def test_unsupported_capabilities_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    indicator = _indicator_tool_request(context)
    statement = _statement_tool_request(context)

    _assert_factory_failure(
        FinancialRequestFactoryFailureReason.UNSUPPORTED_CAPABILITY,
        indicator,
        context,
    )
    with pytest.raises(FinancialRequestFactoryError) as caught:
        build_financial_indicator_routing_request(statement, context)
    assert caught.value.reason is FinancialRequestFactoryFailureReason.UNSUPPORTED_CAPABILITY


def test_projection_has_zero_io_resolution_or_metadata_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    before = context.model_dump_json()

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pure request factory reached an external work seam")

    monkeypatch.setattr(
        "tradingagents.dataflows.financial_eligibility_metadata.FinancialEligibilityMetadataAcquirer.acquire",
        unexpected,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.financial_contracts.resolve_financial_company_type",
        unexpected,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.baostock_capabilities.BaoStockCapabilityAdapter.acquire_lifecycle",
        unexpected,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.financial_capability_routing.MainlandFinancialCapabilityRouter.route_statement",
        unexpected,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.financial_capability_routing.MainlandFinancialCapabilityRouter.route_indicators",
        unexpected,
    )
    monkeypatch.setattr(
        "tradingagents.market_history.coordinator.ProviderRequestCoordinator.execute_retry_sequence",
        unexpected,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.provider_subrequests.ProviderSubrequestCache.execute",
        unexpected,
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.tushare_statements.TushareStatementAdapter.acquire_balance_sheet",
        unexpected,
    )

    build_financial_statement_routing_request(_statement_tool_request(context), context)
    build_financial_indicator_routing_request(_indicator_tool_request(context), context)

    assert context.model_dump_json() == before
    assert context.context_identity == json.loads(before)["context_identity"]


def test_binding_failure_has_zero_io_and_preserves_both_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    request = _with_binding(
        _statement_tool_request(context),
        routing_plan_signature="mainland-routing-plan:v1:" + "0" * 64,
    )
    context_before = context.model_dump_json()
    request_before = request.model_dump_json()

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("binding failure reached an external work seam")

    for target in (
        "tradingagents.dataflows.financial_eligibility_metadata.FinancialEligibilityMetadataAcquirer.acquire",
        "tradingagents.dataflows.financial_contracts.resolve_financial_company_type",
        "tradingagents.dataflows.baostock_capabilities.BaoStockCapabilityAdapter.acquire_lifecycle",
        "tradingagents.dataflows.financial_capability_routing.MainlandFinancialCapabilityRouter.route_statement",
        "tradingagents.dataflows.financial_capability_routing.MainlandFinancialCapabilityRouter.route_indicators",
        "tradingagents.market_history.coordinator.ProviderRequestCoordinator.execute_retry_sequence",
        "tradingagents.dataflows.provider_subrequests.ProviderSubrequestCache.execute",
    ):
        monkeypatch.setattr(target, unexpected)

    _assert_factory_failure(
        FinancialRequestFactoryFailureReason.ROUTING_PLAN_MISMATCH,
        request,
        context,
    )
    assert context.model_dump_json() == context_before
    assert request.model_dump_json() == request_before


def test_versioned_requests_survive_routing_and_dispatcher_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    asset = resolve_run_asset_configuration(
        context.instrument_identity.symbol,
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    plan = _qualified_plan(monkeypatch, asset)
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        indicator_sources={},
    )
    unused_provider = FinancialProvider(
        name="fixture_provider",
        variants=(
            FinancialProviderVariant(
                variant_id="fixture_variant",
                invoke=lambda _request: None,
            ),
        ),
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=context.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_balance_sheet": (unused_provider,),
            "get_fundamentals": (unused_provider,),
        },
        capability_routing_plan_signature=plan.plan_signature,
        qualified_statement_router=router,
    )
    statement_tool = _statement_tool_request(context)
    indicator_tool = _indicator_tool_request(context)
    statement = build_financial_statement_routing_request(statement_tool, context)
    indicator = build_financial_indicator_routing_request(indicator_tool, context)

    statement_result = dispatcher.dispatch_statement_selection(
        statement_tool,
        statement,
    )
    indicator_result = dispatcher.dispatch_indicator_selection(
        indicator_tool,
        indicator,
    )

    assert statement_result.selection_request == statement
    assert indicator_result.selection_request == indicator
    assert statement_result.selection_request.company_type_evidence == (
        context.company_type_evidence
    )
    assert indicator_result.selection_request.listing_provenance == (
        context.listing_provenance
    )
    assert dispatcher.restored_statement_selection_request(statement_tool) == statement
    assert dispatcher.restored_indicator_selection_request(indicator_tool) == indicator


@pytest.mark.parametrize(
    "unsafe_arguments",
    (
        {"credential": "fixture-secret"},
        {"raw_text": "provider payload"},
        {"url": "https://example.invalid/data?token=fixture"},
        {"path": "C:\\private\\provider.json"},
        {"error": "ProviderException: raw failure"},
    ),
)
def test_unsafe_request_material_fails_typed_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_arguments: dict[str, object],
) -> None:
    context = _context(monkeypatch)
    request = _statement_tool_request(context).model_copy(
        update={"material_arguments": unsafe_arguments}
    )

    with pytest.raises(FinancialRequestFactoryError) as caught:
        build_financial_statement_routing_request(request, context)

    assert caught.value.reason is FinancialRequestFactoryFailureReason.UNSAFE_REQUEST
    rendered = repr(caught.value) + str(caught.value)
    assert all(str(value) not in rendered for value in unsafe_arguments.values())


def test_new_versions_are_additive_to_legacy_request_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    legacy = {
        "contract_version": "financial-statement-routing-request-v2",
        "eligibility_policy_version": "mainland-financial-period-eligibility-v2",
        "financial_eligibility_binding": context.financial_eligibility_binding,
        "instrument_identity": context.instrument_identity,
        "statement_type": FinancialStatementType.BALANCE_SHEET,
        "as_of_date": context.as_of_date,
        "company_type": context.company_type_resolution.company_type,
        "consolidation_scope": context.consolidation_scope,
        "currency": context.currency,
        "eligible_annual_period_ends": context.acquisition_annual_period_ends,
        "eligible_reporting_period_ends": context.acquisition_reporting_period_ends,
        "listing_date": context.listing_date,
        "listing_provenance": context.listing_provenance,
    }

    from tradingagents.dataflows.financial_capability_routing import (
        FinancialStatementRoutingRequest,
    )

    restored = FinancialStatementRoutingRequest.model_validate(legacy)
    assert restored.contract_version == "financial-statement-routing-request-v2"
    assert "financial_eligibility_context_binding" not in restored.model_dump(mode="json")
    with pytest.raises(ValidationError):
        FinancialStatementRoutingRequest.model_validate(
            {
                **legacy,
                "financial_eligibility_context_binding": (
                    FinancialEligibilityRequestBinding.from_context(context)
                ),
            }
        )

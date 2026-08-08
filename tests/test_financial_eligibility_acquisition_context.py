from __future__ import annotations

import copy
import importlib.util
import json
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingagents.asset_configuration import RunAssetConfiguration, resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
    preflight_mainland_capability_routing,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialConsolidationScope,
    FinancialListingProvenance,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
    FinancialCompanyTypeEvidenceBinding,
    FinancialEligibilityAcquisitionContext,
    FinancialEligibilityAcquisitionFailureReason,
    FinancialEligibilityAcquisitionResult,
    build_financial_eligibility_acquisition_context,
    derive_mainland_financial_acquisition_period_supersets,
    financial_eligibility_acquisition_context_identity,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind

_AS_OF = date(2026, 8, 8)
_OBSERVED_AT = datetime(2026, 8, 8, 1, 2, tzinfo=timezone.utc)
_RETRIEVED_AT = datetime(2026, 8, 8, 1, 3, tzinfo=timezone.utc)


def _run_id(label: str) -> str:
    return f"run:{sha256(label.encode('utf-8')).hexdigest()}"


def _qualified_config(enabled: object) -> dict[str, object]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": enabled,
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    return config


def _asset(symbol: str) -> RunAssetConfiguration:
    return resolve_run_asset_configuration(
        symbol,
        config=copy.deepcopy(DEFAULT_CONFIG),
    )


def _qualified_plan(
    monkeypatch: pytest.MonkeyPatch,
    asset: RunAssetConfiguration,
    *,
    enabled: object = ("statements", "financial_indicators"),
) -> MainlandCapabilityRoutingPlan:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    result = preflight_mainland_capability_routing(
        asset,
        config=_qualified_config(enabled),
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert result.plan is not None
    return result.plan


def _resolution(company_type: FinancialCompanyType) -> FinancialCompanyTypeResolution:
    if company_type is FinancialCompanyType.BANK:
        return resolve_financial_company_type(
            provider_declared_type=FinancialCompanyType.BANK,
            provider_declaration_qualified=True,
            classifier_metadata={"industry": "bank"},
            present_fields=("customer_deposits", "loans_and_advances"),
        )
    if company_type is FinancialCompanyType.INDUSTRIAL_NON_BANK:
        return resolve_financial_company_type(
            provider_declared_type=None,
            provider_declaration_qualified=False,
            classifier_metadata={"industry": "industrial"},
            present_fields=("inventory", "accounts_receivable"),
        )
    raise AssertionError(company_type)


def _evidence(
    resolution: FinancialCompanyTypeResolution,
    *,
    provider_id: str = "tushare",
    inputs: tuple | None = None,
) -> FinancialCompanyTypeEvidenceBinding:
    return FinancialCompanyTypeEvidenceBinding.create(
        provider_id=provider_id,
        artifact_identity=(
            "provider-artifact:v1:"
            + sha256(f"artifact-{provider_id}".encode()).hexdigest()
        ),
        source_ref=(
            "acq.v1:financial.company-type:"
            + sha256(f"source-{provider_id}".encode()).hexdigest()
        ),
        observed_at=_OBSERVED_AT,
        retrieved_at=_RETRIEVED_AT,
        provider_declared_type=resolution.provider_declared_type,
        provider_declaration_qualified=resolution.provider_declaration_qualified,
        normalized_resolver_inputs=(
            resolution.classifier_inputs if inputs is None else inputs
        ),
    )


def _listing_provenance(label: str = "listing") -> FinancialListingProvenance:
    return FinancialListingProvenance(
        provider_id="baostock",
        source_ref=(
            "provider-artifact:v1:"
            + sha256(label.encode("utf-8")).hexdigest()
        ),
        observed_at=_OBSERVED_AT,
    )


def _context_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol: str = "601328.SS",
    company_type: FinancialCompanyType = FinancialCompanyType.BANK,
    listing_date: date = date(1987, 4, 1),
) -> dict[str, object]:
    asset = _asset(symbol)
    plan = _qualified_plan(monkeypatch, asset)
    resolution = _resolution(company_type)
    annual, reporting = derive_mainland_financial_acquisition_period_supersets(
        run_as_of_date=_AS_OF,
        listing_date=listing_date,
        policy=plan.financial_eligibility_policy,
    )
    run_id = _run_id(symbol)
    return {
        "canonical_run_id": run_id,
        "run_id": run_id,
        "asset_configuration": asset,
        "asset_configuration_signature": asset.asset_configuration_signature,
        "instrument_identity": asset.instrument_identity,
        "authoritative_as_of_date": _AS_OF,
        "as_of_date": _AS_OF,
        "authoritative_availability_scope": FinancialAvailabilityScope.CURRENT_ONLY,
        "availability_scope": FinancialAvailabilityScope.CURRENT_ONLY,
        "acquisition_purpose": "foreground_current_analysis",
        "financial_eligibility_binding": plan.financial_eligibility_binding,
        "routing_plan": plan,
        "routing_plan_signature": plan.plan_signature,
        "company_type_resolution": resolution,
        "company_type_evidence": _evidence(resolution),
        "listing_date": listing_date,
        "listing_provenance": _listing_provenance(symbol),
        "currency": "CNY",
        "consolidation_scope": FinancialConsolidationScope.CONSOLIDATED,
        "acquisition_annual_period_ends": annual,
        "acquisition_reporting_period_ends": reporting,
    }


def _success_context(inputs: dict[str, object]) -> FinancialEligibilityAcquisitionContext:
    result = build_financial_eligibility_acquisition_context(**inputs)
    assert result.failure is None
    assert result.context is not None
    return result.context


def _failure_reason(inputs: dict[str, object]) -> FinancialEligibilityAcquisitionFailureReason:
    result = build_financial_eligibility_acquisition_context(**inputs)
    assert result.context is None
    assert result.failure is not None
    return result.failure.reason


@pytest.mark.parametrize(
    ("symbol", "company_type", "listing_date"),
    (
        ("601328.SS", FinancialCompanyType.BANK, date(1987, 4, 1)),
        (
            "600895.SS",
            FinancialCompanyType.INDUSTRIAL_NON_BANK,
            date(1992, 5, 20),
        ),
    ),
)
def test_valid_contexts_construct_deterministically(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    company_type: FinancialCompanyType,
    listing_date: date,
) -> None:
    inputs = _context_inputs(
        monkeypatch,
        symbol=symbol,
        company_type=company_type,
        listing_date=listing_date,
    )

    first = _success_context(inputs)
    second = _success_context(dict(reversed(tuple(inputs.items()))))

    assert first == second
    assert first.context_identity == second.context_identity
    assert first.contract_version == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION
    assert first.company_type_resolution.company_type is company_type
    assert first.instrument_identity.symbol == symbol
    assert first.safe_projection() == second.safe_projection()
    assert FinancialEligibilityAcquisitionContext.model_validate(
        json.loads(json.dumps(first.safe_projection()))
    ) == first


def test_resolver_input_order_does_not_change_evidence_or_context_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _context_inputs(monkeypatch)
    resolution = inputs["company_type_resolution"]
    assert isinstance(resolution, FinancialCompanyTypeResolution)
    reordered = _evidence(
        resolution,
        inputs=tuple(reversed(resolution.classifier_inputs)),
    )
    reordered_inputs = dict(inputs, company_type_evidence=reordered)

    assert reordered == inputs["company_type_evidence"]
    assert _success_context(reordered_inputs).context_identity == (
        _success_context(inputs).context_identity
    )


def test_each_context_field_is_identity_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _success_context(_context_inputs(monkeypatch))
    projection = context.safe_projection()
    original_identity = context.context_identity
    mutations: dict[str, object] = {
        "contract_version": "financial-eligibility-acquisition-context-v2",
        "run_id": _run_id("other"),
        "asset_configuration_signature": "asset-config:v1:" + "1" * 64,
        "instrument_identity": {
            **projection["instrument_identity"],
            "currency": "USD",
        },
        "as_of_date": "2026-08-07",
        "availability_scope": "strict_pit_eligible",
        "acquisition_purpose": "future_replay",
        "financial_eligibility_binding": {
            **projection["financial_eligibility_binding"],
            "policy_signature": (
                "mainland-financial-eligibility-policy:v2:" + "2" * 64
            ),
        },
        "routing_plan_signature": "mainland-routing-plan:v1:" + "3" * 64,
        "company_type_resolution": {
            **projection["company_type_resolution"],
            "company_type": "industrial/non-bank",
        },
        "company_type_evidence": {
            **projection["company_type_evidence"],
            "artifact_identity": "provider-artifact:v1:" + "4" * 64,
        },
        "listing_date": "1987-04-02",
        "listing_provenance": {
            **projection["listing_provenance"],
            "source_ref": "provider-artifact:v1:" + "5" * 64,
        },
        "currency": "USD",
        "consolidation_scope": "parent",
        "acquisition_annual_period_ends": ["2024-12-31"],
        "acquisition_reporting_period_ends": ["2026-03-31"],
    }

    for field, replacement in mutations.items():
        candidate = {key: value for key, value in projection.items() if key != "context_identity"}
        candidate[field] = replacement
        assert (
            financial_eligibility_acquisition_context_identity(candidate)
            != original_identity
        ), field


def test_source_dict_order_does_not_change_context_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _success_context(_context_inputs(monkeypatch))
    payload = context.identity_payload()
    reordered = dict(reversed(tuple(payload.items())))

    assert financial_eligibility_acquisition_context_identity(payload) == (
        financial_eligibility_acquisition_context_identity(reordered)
    )


def test_unknown_ambiguous_contradictory_and_missing_evidence_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _context_inputs(monkeypatch)
    unresolved = resolve_financial_company_type(
        provider_declared_type=None,
        provider_declaration_qualified=False,
        classifier_metadata={},
        present_fields=(),
    )
    assert _failure_reason(
        dict(
            inputs,
            company_type_resolution=unresolved,
            company_type_evidence=_evidence(unresolved),
        )
    ) is FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_UNRESOLVED

    ambiguous_result = FinancialEligibilityAcquisitionResult.failed(
        FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_AMBIGUOUS,
        company_type_resolution=unresolved,
    )
    assert ambiguous_result.context is None
    assert ambiguous_result.failure is not None
    assert ambiguous_result.failure.company_type_resolution == unresolved

    contradictory = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        classifier_metadata={"industry": "industrial"},
        present_fields=("inventory",),
    )
    assert _failure_reason(
        dict(
            inputs,
            company_type_resolution=contradictory,
            company_type_evidence=_evidence(contradictory),
        )
    ) is FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_CONTRADICTORY

    assert _failure_reason(
        dict(inputs, company_type_evidence=None)
    ) is FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_EVIDENCE_UNAVAILABLE

    resolution = inputs["company_type_resolution"]
    assert isinstance(resolution, FinancialCompanyTypeResolution)
    assert _failure_reason(
        dict(inputs, company_type_evidence=_evidence(resolution, provider_id="baostock"))
    ) is FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH


def test_resolution_failure_preserves_only_the_safe_typed_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _context_inputs(monkeypatch)
    unresolved = resolve_financial_company_type(
        provider_declared_type=None,
        provider_declaration_qualified=False,
        classifier_metadata={},
        present_fields=(),
    )

    result = build_financial_eligibility_acquisition_context(
        **dict(
            inputs,
            company_type_resolution=unresolved,
            company_type_evidence=_evidence(unresolved),
        )
    )

    assert result.context is None
    assert result.failure is not None
    assert result.failure.company_type_resolution == unresolved
    assert "context" not in result.failure.model_fields
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    FinancialEligibilityAcquisitionResult.model_validate(json.loads(serialized))
    rendered = repr(result)
    assert all(
        marker not in serialized and marker not in rendered
        for marker in (
            "token=",
            "credential",
            "raw_payload",
            "raw_error",
            "raw_text",
            "https://example.invalid/path?secret=value",
            "C:\\Users\\operator\\payload.json",
        )
    )
    with pytest.raises(ValidationError, match="safe resolution"):
        FinancialEligibilityAcquisitionResult.failed(
            FinancialEligibilityAcquisitionFailureReason.COMPANY_TYPE_UNRESOLVED
        )
    with pytest.raises(ValidationError, match="safe resolution"):
        FinancialEligibilityAcquisitionResult.failed(
            FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH,
            company_type_resolution=unresolved,
        )


def test_listing_provenance_failures_are_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _context_inputs(monkeypatch)
    assert _failure_reason(
        dict(inputs, listing_provenance=None)
    ) is FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE

    non_authoritative = FinancialListingProvenance.model_construct(
        provider_id="baostock",
        source_ref="provider-artifact:v1:" + "6" * 64,
        observed_at=_OBSERVED_AT,
        authoritative=False,
    )
    assert _failure_reason(
        dict(inputs, listing_provenance=non_authoritative)
    ) is FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE

    wrong_provider = FinancialListingProvenance(
        provider_id="tushare",
        source_ref="provider-artifact:v1:" + "7" * 64,
        observed_at=_OBSERVED_AT,
    )
    assert _failure_reason(
        dict(inputs, listing_provenance=wrong_provider)
    ) is FinancialEligibilityAcquisitionFailureReason.LISTING_PROVENANCE_UNAVAILABLE


@pytest.mark.parametrize(
    "mutation",
    (
        {"run_id": _run_id("wrong-run")},
        {"asset_configuration_signature": "asset-config:v1:" + "8" * 64},
        {"currency": "USD"},
        {"as_of_date": date(2026, 8, 7)},
        {"availability_scope": FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE},
        {"routing_plan_signature": "mainland-routing-plan:v1:" + "9" * 64},
        {"acquisition_annual_period_ends": (date(2025, 12, 31),)},
    ),
)
def test_material_cross_binding_mismatches_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
) -> None:
    inputs = _context_inputs(monkeypatch)
    inputs.update(mutation)
    assert _failure_reason(inputs) is (
        FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
    )


def test_wrong_instrument_identity_fails_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _context_inputs(monkeypatch)
    inputs["instrument_identity"] = _asset("600895.SS").instrument_identity
    assert _failure_reason(inputs) is (
        FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
    )


def test_non_cny_non_mainland_and_non_equity_identities_fail_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _context_inputs(monkeypatch)
    identity = inputs["instrument_identity"]
    assert isinstance(identity, InstrumentIdentityEvidence)
    for update in (
        {"currency": "USD"},
        {"venue": "XNYS"},
        {"instrument_kind": InstrumentKind.FUND},
    ):
        invalid = identity.model_copy(update=update)
        assert _failure_reason(
            dict(inputs, instrument_identity=invalid)
        ) is FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH


def test_eligibility_policy_mismatch_fails_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _context_inputs(monkeypatch)
    binding = inputs["financial_eligibility_binding"]
    payload = binding.model_dump(mode="json")
    payload["policy_signature"] = (
        "mainland-financial-eligibility-policy:v2:" + "a" * 64
    )
    inputs["financial_eligibility_binding"] = payload
    assert _failure_reason(inputs) is (
        FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
    )


def test_interrupted_acquisition_is_typed_and_has_no_partial_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _context_inputs(monkeypatch)
    inputs["interrupted"] = True
    result = build_financial_eligibility_acquisition_context(**inputs)

    assert result.context is None
    assert result.failure is not None
    assert result.failure.reason is (
        FinancialEligibilityAcquisitionFailureReason.INTERRUPTED_ACQUISITION
    )
    assert "context" not in result.failure.model_fields


def test_failure_result_cannot_contain_a_partial_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _success_context(_context_inputs(monkeypatch))
    failure = FinancialEligibilityAcquisitionResult.failed(
        FinancialEligibilityAcquisitionFailureReason.CONTEXT_BINDING_MISMATCH
    ).failure
    assert failure is not None

    with pytest.raises(ValidationError, match="exactly one"):
        FinancialEligibilityAcquisitionResult(
            outcome="failure",
            context=context,
            failure=failure,
        )


def test_safe_contracts_reject_secrets_payloads_queries_paths_and_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _success_context(_context_inputs(monkeypatch))
    result = FinancialEligibilityAcquisitionResult.succeeded(context)
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    rendered = repr(result)
    forbidden = (
        "token=",
        "credential",
        "raw_payload",
        "raw_error",
        "raw_text",
        "https://example.invalid/path?secret=value",
        "C:\\Users\\operator\\payload.json",
    )
    assert all(value not in serialized for value in forbidden)
    assert all(value not in rendered for value in forbidden)

    resolution = context.company_type_resolution
    base = {
        "provider_id": "tushare",
        "artifact_identity": "provider-artifact:v1:" + "b" * 64,
        "observed_at": _OBSERVED_AT,
        "retrieved_at": _RETRIEVED_AT,
        "provider_declared_type": resolution.provider_declared_type,
        "provider_declaration_qualified": resolution.provider_declaration_qualified,
        "normalized_resolver_inputs": resolution.classifier_inputs,
    }
    for unsafe_ref in (
        "https://example.invalid/path?secret=value",
        "C:\\Users\\operator\\payload.json",
        "/var/tmp/provider-payload.json",
        "raw_error:ProviderException: token=fixture",
    ):
        with pytest.raises((ValidationError, ValueError)):
            FinancialCompanyTypeEvidenceBinding.create(
                **base,
                source_ref=unsafe_ref,
            )


def test_qualified_plan_adds_exact_company_type_evidence_route_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset("601328.SS")
    plan = _qualified_plan(monkeypatch, asset)

    assert plan.plan_version == "1.1"
    assert plan.route_for(MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE) == (
        "tushare",
        "akshare_sina",
        "yfinance",
    )
    assert tuple(route.capability for route in plan.routes).count(
        MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE
    ) == 1


def test_plan_input_order_does_not_change_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset("601328.SS")
    first = _qualified_plan(
        monkeypatch,
        asset,
        enabled=("financial_indicators", "statements"),
    )
    second = _qualified_plan(
        monkeypatch,
        asset,
        enabled=("statements", "financial_indicators"),
    )

    assert first.plan_signature == second.plan_signature
    assert first == second


def test_historical_plan_v1_retains_routes_without_synthetic_prerequisite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _qualified_plan(monkeypatch, _asset("601328.SS"))
    payload = current.model_dump(mode="json")
    payload["plan_version"] = "1.0"
    payload["routes"] = [
        route
        for route in payload["routes"]
        if route["capability"] != "financial_company_type_evidence"
    ]
    payload["request_budget_identities"] = [
        item
        for item in payload["request_budget_identities"]
        if item["capability"] != "financial_company_type_evidence"
    ]
    signature_payload = dict(payload)
    signature_payload.pop("plan_signature")
    payload["plan_signature"] = (
        "mainland-routing-plan:v1:"
        + sha256(
            json.dumps(
                signature_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )

    restored = MainlandCapabilityRoutingPlan.model_validate(payload)

    assert restored.plan_version == "1.0"
    with pytest.raises(KeyError):
        restored.route_for(MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE)


def test_composition_factory_contains_no_duplicate_company_type_route_table() -> None:
    root = Path(__file__).resolve().parents[1]
    composition_source = (root / "tradingagents" / "qualified_routing_composition.py").read_text(
        encoding="utf-8"
    )

    assert "FINANCIAL_COMPANY_TYPE_EVIDENCE" not in composition_source
    assert "financial_company_type_evidence" not in composition_source


def test_legacy_plan_and_authoritative_mode_controls_remain_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset("601328.SS")
    legacy = preflight_mainland_capability_routing(
        asset,
        config=copy.deepcopy(DEFAULT_CONFIG),
        environment={},
    )
    assert legacy.plan is not None
    assert legacy.plan.plan_version == "1.0"
    with pytest.raises(KeyError):
        legacy.plan.route_for(MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE)

    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = _qualified_config(("statements",))
    config["mainland_capability_routing_mode"] = "qualified_v1"
    authoritative = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "fixture-only"},
    )
    assert authoritative.plan is None
    assert authoritative.failure is not None
    assert authoritative.failure.reason.value == "qualified_v1_authority_disabled"

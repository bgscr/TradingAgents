"""Pure projections from financial tool requests and resolved eligibility context."""

from __future__ import annotations

import json
from enum import Enum
from hashlib import sha256

from tradingagents.capability_routing import MainlandCapability
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialStatementRoutingRequest,
)
from tradingagents.dataflows.financial_contracts import FinancialStatementType
from tradingagents.dataflows.financial_dispatch import FinancialToolRequest
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
    FinancialEligibilityAcquisitionContext,
    FinancialEligibilityRequestBinding,
    assert_safe_financial_eligibility_projection,
    financial_eligibility_acquisition_context_identity,
)


class FinancialRequestFactoryFailureReason(str, Enum):
    """Bounded reasons emitted before any routing/provider work."""

    INVALID_TOOL_REQUEST = "invalid_tool_request"
    MISSING_ELIGIBILITY_CONTEXT = "missing_eligibility_context"
    REQUEST_BINDING_MISSING = "request_binding_missing"
    CONTEXT_VERSION_MISMATCH = "context_version_mismatch"
    CONTEXT_IDENTITY_MISMATCH = "context_identity_mismatch"
    RUN_MISMATCH = "run_mismatch"
    ASSET_MISMATCH = "asset_mismatch"
    INSTRUMENT_IDENTITY_MISMATCH = "instrument_identity_mismatch"
    SYMBOL_MISMATCH = "symbol_mismatch"
    AS_OF_MISMATCH = "as_of_mismatch"
    AVAILABILITY_SCOPE_MISMATCH = "availability_scope_mismatch"
    ROUTING_PLAN_MISMATCH = "routing_plan_mismatch"
    ELIGIBILITY_POLICY_MISMATCH = "eligibility_policy_mismatch"
    COMPANY_TYPE_RESOLUTION_MISMATCH = "company_type_resolution_mismatch"
    LISTING_PROVENANCE_MISMATCH = "listing_provenance_mismatch"
    ACQUISITION_PERIOD_MISMATCH = "acquisition_period_mismatch"
    CONTEXT_BINDING_MISMATCH = "context_binding_mismatch"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    UNSAFE_REQUEST = "unsafe_request"


class FinancialRequestFactoryError(ValueError):
    """Typed payload-free request projection failure."""

    def __init__(self, reason: FinancialRequestFactoryFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


_STATEMENT_CAPABILITIES = {
    FinancialStatementType.BALANCE_SHEET: MainlandCapability.BALANCE_SHEET,
    FinancialStatementType.INCOME_STATEMENT: MainlandCapability.INCOME_STATEMENT,
    FinancialStatementType.CASH_FLOW: MainlandCapability.CASH_FLOW,
}
_STATEMENT_TOOL_NAMES = {
    FinancialStatementType.BALANCE_SHEET: "get_balance_sheet",
    FinancialStatementType.INCOME_STATEMENT: "get_income_statement",
    FinancialStatementType.CASH_FLOW: "get_cashflow",
}


def _canonical_material_arguments(request: FinancialToolRequest) -> str:
    try:
        material_arguments = request.canonical_material_arguments()
        assert_safe_financial_eligibility_projection(material_arguments)
        return json.dumps(
            material_arguments,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise FinancialRequestFactoryError(
            FinancialRequestFactoryFailureReason.UNSAFE_REQUEST
        ) from None


def _fail(reason: FinancialRequestFactoryFailureReason) -> None:
    raise FinancialRequestFactoryError(reason)


def _validate_context_binding(
    tool_request: FinancialToolRequest,
    context: FinancialEligibilityAcquisitionContext,
) -> FinancialEligibilityRequestBinding:
    binding = tool_request.financial_eligibility_context_binding
    if binding is None or not isinstance(binding, FinancialEligibilityRequestBinding):
        _fail(FinancialRequestFactoryFailureReason.REQUEST_BINDING_MISSING)
    assert binding is not None
    if (
        context.contract_version
        not in {
            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION,
            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
        }
        or binding.context_contract_version != context.contract_version
    ):
        _fail(FinancialRequestFactoryFailureReason.CONTEXT_VERSION_MISMATCH)
    try:
        expected_context_identity = financial_eligibility_acquisition_context_identity(
            context.identity_payload()
        )
    except (AttributeError, TypeError, ValueError):
        _fail(FinancialRequestFactoryFailureReason.CONTEXT_IDENTITY_MISMATCH)
    if (
        context.context_identity != expected_context_identity
        or binding.context_identity != context.context_identity
    ):
        _fail(FinancialRequestFactoryFailureReason.CONTEXT_IDENTITY_MISMATCH)
    if binding.canonical_run_id != context.run_id:
        _fail(FinancialRequestFactoryFailureReason.RUN_MISMATCH)
    if binding.asset_configuration_signature != context.asset_configuration_signature:
        _fail(FinancialRequestFactoryFailureReason.ASSET_MISMATCH)
    if binding.instrument_identity != context.instrument_identity:
        _fail(FinancialRequestFactoryFailureReason.INSTRUMENT_IDENTITY_MISMATCH)
    if binding.canonical_symbol != context.instrument_identity.symbol.strip().upper():
        _fail(FinancialRequestFactoryFailureReason.SYMBOL_MISMATCH)
    temporal = context.contract_version in {
        FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
        FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
    }
    expected_session = context.requested_session if temporal else context.as_of_date
    if (
        (binding.requested_session if temporal else binding.as_of_date)
        != expected_session
        or tool_request.as_of_date != expected_session
    ):
        _fail(FinancialRequestFactoryFailureReason.AS_OF_MISMATCH)
    if binding.availability_scope is not context.availability_scope:
        _fail(FinancialRequestFactoryFailureReason.AVAILABILITY_SCOPE_MISMATCH)
    if binding.routing_plan_signature != context.routing_plan_signature:
        _fail(FinancialRequestFactoryFailureReason.ROUTING_PLAN_MISMATCH)
    if binding.financial_eligibility_binding != context.financial_eligibility_binding:
        _fail(FinancialRequestFactoryFailureReason.ELIGIBILITY_POLICY_MISMATCH)
    if (
        binding.company_type_resolution != context.company_type_resolution
        or binding.company_type_evidence != context.company_type_evidence
    ):
        _fail(FinancialRequestFactoryFailureReason.COMPANY_TYPE_RESOLUTION_MISMATCH)
    if (
        binding.listing_date != context.listing_date
        or binding.listing_provenance != context.listing_provenance
    ):
        _fail(FinancialRequestFactoryFailureReason.LISTING_PROVENANCE_MISMATCH)
    if (
        binding.acquisition_annual_period_ends
        != context.acquisition_annual_period_ends
        or binding.acquisition_reporting_period_ends
        != context.acquisition_reporting_period_ends
    ):
        _fail(FinancialRequestFactoryFailureReason.ACQUISITION_PERIOD_MISMATCH)
    if (
        binding.acquisition_purpose != context.acquisition_purpose
        or binding.currency != context.currency
        or binding.consolidation_scope is not context.consolidation_scope
        or binding.temporal_eligibility_binding
        != context.temporal_eligibility_binding
    ):
        _fail(FinancialRequestFactoryFailureReason.CONTEXT_BINDING_MISMATCH)
    return binding


def _statement_identity(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    prefix = {
        "financial-statement-routing-request-v3": (
            "financial-statement-routing-request:v3:"
        ),
        "financial-statement-routing-request-v4": (
            "financial-statement-routing-request:v4:"
        ),
        "financial-statement-routing-request-v5": (
            "financial-statement-routing-request:v5:"
        ),
    }[str(payload.get("contract_version"))]
    return prefix + sha256(encoded).hexdigest()


def _indicator_identity(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    prefix = {
        "financial-indicator-routing-request-v6": (
            "financial-indicator-routing-request:v6:"
        ),
        "financial-indicator-routing-request-v7": (
            "financial-indicator-routing-request:v7:"
        ),
        "financial-indicator-routing-request-v8": (
            "financial-indicator-routing-request:v8:"
        ),
    }[str(payload.get("contract_version"))]
    return prefix + sha256(encoded).hexdigest()


def build_financial_statement_routing_request(
    tool_request: FinancialToolRequest,
    context: FinancialEligibilityAcquisitionContext,
) -> FinancialStatementRoutingRequest:
    """Project one full-statement request without I/O, resolution, or policy work."""

    if not isinstance(tool_request, FinancialToolRequest):
        raise FinancialRequestFactoryError(
            FinancialRequestFactoryFailureReason.INVALID_TOOL_REQUEST
        )
    if not isinstance(context, FinancialEligibilityAcquisitionContext):
        raise FinancialRequestFactoryError(
            FinancialRequestFactoryFailureReason.MISSING_ELIGIBILITY_CONTEXT
        )
    binding = _validate_context_binding(tool_request, context)
    capability = _STATEMENT_CAPABILITIES.get(tool_request.statement_type)
    if (
        capability is None
        or tool_request.tool_name
        != _STATEMENT_TOOL_NAMES.get(tool_request.statement_type)
    ):
        raise FinancialRequestFactoryError(
            FinancialRequestFactoryFailureReason.UNSUPPORTED_CAPABILITY
        )
    material_arguments_json = _canonical_material_arguments(tool_request)
    temporal = context.contract_version in {
        FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
        FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
    }
    windowed = (
        context.contract_version
        == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3
    )
    payload: dict[str, object] = {
        "contract_version": (
            "financial-statement-routing-request-v5"
            if windowed
            else "financial-statement-routing-request-v4"
            if temporal
            else "financial-statement-routing-request-v3"
        ),
        "eligibility_policy_version": (
            context.financial_eligibility_binding.eligibility_policy_version
        ),
        "financial_eligibility_binding": context.financial_eligibility_binding,
        "instrument_identity": context.instrument_identity,
        "statement_type": tool_request.statement_type,
        (
            "requested_session" if temporal else "as_of_date"
        ): context.requested_session if temporal else context.as_of_date,
        "company_type": context.company_type_resolution.company_type,
        "consolidation_scope": context.consolidation_scope,
        "currency": context.currency,
        "eligible_annual_period_ends": context.acquisition_annual_period_ends,
        "eligible_reporting_period_ends": context.acquisition_reporting_period_ends,
        "listing_date": context.listing_date,
        "listing_provenance": context.listing_provenance,
        "requested_capability": capability,
        "requested_analysis_scope": context.availability_scope,
        "acquisition_purpose": context.acquisition_purpose,
        "company_type_resolution": context.company_type_resolution,
        "company_type_evidence": context.company_type_evidence,
        "tool_name": tool_request.tool_name,
        "frequency": tool_request.frequency,
        "material_arguments_json": material_arguments_json,
        "financial_eligibility_context_binding": binding,
    }
    identity_payload = FinancialStatementRoutingRequest.model_construct(
        **payload,
        request_identity=None,
    ).model_dump(mode="json", exclude={"request_identity"})
    return FinancialStatementRoutingRequest(
        **payload,
        request_identity=_statement_identity(identity_payload),
    )


def build_financial_indicator_routing_request(
    tool_request: FinancialToolRequest,
    context: FinancialEligibilityAcquisitionContext,
) -> FinancialIndicatorRoutingRequest:
    """Project one indicator-family request without I/O or policy construction."""

    if not isinstance(tool_request, FinancialToolRequest):
        raise FinancialRequestFactoryError(
            FinancialRequestFactoryFailureReason.INVALID_TOOL_REQUEST
        )
    if not isinstance(context, FinancialEligibilityAcquisitionContext):
        raise FinancialRequestFactoryError(
            FinancialRequestFactoryFailureReason.MISSING_ELIGIBILITY_CONTEXT
        )
    binding = _validate_context_binding(tool_request, context)
    arguments = tool_request.indicator_request_arguments
    if (
        tool_request.statement_type
        is not FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
        or tool_request.tool_name != "get_fundamentals"
        or arguments is None
    ):
        raise FinancialRequestFactoryError(
            FinancialRequestFactoryFailureReason.UNSUPPORTED_CAPABILITY
        )
    material_arguments_json = _canonical_material_arguments(tool_request)
    temporal = context.contract_version in {
        FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
        FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
    }
    windowed = (
        context.contract_version
        == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3
    )
    payload: dict[str, object] = {
        "contract_version": (
            "financial-indicator-routing-request-v8"
            if windowed
            else "financial-indicator-routing-request-v7"
            if temporal
            else "financial-indicator-routing-request-v6"
        ),
        "eligibility_policy_version": (
            context.financial_eligibility_binding.eligibility_policy_version
        ),
        "financial_eligibility_binding": context.financial_eligibility_binding,
        "requested_analysis_scope": context.availability_scope,
        "instrument_identity": context.instrument_identity,
        (
            "requested_session" if temporal else "as_of_date"
        ): context.requested_session if temporal else context.as_of_date,
        "company_type": context.company_type_resolution.company_type,
        "company_type_resolution": context.company_type_resolution,
        "consolidation_scope": context.consolidation_scope,
        "currency": context.currency,
        "ratio_families": arguments.requested_families,
        "eligible_reporting_period_ends": context.acquisition_reporting_period_ends,
        "remaining_evidence_decision_ready": (
            arguments.remaining_evidence_decision_ready
        ),
        "family_evidence_requirements": arguments.family_evidence_requirements,
        "listing_date": context.listing_date,
        "listing_provenance": context.listing_provenance,
        "eligible_annual_period_ends": context.acquisition_annual_period_ends,
        "requested_capability": MainlandCapability.FINANCIAL_INDICATORS,
        "acquisition_purpose": context.acquisition_purpose,
        "company_type_evidence": context.company_type_evidence,
        "tool_name": tool_request.tool_name,
        "frequency": tool_request.frequency,
        "material_arguments_json": material_arguments_json,
        "financial_eligibility_context_binding": binding,
    }
    identity_payload = FinancialIndicatorRoutingRequest.model_construct(
        **payload,
        request_identity=None,
    ).model_dump(mode="json", exclude={"request_identity"})
    return FinancialIndicatorRoutingRequest(
        **payload,
        request_identity=_indicator_identity(identity_payload),
    )


__all__ = [
    "FinancialRequestFactoryError",
    "FinancialRequestFactoryFailureReason",
    "build_financial_indicator_routing_request",
    "build_financial_statement_routing_request",
]

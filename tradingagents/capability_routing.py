"""Closed, run-scoped mainland Capability Routing Plan configuration."""

from __future__ import annotations

import importlib.util
import json
import os
import re
from collections.abc import Mapping
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.asset_configuration import RunAssetConfiguration
from tradingagents.dataflows.financial_eligibility_policy import (
    MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2,
    MainlandFinancialEligibilityPolicy,
    MainlandFinancialEligibilityProjectionBinding,
    build_mainland_financial_eligibility_policy_v2,
)
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    InstrumentKind,
)

CAPABILITY_ROUTING_PLAN_VERSION_V1 = "1.0"
CAPABILITY_ROUTING_PLAN_VERSION_V2 = "1.1"
CAPABILITY_ROUTING_PLAN_VERSION = "1.2"
CAPABILITY_ROUTING_PLAN_VERSION_1_3 = "1.3"
CAPABILITY_ROUTING_PLAN_VERSION_1_4 = "1.4"
CAPABILITY_ROUTING_POLICY_VERSION = "mainland-capability-routing-policy-v1"
QUALIFICATION_PROFILE = "cn-a-2000-20260729-v1"
NORMALIZER_VERSION_V1 = "mainland-financial-normalizer-v1"
NORMALIZER_VERSION = "mainland-financial-normalizer-v2"
COMPLETENESS_POLICY_VERSION_V1 = "mainland-financial-completeness-v1"
COMPLETENESS_POLICY_VERSION = "mainland-financial-completeness-v2"

_CLOSED_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    hide_input_in_errors=True,
)
_PLAN_SIGNATURE_PREFIX = "mainland-routing-plan:v1:"
_PROVIDER_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
_IDENTITY_PATTERN = r"^[a-z][a-z0-9._:-]*$"
_RESERVED_ACCOUNT_SCOPE_MARKERS = (
    "token",
    "credential",
    "secret",
    "password",
    "api-key",
    "api_key",
    "apikey",
)


class MainlandCapabilityRoutingMode(str, Enum):
    LEGACY = "legacy"
    QUALIFIED_V1_SHADOW = "qualified_v1_shadow"
    QUALIFIED_V1 = "qualified_v1"


def _contract_versions_for_mode(
    mode: MainlandCapabilityRoutingMode,
) -> tuple[str, str, str]:
    if mode is MainlandCapabilityRoutingMode.LEGACY:
        return (
            NORMALIZER_VERSION_V1,
            COMPLETENESS_POLICY_VERSION_V1,
            "financial-period-selection-v1",
        )
    return (
        NORMALIZER_VERSION,
        COMPLETENESS_POLICY_VERSION,
        "financial-period-selection-statement-v2",
    )


_CURRENT_INDICATOR_CONTRACT_BINDINGS = (
    "financial-indicator-field-semantics-v1",
    "financial-indicator-metadata-compatibility-v1",
    "financial-manifest-v3",
    "financial-period-selection-v3",
)


class MainlandTemporalContractBindings(BaseModel):
    """Exact forward-only temporal contract inventory pinned by plan 1.3."""

    model_config = _CLOSED_MODEL_CONFIG

    temporal_policy_version: Literal[
        "provider-neutral-temporal-eligibility-v1"
    ] = "provider-neutral-temporal-eligibility-v1"
    calendar_eligibility_binding_version: Literal[
        "market-session-eligibility-binding-v1"
    ] = "market-session-eligibility-binding-v1"
    temporal_decision_version: Literal[
        "financial-metadata-temporal-eligibility-decision-v1"
    ] = "financial-metadata-temporal-eligibility-decision-v1"
    company_type_evidence_binding_version: Literal[
        "financial-company-type-evidence-binding-v3"
    ] = "financial-company-type-evidence-binding-v3"
    eligibility_metadata_binding_version: Literal[
        "financial-eligibility-metadata-binding-v2"
    ] = "financial-eligibility-metadata-binding-v2"
    company_type_evidence_projection_version: Literal[
        "financial-company-type-evidence-projection-v2"
    ] = "financial-company-type-evidence-projection-v2"
    listing_metadata_version: Literal["financial-listing-metadata-v2"] = (
        "financial-listing-metadata-v2"
    )
    listing_metadata_projection_version: Literal[
        "financial-listing-metadata-projection-v2"
    ] = "financial-listing-metadata-projection-v2"
    company_type_routing_request_version: Literal[
        "financial-company-type-evidence-routing-request-v2"
    ] = "financial-company-type-evidence-routing-request-v2"
    company_type_provider_response_version: Literal[
        "financial-company-type-evidence-provider-response-v2"
    ] = "financial-company-type-evidence-provider-response-v2"
    financial_eligibility_context_version: Literal[
        "financial-eligibility-acquisition-context-v2"
    ] = "financial-eligibility-acquisition-context-v2"
    financial_eligibility_request_binding_version: Literal[
        "financial-eligibility-request-binding-v2"
    ] = "financial-eligibility-request-binding-v2"
    financial_eligibility_acquisition_result_version: Literal[
        "financial-eligibility-acquisition-result-v2"
    ] = "financial-eligibility-acquisition-result-v2"
    statement_routing_request_version: Literal[
        "financial-statement-routing-request-v4"
    ] = "financial-statement-routing-request-v4"
    indicator_routing_request_version: Literal[
        "financial-indicator-routing-request-v7"
    ] = "financial-indicator-routing-request-v7"
    statement_routing_result_version: Literal[
        "financial-statement-routing-result-v2"
    ] = "financial-statement-routing-result-v2"
    indicator_routing_result_version: Literal[
        "financial-indicator-routing-result-v4"
    ] = "financial-indicator-routing-result-v4"
    statement_manifest_version: Literal["financial-manifest-v4"] = (
        "financial-manifest-v4"
    )
    indicator_manifest_version: Literal["financial-manifest-v4"] = (
        "financial-manifest-v4"
    )
    statement_selection_dispatch_version: Literal[
        "financial-statement-selection-dispatch-v2"
    ] = "financial-statement-selection-dispatch-v2"
    statement_selection_checkpoint_version: Literal[
        "financial-statement-selection-checkpoint-v2"
    ] = "financial-statement-selection-checkpoint-v2"
    indicator_selection_dispatch_version: Literal[
        "financial-indicator-selection-dispatch-v3"
    ] = "financial-indicator-selection-dispatch-v3"
    indicator_selection_checkpoint_version: Literal[
        "financial-indicator-selection-checkpoint-v3"
    ] = "financial-indicator-selection-checkpoint-v3"
    provider_subrequest_cache_checkpoint_version: Literal["1.2"] = "1.2"
    financial_dispatch_ledger_version: Literal["1.6"] = "1.6"
    completeness_row_version: Literal["financial-completeness-row-v5"] = (
        "financial-completeness-row-v5"
    )
    aggregate_completeness_version: Literal[
        "financial-aggregate-completeness-v4"
    ] = "financial-aggregate-completeness-v4"
    manifest_audit_version: Literal["financial-manifest-audit-v4"] = (
        "financial-manifest-audit-v4"
    )
    manifest_request_audit_version: Literal[
        "financial-manifest-request-audit-v4"
    ] = "financial-manifest-request-audit-v4"
    financial_dispatch_audit_projection_version: Literal["4.0"] = "4.0"
    financial_tool_manifest_version: Literal["financial-tool-manifest-v4"] = (
        "financial-tool-manifest-v4"
    )
    financial_tool_message_envelope_version: Literal[
        "financial-tool-message-envelope-v4"
    ] = "financial-tool-message-envelope-v4"


class MainlandTemporalContractBindingsV2(MainlandTemporalContractBindings):
    """Forward fresh-acquisition window inventory pinned by plan 1.4."""

    temporal_policy_version: Literal[
        "provider-neutral-temporal-eligibility-v2"
    ] = "provider-neutral-temporal-eligibility-v2"
    calendar_eligibility_binding_version: Literal[
        "market-session-eligibility-binding-v2"
    ] = "market-session-eligibility-binding-v2"
    acquisition_window_version: Literal[
        "financial-eligibility-acquisition-window-v1"
    ] = "financial-eligibility-acquisition-window-v1"
    temporal_transaction_binding_version: Literal[
        "financial-eligibility-temporal-transaction-binding-v1"
    ] = "financial-eligibility-temporal-transaction-binding-v1"
    temporal_evidence_acquisition_binding_version: Literal[
        "financial-temporal-evidence-acquisition-binding-v1"
    ] = "financial-temporal-evidence-acquisition-binding-v1"
    temporal_decision_version: Literal[
        "financial-metadata-temporal-eligibility-decision-v2"
    ] = "financial-metadata-temporal-eligibility-decision-v2"
    company_type_evidence_binding_version: Literal[
        "financial-company-type-evidence-binding-v4"
    ] = "financial-company-type-evidence-binding-v4"
    eligibility_metadata_binding_version: Literal[
        "financial-eligibility-metadata-binding-v3"
    ] = "financial-eligibility-metadata-binding-v3"
    company_type_evidence_projection_version: Literal[
        "financial-company-type-evidence-projection-v3"
    ] = "financial-company-type-evidence-projection-v3"
    listing_metadata_version: Literal["financial-listing-metadata-v3"] = (
        "financial-listing-metadata-v3"
    )
    listing_metadata_projection_version: Literal[
        "financial-listing-metadata-projection-v3"
    ] = "financial-listing-metadata-projection-v3"
    company_type_routing_request_version: Literal[
        "financial-company-type-evidence-routing-request-v3"
    ] = "financial-company-type-evidence-routing-request-v3"
    company_type_provider_response_version: Literal[
        "financial-company-type-evidence-provider-response-v3"
    ] = "financial-company-type-evidence-provider-response-v3"
    financial_eligibility_context_version: Literal[
        "financial-eligibility-acquisition-context-v3"
    ] = "financial-eligibility-acquisition-context-v3"
    financial_eligibility_request_binding_version: Literal[
        "financial-eligibility-request-binding-v3"
    ] = "financial-eligibility-request-binding-v3"
    financial_eligibility_acquisition_result_version: Literal[
        "financial-eligibility-acquisition-result-v3"
    ] = "financial-eligibility-acquisition-result-v3"
    statement_routing_request_version: Literal[
        "financial-statement-routing-request-v5"
    ] = "financial-statement-routing-request-v5"
    indicator_routing_request_version: Literal[
        "financial-indicator-routing-request-v8"
    ] = "financial-indicator-routing-request-v8"
    statement_routing_result_version: Literal[
        "financial-statement-routing-result-v3"
    ] = "financial-statement-routing-result-v3"
    indicator_routing_result_version: Literal[
        "financial-indicator-routing-result-v5"
    ] = "financial-indicator-routing-result-v5"
    statement_manifest_version: Literal["financial-manifest-v5"] = (
        "financial-manifest-v5"
    )
    indicator_manifest_version: Literal["financial-manifest-v5"] = (
        "financial-manifest-v5"
    )
    statement_selection_dispatch_version: Literal[
        "financial-statement-selection-dispatch-v3"
    ] = "financial-statement-selection-dispatch-v3"
    statement_selection_checkpoint_version: Literal[
        "financial-statement-selection-checkpoint-v3"
    ] = "financial-statement-selection-checkpoint-v3"
    indicator_selection_dispatch_version: Literal[
        "financial-indicator-selection-dispatch-v4"
    ] = "financial-indicator-selection-dispatch-v4"
    indicator_selection_checkpoint_version: Literal[
        "financial-indicator-selection-checkpoint-v4"
    ] = "financial-indicator-selection-checkpoint-v4"
    provider_subrequest_cache_checkpoint_version: Literal["1.3"] = "1.3"
    financial_dispatch_ledger_version: Literal["1.7"] = "1.7"
    completeness_row_version: Literal["financial-completeness-row-v6"] = (
        "financial-completeness-row-v6"
    )
    aggregate_completeness_version: Literal[
        "financial-aggregate-completeness-v5"
    ] = "financial-aggregate-completeness-v5"
    manifest_audit_version: Literal["financial-manifest-audit-v5"] = (
        "financial-manifest-audit-v5"
    )
    manifest_request_audit_version: Literal[
        "financial-manifest-request-audit-v5"
    ] = "financial-manifest-request-audit-v5"
    financial_dispatch_audit_projection_version: Literal["5.0"] = "5.0"
    financial_tool_manifest_version: Literal["financial-tool-manifest-v5"] = (
        "financial-tool-manifest-v5"
    )
    financial_tool_message_envelope_version: Literal[
        "financial-tool-message-envelope-v5"
    ] = "financial-tool-message-envelope-v5"


def _indicator_contract_bindings_for_plan_version(
    plan_version: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    if plan_version in {
        CAPABILITY_ROUTING_PLAN_VERSION,
        CAPABILITY_ROUTING_PLAN_VERSION_1_3,
        CAPABILITY_ROUTING_PLAN_VERSION_1_4,
    }:
        return _CURRENT_INDICATOR_CONTRACT_BINDINGS
    return (None, None, None, None)


class MainlandCapability(str, Enum):
    DAILY_MARKET_SNAPSHOT = "daily_market_snapshot"
    BALANCE_SHEET = "balance_sheet"
    INCOME_STATEMENT = "income_statement"
    CASH_FLOW = "cash_flow"
    FINANCIAL_INDICATORS = "financial_indicators"
    ADJUSTMENT_FACTORS = "adjustment_factors"
    SUSPENSION_STATUS = "suspension_status"
    NAME_EVENTS = "name_events"
    ISSUER_LIFECYCLE = "issuer_lifecycle"
    FINANCIAL_COMPANY_TYPE_EVIDENCE = "financial_company_type_evidence"


_PLAN_V1_CAPABILITIES = (
    MainlandCapability.DAILY_MARKET_SNAPSHOT,
    MainlandCapability.BALANCE_SHEET,
    MainlandCapability.INCOME_STATEMENT,
    MainlandCapability.CASH_FLOW,
    MainlandCapability.FINANCIAL_INDICATORS,
    MainlandCapability.ADJUSTMENT_FACTORS,
    MainlandCapability.SUSPENSION_STATUS,
    MainlandCapability.NAME_EVENTS,
    MainlandCapability.ISSUER_LIFECYCLE,
)


def _capabilities_for_plan_version(
    plan_version: str,
) -> tuple[MainlandCapability, ...]:
    if plan_version == CAPABILITY_ROUTING_PLAN_VERSION_V1:
        return _PLAN_V1_CAPABILITIES
    if plan_version in {
        CAPABILITY_ROUTING_PLAN_VERSION_V2,
        CAPABILITY_ROUTING_PLAN_VERSION,
        CAPABILITY_ROUTING_PLAN_VERSION_1_3,
        CAPABILITY_ROUTING_PLAN_VERSION_1_4,
    }:
        return tuple(MainlandCapability)
    raise ValueError("Capability Routing Plan version is unsupported")


class TushareCapability(str, Enum):
    STATEMENTS = "statements"
    FINANCIAL_INDICATORS = "financial_indicators"
    ADJUSTMENT_FACTORS = "adjustment_factors"
    NAME_EVENTS = "name_events"


class MainlandCapabilityRoutingDisposition(str, Enum):
    """Whether qualified selections may influence the authoritative run."""

    LEGACY_AUTHORITATIVE = "legacy_authoritative"
    SHADOW_NON_AUTHORITATIVE = "shadow_non_authoritative"
    QUALIFIED_AUTHORITATIVE = "qualified_authoritative"


class MainlandCapabilityRoutingFailureReason(str, Enum):
    INSTRUMENT_NOT_MAINLAND_EQUITY = "instrument_not_mainland_equity"
    INVALID_ROUTING_MODE = "invalid_routing_mode"
    TUSHARE_REQUIRES_QUALIFIED_V1 = "tushare_requires_qualified_v1"
    UNSUPPORTED_TUSHARE_CAPABILITY = "unsupported_tushare_capability"
    QUALIFICATION_PROFILE_MISMATCH = "qualification_profile_mismatch"
    TUSHARE_TOKEN_MISSING = "tushare_token_missing"
    TUSHARE_DEPENDENCY_MISSING = "tushare_dependency_missing"
    DATA_USAGE_MODE_NOT_PERSONAL_RESEARCH = (
        "data_usage_mode_not_personal_research"
    )
    TUSHARE_PACING_INVALID = "tushare_pacing_invalid"
    TUSHARE_OPERATOR_SAFETY_CEILING_INVALID = (
        "tushare_operator_safety_ceiling_invalid"
    )
    TUSHARE_ACCOUNT_SCOPE_INVALID = "tushare_account_scope_invalid"
    QUALIFIED_ROUTING_COMPOSITION_MISSING = (
        "qualified_routing_composition_missing"
    )
    QUALIFIED_ROUTING_FACTORY_CONTRACT_INVALID = (
        "qualified_routing_factory_contract_invalid"
    )
    QUALIFIED_ROUTING_FACTORY_OUTPUT_INVALID = (
        "qualified_routing_factory_output_invalid"
    )
    QUALIFIED_VALIDATION_WORKLOAD_REQUIRED = (
        "qualified_validation_workload_required"
    )
    QUALIFIED_ROUTING_PLAN_NOT_APPLICABLE = (
        "qualified_routing_plan_not_applicable"
    )
    QUALIFIED_ROUTING_PLAN_INVALID = "qualified_routing_plan_invalid"
    QUALIFIED_ROUTING_DISPOSITION_INVALID = (
        "qualified_routing_disposition_invalid"
    )
    QUALIFIED_ROUTING_ASSET_BINDING_INVALID = (
        "qualified_routing_asset_binding_invalid"
    )
    QUALIFIED_ROUTING_ELIGIBILITY_BINDING_INVALID = (
        "qualified_routing_eligibility_binding_invalid"
    )
    QUALIFIED_ROUTING_DEPENDENCY_MISSING = (
        "qualified_routing_dependency_missing"
    )
    QUALIFIED_ROUTING_SOURCE_COVERAGE_INCOMPLETE = (
        "qualified_routing_source_coverage_incomplete"
    )
    QUALIFIED_ROUTING_RUNTIME_ROOT_INVALID = (
        "qualified_routing_runtime_root_invalid"
    )
    QUALIFIED_V1_AUTHORITY_DISABLED = "qualified_v1_authority_disabled"


class MainlandCapabilityRoute(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    capability: MainlandCapability
    providers: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_providers(self) -> MainlandCapabilityRoute:
        if len(self.providers) != len(set(self.providers)):
            raise ValueError("capability route providers must be unique")
        if any(
            not provider
            or re.fullmatch(_PROVIDER_ID_PATTERN, provider) is None
            for provider in self.providers
        ):
            raise ValueError("capability route provider ID is malformed")
        return self


class TushareEndpointPacingIdentity(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    endpoint_id: str = Field(pattern=_PROVIDER_ID_PATTERN)
    upstream_service_identity: Literal["tushare-pro"] = "tushare-pro"
    pacing_policy_identity: Literal[
        "tushare-2000-point-endpoint-pacing-v1"
    ] = "tushare-2000-point-endpoint-pacing-v1"
    calls_per_minute: int = Field(ge=1, le=200)
    operator_safety_ceiling_identity: Literal[
        "tushare-operator-safety-ceiling-v1"
    ] = "tushare-operator-safety-ceiling-v1"
    operator_calls_per_minute: int = Field(ge=1, le=200)


class MainlandRequestBudgetIdentity(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    capability: MainlandCapability
    budget_identity: str = Field(pattern=_IDENTITY_PATTERN)


class MainlandCapabilityRoutingPlan(BaseModel):
    """Immutable safe projection used by state, checkpoints, and audit."""

    model_config = _CLOSED_MODEL_CONFIG

    plan_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4"] = (
        CAPABILITY_ROUTING_PLAN_VERSION
    )
    policy_version: Literal[
        "mainland-capability-routing-policy-v1"
    ] = CAPABILITY_ROUTING_POLICY_VERSION
    mode: MainlandCapabilityRoutingMode
    qualification_profile: Literal["legacy", "cn-a-2000-20260729-v1"]
    routes: tuple[MainlandCapabilityRoute, ...]
    enabled_tushare_capabilities: tuple[TushareCapability, ...] = ()
    normalizer_version: Literal[
        "mainland-financial-normalizer-v1",
        "mainland-financial-normalizer-v2",
    ] = NORMALIZER_VERSION
    completeness_policy_version: Literal[
        "mainland-financial-completeness-v1",
        "mainland-financial-completeness-v2",
    ] = COMPLETENESS_POLICY_VERSION
    account_scope_label: str = Field(
        min_length=1,
        max_length=64,
        pattern=_IDENTITY_PATTERN,
    )
    endpoint_pacing_identities: tuple[TushareEndpointPacingIdentity, ...] = ()
    request_budget_identities: tuple[MainlandRequestBudgetIdentity, ...]
    artifact_contract_version: Literal["source-artifact-v1"] = "source-artifact-v1"
    manifest_contract_version: Literal[
        "financial-manifest-v1",
        "financial-manifest-v2",
    ] = "financial-manifest-v1"
    selection_contract_version: Literal[
        "financial-period-selection-v1",
        "financial-period-selection-statement-v2",
    ] = "financial-period-selection-statement-v2"
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_manifest_contract_version: Literal[
        "financial-manifest-v3"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_selection_contract_version: Literal[
        "financial-period-selection-v3"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    temporal_contract_bindings: (
        MainlandTemporalContractBindings | MainlandTemporalContractBindingsV2 | None
    ) = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    degradation_contract_version: Literal[
        "financial-degradation-v1"
    ] = "financial-degradation-v1"
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_policy: MainlandFinancialEligibilityPolicy | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    plan_signature: str = Field(pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_closed_plan(self) -> MainlandCapabilityRoutingPlan:
        if (
            self.mode is MainlandCapabilityRoutingMode.LEGACY
            and self.plan_version != CAPABILITY_ROUTING_PLAN_VERSION_V1
        ):
            raise ValueError("legacy routing requires the historical plan version")
        expected_profile = (
            "legacy"
            if self.mode is MainlandCapabilityRoutingMode.LEGACY
            else QUALIFICATION_PROFILE
        )
        if self.qualification_profile != expected_profile:
            raise ValueError("routing mode and qualification profile contradict")
        contract_versions = (
            self.normalizer_version,
            self.completeness_policy_version,
            self.selection_contract_version,
        )
        if contract_versions != _contract_versions_for_mode(self.mode):
            raise ValueError("routing mode and contract versions contradict")
        expected_manifest_version = (
            "financial-manifest-v2"
            if self.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
            else "financial-manifest-v1"
        )
        if self.manifest_contract_version != expected_manifest_version:
            raise ValueError("routing mode and manifest contract version contradict")
        indicator_bindings = (
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
            self.indicator_manifest_contract_version,
            self.indicator_selection_contract_version,
        )
        expected_indicator_bindings = (
            _indicator_contract_bindings_for_plan_version(self.plan_version)
        )
        if indicator_bindings != expected_indicator_bindings:
            raise ValueError("routing plan indicator contract bindings contradict version")
        expected_temporal_bindings = {
            CAPABILITY_ROUTING_PLAN_VERSION_1_3: MainlandTemporalContractBindings(),
            CAPABILITY_ROUTING_PLAN_VERSION_1_4: MainlandTemporalContractBindingsV2(),
        }.get(self.plan_version)
        if self.temporal_contract_bindings != expected_temporal_bindings:
            raise ValueError("routing plan temporal contract bindings contradict version")
        if self.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW:
            historical_pre_v2 = (
                self.eligibility_policy_version is None
                and self.financial_eligibility_policy is None
                and self.financial_eligibility_binding is None
            )
            if not historical_pre_v2 and (
                self.eligibility_policy_version
                != MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
                or self.financial_eligibility_policy is None
                or self.financial_eligibility_policy.eligibility_policy_version
                != self.eligibility_policy_version
                or self.financial_eligibility_binding
                != MainlandFinancialEligibilityProjectionBinding.from_policy(
                    self.financial_eligibility_policy
                )
            ):
                raise ValueError(
                    "qualified shadow routing requires the exact v2 eligibility policy"
                )
        elif (
            self.eligibility_policy_version is not None
            or self.financial_eligibility_policy is not None
            or self.financial_eligibility_binding is not None
        ):
            raise ValueError(
                "legacy and historical qualified plans cannot carry synthetic v2 policy data"
            )
        if (
            self.mode is MainlandCapabilityRoutingMode.LEGACY
            and self.enabled_tushare_capabilities
        ):
            raise ValueError("legacy routing cannot enable Tushare capabilities")
        plan_capabilities = _capabilities_for_plan_version(self.plan_version)
        route_capabilities = tuple(route.capability for route in self.routes)
        if route_capabilities != plan_capabilities:
            raise ValueError("capability routes must be complete and canonical")
        if self.enabled_tushare_capabilities != tuple(
            sorted(
                set(self.enabled_tushare_capabilities),
                key=tuple(TushareCapability).index,
            )
        ):
            raise ValueError("enabled Tushare capabilities must be canonical")
        if self.routes != _routes(
            self.mode,
            self.enabled_tushare_capabilities,
            plan_version=self.plan_version,
        ):
            raise ValueError("capability policy routes do not match their identity")
        endpoint_ids = tuple(
            identity.endpoint_id for identity in self.endpoint_pacing_identities
        )
        if endpoint_ids != tuple(sorted(set(endpoint_ids))):
            raise ValueError("endpoint pacing identities must be canonical")
        if endpoint_ids != _endpoint_ids(self.enabled_tushare_capabilities):
            raise ValueError("endpoint pacing identities do not match enablement")
        if any(
            identity.operator_calls_per_minute > identity.calls_per_minute
            for identity in self.endpoint_pacing_identities
        ):
            raise ValueError("Operator Safety Ceiling cannot exceed pacing")
        budget_capabilities = tuple(
            identity.capability for identity in self.request_budget_identities
        )
        if budget_capabilities != plan_capabilities:
            raise ValueError("request-budget identities must be complete and canonical")
        if any(
            identity.budget_identity
            != f"foreground-{identity.capability.value}-budget-v1"
            for identity in self.request_budget_identities
        ):
            raise ValueError("request-budget identity does not match its capability")
        normalized_scope = self.account_scope_label.casefold()
        if any(
            marker in normalized_scope
            for marker in _RESERVED_ACCOUNT_SCOPE_MARKERS
        ):
            raise ValueError("account-scope label contains reserved secret text")
        expected_signature = _plan_signature(self.signature_payload())
        if self.plan_signature != expected_signature:
            raise ValueError("mainland Capability Routing Plan signature mismatch")
        return self

    def signature_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"plan_signature"})

    def route_for(
        self,
        capability: MainlandCapability,
    ) -> tuple[str, ...]:
        for route in self.routes:
            if route.capability is capability:
                return route.providers
        raise KeyError(capability.value)


class MainlandCapabilityRoutingRunProjection(BaseModel):
    """Closed checkpoint/audit projection of one rollout-bound run."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = "1.0"
    mode: MainlandCapabilityRoutingMode
    plan_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4"]
    policy_version: Literal["mainland-capability-routing-policy-v1"]
    plan_signature: str = Field(pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$")
    enabled_tushare_capabilities: tuple[TushareCapability, ...]
    qualification_profile: Literal["legacy", "cn-a-2000-20260729-v1"]
    normalizer_version: Literal[
        "mainland-financial-normalizer-v1",
        "mainland-financial-normalizer-v2",
    ]
    completeness_policy_version: Literal[
        "mainland-financial-completeness-v1",
        "mainland-financial-completeness-v2",
    ]
    manifest_contract_version: Literal[
        "financial-manifest-v1",
        "financial-manifest-v2",
    ]
    selection_contract_version: Literal[
        "financial-period-selection-v1",
        "financial-period-selection-statement-v2",
    ]
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_manifest_contract_version: Literal[
        "financial-manifest-v3"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_selection_contract_version: Literal[
        "financial-period-selection-v3"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    temporal_contract_bindings: (
        MainlandTemporalContractBindings | MainlandTemporalContractBindingsV2 | None
    ) = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    degradation_contract_version: Literal["financial-degradation-v1"]
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_policy: MainlandFinancialEligibilityPolicy | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    disposition: MainlandCapabilityRoutingDisposition
    rollback_compatible: Literal[True] = True

    @classmethod
    def from_plan(
        cls,
        plan: MainlandCapabilityRoutingPlan,
    ) -> MainlandCapabilityRoutingRunProjection:
        disposition = {
            MainlandCapabilityRoutingMode.LEGACY: (
                MainlandCapabilityRoutingDisposition.LEGACY_AUTHORITATIVE
            ),
            MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW: (
                MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
            ),
            MainlandCapabilityRoutingMode.QUALIFIED_V1: (
                MainlandCapabilityRoutingDisposition.QUALIFIED_AUTHORITATIVE
            ),
        }[plan.mode]
        return cls(
            mode=plan.mode,
            plan_version=plan.plan_version,
            policy_version=plan.policy_version,
            plan_signature=plan.plan_signature,
            enabled_tushare_capabilities=plan.enabled_tushare_capabilities,
            qualification_profile=plan.qualification_profile,
            normalizer_version=plan.normalizer_version,
            completeness_policy_version=plan.completeness_policy_version,
            manifest_contract_version=plan.manifest_contract_version,
            selection_contract_version=plan.selection_contract_version,
            indicator_field_semantics_version=(
                plan.indicator_field_semantics_version
            ),
            indicator_metadata_compatibility_version=(
                plan.indicator_metadata_compatibility_version
            ),
            indicator_manifest_contract_version=(
                plan.indicator_manifest_contract_version
            ),
            indicator_selection_contract_version=(
                plan.indicator_selection_contract_version
            ),
            temporal_contract_bindings=plan.temporal_contract_bindings,
            degradation_contract_version=plan.degradation_contract_version,
            eligibility_policy_version=plan.eligibility_policy_version,
            financial_eligibility_policy=plan.financial_eligibility_policy,
            financial_eligibility_binding=plan.financial_eligibility_binding,
            disposition=disposition,
        )

    @model_validator(mode="after")
    def _validate_plan_disposition(self) -> MainlandCapabilityRoutingRunProjection:
        if (
            self.mode is MainlandCapabilityRoutingMode.LEGACY
            and self.plan_version != CAPABILITY_ROUTING_PLAN_VERSION_V1
        ):
            raise ValueError("legacy rollout requires the historical plan version")
        expected_profile = (
            "legacy"
            if self.mode is MainlandCapabilityRoutingMode.LEGACY
            else QUALIFICATION_PROFILE
        )
        if self.qualification_profile != expected_profile:
            raise ValueError("routing rollout profile contradicts its mode")
        contract_versions = (
            self.normalizer_version,
            self.completeness_policy_version,
            self.selection_contract_version,
        )
        if contract_versions != _contract_versions_for_mode(self.mode):
            raise ValueError("routing rollout mode and contract versions contradict")
        expected_manifest_version = (
            "financial-manifest-v2"
            if self.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
            else "financial-manifest-v1"
        )
        if self.manifest_contract_version != expected_manifest_version:
            raise ValueError(
                "routing rollout mode and manifest contract version contradict"
            )
        indicator_bindings = (
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
            self.indicator_manifest_contract_version,
            self.indicator_selection_contract_version,
        )
        expected_indicator_bindings = (
            _indicator_contract_bindings_for_plan_version(self.plan_version)
        )
        if indicator_bindings != expected_indicator_bindings:
            raise ValueError("routing rollout indicator bindings contradict version")
        expected_temporal_bindings = {
            CAPABILITY_ROUTING_PLAN_VERSION_1_3: MainlandTemporalContractBindings(),
            CAPABILITY_ROUTING_PLAN_VERSION_1_4: MainlandTemporalContractBindingsV2(),
        }.get(self.plan_version)
        if self.temporal_contract_bindings != expected_temporal_bindings:
            raise ValueError("routing rollout temporal bindings contradict version")
        if self.mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW:
            historical_pre_v2 = (
                self.eligibility_policy_version is None
                and self.financial_eligibility_policy is None
                and self.financial_eligibility_binding is None
            )
            if not historical_pre_v2 and (
                self.eligibility_policy_version
                != MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
                or self.financial_eligibility_policy is None
                or self.financial_eligibility_policy.eligibility_policy_version
                != self.eligibility_policy_version
                or self.financial_eligibility_binding
                != MainlandFinancialEligibilityProjectionBinding.from_policy(
                    self.financial_eligibility_policy
                )
            ):
                raise ValueError(
                    "qualified shadow rollout requires the exact v2 eligibility policy"
                )
        elif (
            self.eligibility_policy_version is not None
            or self.financial_eligibility_policy is not None
            or self.financial_eligibility_binding is not None
        ):
            raise ValueError(
                "legacy and historical qualified rollouts cannot carry synthetic v2 policy data"
            )
        canonical_enablement = tuple(
            sorted(
                set(self.enabled_tushare_capabilities),
                key=tuple(TushareCapability).index,
            )
        )
        if self.enabled_tushare_capabilities != canonical_enablement:
            raise ValueError("routing rollout enablement must be canonical")
        if (
            self.mode is MainlandCapabilityRoutingMode.LEGACY
            and self.enabled_tushare_capabilities
        ):
            raise ValueError("legacy rollout cannot enable Tushare capabilities")
        expected = {
            MainlandCapabilityRoutingMode.LEGACY: (
                MainlandCapabilityRoutingDisposition.LEGACY_AUTHORITATIVE
            ),
            MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW: (
                MainlandCapabilityRoutingDisposition.SHADOW_NON_AUTHORITATIVE
            ),
            MainlandCapabilityRoutingMode.QUALIFIED_V1: (
                MainlandCapabilityRoutingDisposition.QUALIFIED_AUTHORITATIVE
            ),
        }[self.mode]
        if self.disposition is not expected:
            raise ValueError("routing rollout disposition contradicts its mode")
        if (
            self.mode is MainlandCapabilityRoutingMode.LEGACY
            and self.plan_signature != _legacy_plan_signature()
        ):
            raise ValueError("legacy rollout signature is not authoritative")
        return self


class MainlandCapabilityRoutingShadowFailureStage(str, Enum):
    CONTEXT_RESULT = "context_result"
    CONTEXT_EXCEPTION = "context_exception"
    DISPATCHER_CONSTRUCTION = "dispatcher_construction"
    REQUEST_ENRICHMENT = "request_enrichment"
    REQUEST_FACTORY = "request_factory"
    SELECTION_DISPATCH = "selection_dispatch"
    CHECKPOINT_PROJECTION = "checkpoint_projection"


class MainlandCapabilityRoutingShadowFailureKind(str, Enum):
    TYPED_RESULT = "typed_result"
    EXCEPTION = "exception"


class MainlandCapabilityRoutingShadowSelectionKind(str, Enum):
    STATEMENT = "statement"
    INDICATOR = "indicator"


class MainlandCapabilityRoutingShadowAcquisitionReason(str, Enum):
    COMPANY_TYPE_EVIDENCE_UNAVAILABLE = "company_type_evidence_unavailable"
    COMPANY_TYPE_UNRESOLVED = "company_type_unresolved"
    COMPANY_TYPE_AMBIGUOUS = "company_type_ambiguous"
    COMPANY_TYPE_CONTRADICTORY = "company_type_contradictory"
    LISTING_PROVENANCE_UNAVAILABLE = "listing_provenance_unavailable"
    TEMPORAL_PROOF_UNAVAILABLE = "temporal_proof_unavailable"
    TEMPORAL_ELIGIBILITY_REJECTED = "temporal_eligibility_rejected"
    CONTEXT_BINDING_MISMATCH = "context_binding_mismatch"
    INTERRUPTED_ACQUISITION = "interrupted_acquisition"
    UNKNOWN = "unknown"


class MainlandCapabilityRoutingShadowTemporalRejectionReason(str, Enum):
    REQUESTED_SESSION_NOT_OPEN_IN_PINNED_CALENDAR = (
        "requested_session_not_open_in_pinned_calendar"
    )
    CALENDAR_ELIGIBILITY_BINDING_MISMATCH = (
        "calendar_eligibility_binding_mismatch"
    )
    OBSERVATION_AFTER_RETRIEVAL = "observation_after_retrieval"
    EVIDENCE_EFFECTIVE_AS_OF_UNPROVEN = "evidence_effective_as_of_unproven"
    EVIDENCE_EFFECTIVE_AFTER_REQUESTED_SESSION = (
        "evidence_effective_after_requested_session"
    )
    ANN_DATE_MISSING = "ann_date_missing"
    ANN_DATE_AFTER_REQUESTED_SESSION = "ann_date_after_requested_session"
    RETRIEVAL_BEFORE_REQUESTED_SESSION_COMPLETED = (
        "retrieval_before_requested_session_completed"
    )
    RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION = (
        "retrieval_after_eligibility_evaluation"
    )
    REQUESTED_SESSION_NOT_LATEST_COMPLETED_FRONTIER = (
        "requested_session_not_latest_completed_frontier"
    )
    F_ANN_DATE_MISSING_FOR_STRICT_PIT = "f_ann_date_missing_for_strict_pit"
    F_ANN_DATE_AFTER_REQUESTED_SESSION = "f_ann_date_after_requested_session"
    EXACT_REVISION_NOT_AVAILABLE_BY_STRICT_CUTOFF = (
        "exact_revision_not_available_by_strict_cutoff"
    )
    TEMPORAL_EVIDENCE_ARTIFACT_BINDING_MISMATCH = (
        "temporal_evidence_artifact_binding_mismatch"
    )
    UNKNOWN_TEMPORAL_ELIGIBILITY_MODE = "unknown_temporal_eligibility_mode"
    ELIGIBILITY_EVALUATION_BEFORE_START = "eligibility_evaluation_before_start"
    OBSERVATION_BEFORE_ELIGIBILITY_START = "observation_before_eligibility_start"
    RETRIEVAL_BEFORE_ELIGIBILITY_START = "retrieval_before_eligibility_start"
    ACQUISITION_WINDOW_BINDING_MISMATCH = "acquisition_window_binding_mismatch"


class MainlandCapabilityRoutingShadowExceptionFamily(str, Enum):
    BUILTINS = "builtins"
    FINANCIAL_DISPATCH = "financial_dispatch"
    PYDANTIC = "pydantic"
    QUALIFIED_ROUTING_LIFECYCLE = "qualified_routing_lifecycle"
    UNKNOWN = "unknown"


class MainlandCapabilityRoutingShadowExceptionClass(str, Enum):
    FINANCIAL_DISPATCH_CHECKPOINT_ERROR = "FinancialDispatchCheckpointError"
    PRODUCTION_QUALIFIED_ROUTING_LIFECYCLE_ERROR = (
        "ProductionQualifiedRoutingLifecycleError"
    )
    RUNTIME_ERROR = "RuntimeError"
    VALUE_ERROR = "ValueError"
    TYPE_ERROR = "TypeError"
    ATTRIBUTE_ERROR = "AttributeError"
    KEY_ERROR = "KeyError"
    VALIDATION_ERROR = "ValidationError"
    UNKNOWN = "unknown"


class MainlandCapabilityRoutingShadowTypedReason(str, Enum):
    FINANCIAL_DISPATCH_CHECKPOINT_MALFORMED = (
        "financial_dispatch_checkpoint_malformed"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_UNSAFE_BOUNDARY = (
        "financial_dispatch_checkpoint_unsafe_boundary"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_POLICY_MISMATCH = (
        "financial_dispatch_checkpoint_policy_mismatch"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_PROVIDER_CHAIN_MISMATCH = (
        "financial_dispatch_checkpoint_provider_chain_mismatch"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_ASSET_CONFIGURATION_MISMATCH = (
        "financial_dispatch_checkpoint_asset_configuration_mismatch"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_CANONICAL_REQUEST_MISMATCH = (
        "financial_dispatch_checkpoint_canonical_request_mismatch"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_ARTIFACT_REFERENCE_INVALID = (
        "financial_dispatch_checkpoint_artifact_reference_invalid"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_BUDGET_PROGRESS_INVALID = (
        "financial_dispatch_checkpoint_budget_progress_invalid"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_TERMINAL_OUTCOME_INVALID = (
        "financial_dispatch_checkpoint_terminal_outcome_invalid"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_PROVIDER_SUBREQUEST_CACHE_INVALID = (
        "financial_dispatch_checkpoint_provider_subrequest_cache_invalid"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_QUALIFIED_SELECTION_INVALID = (
        "financial_dispatch_checkpoint_qualified_selection_invalid"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_MANIFEST_INVALID = (
        "financial_dispatch_checkpoint_manifest_invalid"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_ROUTING_PLAN_MISMATCH = (
        "financial_dispatch_checkpoint_routing_plan_mismatch"
    )
    FINANCIAL_DISPATCH_CHECKPOINT_RUN_SCOPE_MISMATCH = (
        "financial_dispatch_checkpoint_run_scope_mismatch"
    )
    FINANCIAL_ELIGIBILITY_POLICY_MISMATCH = "financial_eligibility_policy_mismatch"
    FINANCIAL_ELIGIBILITY_CONTEXT_MISSING = "financial_eligibility_context_missing"
    FINANCIAL_ELIGIBILITY_CONTEXT_VERSION_UNSUPPORTED = (
        "financial_eligibility_context_version_unsupported"
    )
    FINANCIAL_ELIGIBILITY_CONTEXT_IDENTITY_MISMATCH = (
        "financial_eligibility_context_identity_mismatch"
    )
    FINANCIAL_ELIGIBILITY_CONTEXT_INVALID = "financial_eligibility_context_invalid"
    FINANCIAL_ELIGIBILITY_CONTEXT_BINDING_MISMATCH = (
        "financial_eligibility_context_binding_mismatch"
    )
    FINANCIAL_ELIGIBILITY_CONTEXT_CACHE_MISMATCH = (
        "financial_eligibility_context_cache_mismatch"
    )
    FINANCIAL_ELIGIBILITY_CONTEXT_DISPATCHER_MISMATCH = (
        "financial_eligibility_context_dispatcher_mismatch"
    )
    FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_MISMATCH = (
        "financial_indicator_metadata_compatibility_mismatch"
    )
    FINANCIAL_TEMPORAL_COMPATIBILITY_MISMATCH = (
        "financial_temporal_compatibility_mismatch"
    )
    QUALIFIED_ROUTING_RUNTIME_BINDING_INVALID = (
        "qualified_routing_runtime_binding_invalid"
    )
    QUALIFIED_ROUTING_RUNTIME_CLOSED = "qualified_routing_runtime_closed"
    QUALIFIED_ROUTING_RUNTIME_DIFFERENT_RUN_ACTIVE = (
        "qualified_routing_runtime_different_run_active"
    )
    QUALIFIED_ROUTING_RUNTIME_INTERRUPTED = "qualified_routing_runtime_interrupted"
    QUALIFIED_ROUTING_ELIGIBILITY_CONTEXT_UNBOUND = (
        "qualified_routing_eligibility_context_unbound"
    )
    QUALIFIED_ROUTING_ELIGIBILITY_CONTEXT_BINDING_INVALID = (
        "qualified_routing_eligibility_context_binding_invalid"
    )


# Retain one slot for every closed stage. The total uses a fixed signed-64-bit
# scalar, which bounds serialization beyond any realizable in-memory request list.
MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_RETENTION_BOUND = len(
    MainlandCapabilityRoutingShadowFailureStage
)
MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_COUNT_MAX = (1 << 63) - 1


class MainlandCapabilityRoutingShadowFailureEntry(BaseModel):
    """One closed, sanitized, independently localized shadow failure."""

    model_config = _CLOSED_MODEL_CONFIG

    stage: MainlandCapabilityRoutingShadowFailureStage
    failure_kind: MainlandCapabilityRoutingShadowFailureKind
    typed_acquisition_reason: (
        MainlandCapabilityRoutingShadowAcquisitionReason | None
    ) = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    temporal_rejection_reasons: tuple[
        MainlandCapabilityRoutingShadowTemporalRejectionReason,
        ...,
    ] = Field(
        default=(),
        max_length=len(MainlandCapabilityRoutingShadowTemporalRejectionReason),
        exclude_if=lambda value: not value,
    )
    exception_family: MainlandCapabilityRoutingShadowExceptionFamily | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exception_class: MainlandCapabilityRoutingShadowExceptionClass | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    typed_reason: MainlandCapabilityRoutingShadowTypedReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    canonical_request_reference: str | None = Field(
        default=None,
        pattern=r"^financial-request:v1:[0-9a-f]{64}$",
        max_length=85,
        exclude_if=lambda value: value is None,
    )
    selection_kind: MainlandCapabilityRoutingShadowSelectionKind | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    context_constructed: bool
    dispatcher_constructed: bool

    @model_validator(mode="after")
    def _validate_entry(self) -> MainlandCapabilityRoutingShadowFailureEntry:
        if self.failure_kind is MainlandCapabilityRoutingShadowFailureKind.TYPED_RESULT:
            if (
                self.stage
                is not MainlandCapabilityRoutingShadowFailureStage.CONTEXT_RESULT
                or self.typed_acquisition_reason is None
                or self.exception_family is not None
                or self.exception_class is not None
                or self.typed_reason is not None
                or self.context_constructed
                or self.dispatcher_constructed
            ):
                raise ValueError("typed context-result shadow failure is inconsistent")
            canonical_temporal_reasons = tuple(
                sorted(
                    set(self.temporal_rejection_reasons),
                    key=lambda reason: reason.value,
                )
            )
            if self.temporal_rejection_reasons != canonical_temporal_reasons:
                raise ValueError(
                    "shadow temporal rejection reasons must be sorted and unique"
                )
            has_temporal_rejection = (
                self.typed_acquisition_reason
                is MainlandCapabilityRoutingShadowAcquisitionReason.TEMPORAL_ELIGIBILITY_REJECTED
            )
            if has_temporal_rejection != bool(self.temporal_rejection_reasons):
                raise ValueError("shadow temporal rejection detail is inconsistent")
            return self
        if (
            self.stage
            is MainlandCapabilityRoutingShadowFailureStage.CONTEXT_RESULT
            or self.typed_acquisition_reason is not None
            or self.temporal_rejection_reasons
            or self.exception_family is None
            or self.exception_class is None
        ):
            raise ValueError("exception shadow failure is inconsistent")
        expected_progress = {
            MainlandCapabilityRoutingShadowFailureStage.CONTEXT_EXCEPTION: (
                False,
                False,
            ),
            MainlandCapabilityRoutingShadowFailureStage.DISPATCHER_CONSTRUCTION: (
                True,
                False,
            ),
        }.get(self.stage)
        if expected_progress is not None and (
            self.context_constructed,
            self.dispatcher_constructed,
        ) != expected_progress:
            raise ValueError("shadow failure stage contradicts progress")
        if self.stage in {
            MainlandCapabilityRoutingShadowFailureStage.REQUEST_ENRICHMENT,
            MainlandCapabilityRoutingShadowFailureStage.REQUEST_FACTORY,
            MainlandCapabilityRoutingShadowFailureStage.SELECTION_DISPATCH,
            MainlandCapabilityRoutingShadowFailureStage.CHECKPOINT_PROJECTION,
        } and not self.dispatcher_constructed:
            raise ValueError("post-dispatcher shadow failure requires a dispatcher")
        allowed_exception_identities = {
            (
                MainlandCapabilityRoutingShadowExceptionFamily.BUILTINS,
                MainlandCapabilityRoutingShadowExceptionClass.RUNTIME_ERROR,
            ),
            (
                MainlandCapabilityRoutingShadowExceptionFamily.BUILTINS,
                MainlandCapabilityRoutingShadowExceptionClass.VALUE_ERROR,
            ),
            (
                MainlandCapabilityRoutingShadowExceptionFamily.BUILTINS,
                MainlandCapabilityRoutingShadowExceptionClass.TYPE_ERROR,
            ),
            (
                MainlandCapabilityRoutingShadowExceptionFamily.BUILTINS,
                MainlandCapabilityRoutingShadowExceptionClass.ATTRIBUTE_ERROR,
            ),
            (
                MainlandCapabilityRoutingShadowExceptionFamily.BUILTINS,
                MainlandCapabilityRoutingShadowExceptionClass.KEY_ERROR,
            ),
            (
                MainlandCapabilityRoutingShadowExceptionFamily.PYDANTIC,
                MainlandCapabilityRoutingShadowExceptionClass.VALIDATION_ERROR,
            ),
            (
                MainlandCapabilityRoutingShadowExceptionFamily.FINANCIAL_DISPATCH,
                MainlandCapabilityRoutingShadowExceptionClass.FINANCIAL_DISPATCH_CHECKPOINT_ERROR,
            ),
            (
                MainlandCapabilityRoutingShadowExceptionFamily.QUALIFIED_ROUTING_LIFECYCLE,
                MainlandCapabilityRoutingShadowExceptionClass.PRODUCTION_QUALIFIED_ROUTING_LIFECYCLE_ERROR,
            ),
            (
                MainlandCapabilityRoutingShadowExceptionFamily.UNKNOWN,
                MainlandCapabilityRoutingShadowExceptionClass.UNKNOWN,
            ),
        }
        if (self.exception_family, self.exception_class) not in (
            allowed_exception_identities
        ):
            raise ValueError("shadow exception family and class are inconsistent")
        typed_exception = self.exception_family in {
            MainlandCapabilityRoutingShadowExceptionFamily.FINANCIAL_DISPATCH,
            MainlandCapabilityRoutingShadowExceptionFamily.QUALIFIED_ROUTING_LIFECYCLE,
        }
        if self.typed_reason is not None and not typed_exception:
            raise ValueError("shadow exception typed reason is inconsistent")
        if self.typed_reason is not None:
            expected_family = (
                MainlandCapabilityRoutingShadowExceptionFamily.FINANCIAL_DISPATCH
                if self.typed_reason.value.startswith("financial_")
                else MainlandCapabilityRoutingShadowExceptionFamily.QUALIFIED_ROUTING_LIFECYCLE
            )
            if self.exception_family is not expected_family:
                raise ValueError("shadow exception typed reason family is inconsistent")
        return self


class MainlandCapabilityRoutingShadowFailure(BaseModel):
    """Versioned, bounded non-authoritative shadow-failure projection."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0", "2.0", "2.1"] = "2.1"
    diagnostic_code: Literal["shadow_dispatch_failed"] = "shadow_dispatch_failed"
    failure_count: int = Field(
        ge=1,
        le=MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_COUNT_MAX,
    )
    retained_failure_count: int | None = Field(
        default=None,
        ge=1,
        le=MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_RETENTION_BOUND,
        exclude_if=lambda value: value is None,
    )
    dropped_failure_count: int | None = Field(
        default=None,
        ge=0,
        le=MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_COUNT_MAX,
        exclude_if=lambda value: value is None,
    )
    failures: tuple[MainlandCapabilityRoutingShadowFailureEntry, ...] = Field(
        default=(),
        max_length=MAINLAND_CAPABILITY_ROUTING_SHADOW_FAILURE_RETENTION_BOUND,
        exclude_if=lambda value: not value,
    )
    stage: MainlandCapabilityRoutingShadowFailureStage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    failure_kind: MainlandCapabilityRoutingShadowFailureKind | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    typed_acquisition_reason: (
        MainlandCapabilityRoutingShadowAcquisitionReason | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    temporal_rejection_reasons: tuple[
        MainlandCapabilityRoutingShadowTemporalRejectionReason,
        ...,
    ] = Field(
        default=(),
        max_length=len(MainlandCapabilityRoutingShadowTemporalRejectionReason),
        exclude_if=lambda value: not value,
    )
    exception_family: MainlandCapabilityRoutingShadowExceptionFamily | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exception_class: MainlandCapabilityRoutingShadowExceptionClass | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    typed_reason: MainlandCapabilityRoutingShadowTypedReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    canonical_request_reference: str | None = Field(
        default=None,
        pattern=r"^financial-request:v1:[0-9a-f]{64}$",
        max_length=85,
        exclude_if=lambda value: value is None,
    )
    selection_kind: MainlandCapabilityRoutingShadowSelectionKind | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    context_constructed: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    dispatcher_constructed: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    def _legacy_entry_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "failure_kind": self.failure_kind,
            "typed_acquisition_reason": self.typed_acquisition_reason,
            "temporal_rejection_reasons": self.temporal_rejection_reasons,
            "exception_family": self.exception_family,
            "exception_class": self.exception_class,
            "typed_reason": self.typed_reason,
            "canonical_request_reference": self.canonical_request_reference,
            "selection_kind": self.selection_kind,
            "context_constructed": self.context_constructed,
            "dispatcher_constructed": self.dispatcher_constructed,
        }

    @model_validator(mode="after")
    def _validate_versioned_projection(self) -> MainlandCapabilityRoutingShadowFailure:
        legacy_detail = self._legacy_entry_payload()
        has_legacy_detail = any(
            value is not None and value != () for value in legacy_detail.values()
        )
        has_collection_detail = bool(self.failures) or any(
            value is not None
            for value in (
                self.retained_failure_count,
                self.dropped_failure_count,
            )
        )
        if self.contract_version == "1.0":
            if has_legacy_detail or has_collection_detail:
                raise ValueError("shadow failure v1 cannot carry localized detail")
            return self
        if self.contract_version == "2.0":
            if has_collection_detail:
                raise ValueError("shadow failure v2.0 cannot carry a failure collection")
            MainlandCapabilityRoutingShadowFailureEntry.model_validate(
                legacy_detail
            )
            return self
        if has_legacy_detail:
            raise ValueError("shadow failure v2.1 cannot carry legacy single detail")
        if (
            self.retained_failure_count is None
            or self.dropped_failure_count is None
            or len(self.failures) != self.retained_failure_count
            or self.failure_count
            != self.retained_failure_count + self.dropped_failure_count
        ):
            raise ValueError("shadow failure v2.1 accounting is inconsistent")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> MainlandCapabilityRoutingShadowFailure:
        """Preserve Pydantic's copy API while revalidating every update."""

        payload = self.model_dump(mode="python")
        if update:
            payload.update(update)
        if deep:
            import copy

            payload = copy.deepcopy(payload)
        return type(self).model_validate(payload)

    def localized_failures(
        self,
    ) -> tuple[MainlandCapabilityRoutingShadowFailureEntry, ...]:
        """Return only versioned localized entries; never reinterpret v1."""

        if self.contract_version == "2.1":
            return self.failures
        if self.contract_version == "2.0":
            return (
                MainlandCapabilityRoutingShadowFailureEntry.model_validate(
                    self._legacy_entry_payload()
                ),
            )
        return ()


class MainlandCapabilityRoutingCheckpointAction(str, Enum):
    RESUME = "resume"
    START_NEW = "start_new"


class MainlandCapabilityRoutingCheckpointFailureReason(str, Enum):
    UNVERSIONED_LEGACY_REQUIRES_NEW_RUN = "unversioned_legacy_requires_new_run"
    LEGACY_PLAN_REQUIRES_NEW_RUN = "legacy_plan_requires_new_run"
    QUALIFIED_PLAN_DOWNGRADE_FORBIDDEN = "qualified_plan_downgrade_forbidden"
    ROLLOUT_MODE_MISMATCH = "rollout_mode_mismatch"
    PLAN_MISMATCH = "plan_mismatch"
    TAMPERED_PROJECTION = "tampered_projection"
    FINANCIAL_ELIGIBILITY_POLICY_MISMATCH = (
        "financial_eligibility_policy_mismatch"
    )
    INDICATOR_METADATA_COMPATIBILITY_MISMATCH = (
        "indicator_metadata_compatibility_mismatch"
    )
    TEMPORAL_COMPATIBILITY_MISMATCH = "temporal_compatibility_mismatch"


class MainlandCapabilityRoutingCheckpointError(ValueError):
    """Typed, secret-free refusal to reuse an incompatible checkpoint."""

    def __init__(
        self,
        reason: MainlandCapabilityRoutingCheckpointFailureReason,
    ) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class MainlandCapabilityRoutingCheckpointCompatibility(BaseModel):
    """Pure resume decision that never rewrites historical checkpoint state."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = "1.0"
    action: MainlandCapabilityRoutingCheckpointAction
    reason: MainlandCapabilityRoutingCheckpointFailureReason | None = None
    active: MainlandCapabilityRoutingRunProjection
    checkpoint: MainlandCapabilityRoutingRunProjection | None
    previous_run_id: str | None = Field(
        default=None,
        pattern=r"^run:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    new_run_id: str | None = Field(
        default=None,
        pattern=r"^run:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    provider_io_performed: Literal[False] = False
    checkpoint_rewrite_permitted: Literal[False] = False
    historical_state_preserved: Literal[True] = True

    @model_validator(mode="after")
    def _validate_action(self) -> MainlandCapabilityRoutingCheckpointCompatibility:
        if self.action is MainlandCapabilityRoutingCheckpointAction.RESUME:
            if (
                self.reason is not None
                or self.checkpoint is None
                or self.active != self.checkpoint
                or self.previous_run_id is not None
                or self.new_run_id is not None
            ):
                raise ValueError("resume requires an exact routing plan match")
        elif (
            self.reason
            is MainlandCapabilityRoutingCheckpointFailureReason.UNVERSIONED_LEGACY_REQUIRES_NEW_RUN
        ):
            if self.checkpoint is not None:
                raise ValueError("unversioned legacy new-run state has no projection")
        elif (
            self.reason
            is MainlandCapabilityRoutingCheckpointFailureReason.LEGACY_PLAN_REQUIRES_NEW_RUN
        ):
            if (
                self.checkpoint is None
                or self.checkpoint.mode is not MainlandCapabilityRoutingMode.LEGACY
                or self.active.mode is MainlandCapabilityRoutingMode.LEGACY
            ):
                raise ValueError("legacy new-run state requires a qualified active plan")
        elif self.reason is (
            MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
        ):
            if (
                self.checkpoint is None
                or self.checkpoint.mode is MainlandCapabilityRoutingMode.LEGACY
                or self.active.financial_eligibility_binding is None
                or self.previous_run_id is None
                or self.new_run_id is None
                or self.previous_run_id == self.new_run_id
            ):
                raise ValueError(
                    "qualified-mismatch new-run state requires distinct run identities"
                )
        elif self.reason is (
            MainlandCapabilityRoutingCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
        ):
            if (
                self.checkpoint is None
                or self.checkpoint.mode is MainlandCapabilityRoutingMode.LEGACY
                or self.active.mode is MainlandCapabilityRoutingMode.LEGACY
                or self.active.plan_version != CAPABILITY_ROUTING_PLAN_VERSION
                or self.checkpoint.plan_version == CAPABILITY_ROUTING_PLAN_VERSION
                or self.active.indicator_field_semantics_version is None
                or self.active.indicator_metadata_compatibility_version is None
                or self.previous_run_id is None
                or self.new_run_id is None
                or self.previous_run_id == self.new_run_id
            ):
                raise ValueError(
                    "indicator-mismatch new-run state requires distinct run identities"
                )
        elif self.reason is (
            MainlandCapabilityRoutingCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
        ):
            if (
                self.checkpoint is None
                or self.checkpoint.mode is MainlandCapabilityRoutingMode.LEGACY
                or self.active.mode is MainlandCapabilityRoutingMode.LEGACY
                or self.active.plan_version
                not in {
                    CAPABILITY_ROUTING_PLAN_VERSION_1_3,
                    CAPABILITY_ROUTING_PLAN_VERSION_1_4,
                }
                or self.checkpoint.plan_version == self.active.plan_version
                or self.active.temporal_contract_bindings is None
                or self.previous_run_id is None
                or self.new_run_id is None
                or self.previous_run_id == self.new_run_id
            ):
                raise ValueError(
                    "temporal-mismatch new-run state requires distinct run identities"
                )
        else:
            raise ValueError("new-run routing decision requires a legacy mismatch")
        if self.reason not in {
            MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH,
            MainlandCapabilityRoutingCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH,
            MainlandCapabilityRoutingCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        } and (
            self.previous_run_id is not None or self.new_run_id is not None
        ):
            raise ValueError("legacy new-run compatibility cannot relabel run identity")
        return self


def validate_mainland_capability_routing_checkpoint(
    *,
    active_plan: MainlandCapabilityRoutingPlan,
    checkpoint_plan: Mapping[str, Any] | MainlandCapabilityRoutingPlan | None,
) -> MainlandCapabilityRoutingCheckpointCompatibility:
    """Classify resume compatibility without provider I/O or state mutation."""

    active_projection = MainlandCapabilityRoutingRunProjection.from_plan(active_plan)
    if checkpoint_plan is None:
        return MainlandCapabilityRoutingCheckpointCompatibility(
            action=MainlandCapabilityRoutingCheckpointAction.START_NEW,
            reason=(
                MainlandCapabilityRoutingCheckpointFailureReason.UNVERSIONED_LEGACY_REQUIRES_NEW_RUN
            ),
            active=active_projection,
            checkpoint=None,
        )
    if (
        active_plan.eligibility_policy_version
        == MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
        and isinstance(checkpoint_plan, Mapping)
    ):
        checkpoint_identity = checkpoint_plan.get("eligibility_policy_version")
        checkpoint_mode = checkpoint_plan.get("mode")
        checkpoint_policy = checkpoint_plan.get("financial_eligibility_policy")
        nested_identity = (
            checkpoint_policy.get("eligibility_policy_version")
            if isinstance(checkpoint_policy, Mapping)
            else None
        )
        checkpoint_binding = checkpoint_plan.get("financial_eligibility_binding")
        if (
            checkpoint_mode != MainlandCapabilityRoutingMode.LEGACY.value
            and (
                checkpoint_identity != MAINLAND_FINANCIAL_ELIGIBILITY_POLICY_V2
                or nested_identity != checkpoint_identity
                or active_plan.financial_eligibility_binding is None
                or checkpoint_binding
                != active_plan.financial_eligibility_binding.model_dump(mode="json")
            )
        ):
            raise MainlandCapabilityRoutingCheckpointError(
                MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
            )
    try:
        restored = MainlandCapabilityRoutingPlan.model_validate(checkpoint_plan)
    except (TypeError, ValueError) as exc:
        raise MainlandCapabilityRoutingCheckpointError(
            MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
        ) from exc
    checkpoint_projection = MainlandCapabilityRoutingRunProjection.from_plan(restored)
    if (
        restored.mode is not MainlandCapabilityRoutingMode.LEGACY
        and restored.eligibility_policy_version
        != active_plan.eligibility_policy_version
    ):
        raise MainlandCapabilityRoutingCheckpointError(
            MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
        )
    if (
        active_plan.plan_version
        in {CAPABILITY_ROUTING_PLAN_VERSION_1_3, CAPABILITY_ROUTING_PLAN_VERSION_1_4}
        and restored.plan_version != active_plan.plan_version
        and restored.mode is not MainlandCapabilityRoutingMode.LEGACY
        and active_plan.mode is not MainlandCapabilityRoutingMode.LEGACY
    ):
        raise MainlandCapabilityRoutingCheckpointError(
            MainlandCapabilityRoutingCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
        )
    if (
        active_plan.plan_version == CAPABILITY_ROUTING_PLAN_VERSION
        and restored.plan_version != CAPABILITY_ROUTING_PLAN_VERSION
        and restored.mode is not MainlandCapabilityRoutingMode.LEGACY
        and active_plan.mode is not MainlandCapabilityRoutingMode.LEGACY
    ):
        raise MainlandCapabilityRoutingCheckpointError(
            MainlandCapabilityRoutingCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
        )
    if restored == active_plan:
        return MainlandCapabilityRoutingCheckpointCompatibility(
            action=MainlandCapabilityRoutingCheckpointAction.RESUME,
            active=active_projection,
            checkpoint=checkpoint_projection,
        )
    if (
        restored.mode is MainlandCapabilityRoutingMode.LEGACY
        and active_plan.mode is not MainlandCapabilityRoutingMode.LEGACY
    ):
        return MainlandCapabilityRoutingCheckpointCompatibility(
            action=MainlandCapabilityRoutingCheckpointAction.START_NEW,
            reason=(
                MainlandCapabilityRoutingCheckpointFailureReason.LEGACY_PLAN_REQUIRES_NEW_RUN
            ),
            active=active_projection,
            checkpoint=checkpoint_projection,
        )
    if (
        restored.mode is not MainlandCapabilityRoutingMode.LEGACY
        and active_plan.mode is MainlandCapabilityRoutingMode.LEGACY
    ):
        raise MainlandCapabilityRoutingCheckpointError(
            MainlandCapabilityRoutingCheckpointFailureReason.QUALIFIED_PLAN_DOWNGRADE_FORBIDDEN
        )
    if restored.mode is not active_plan.mode:
        raise MainlandCapabilityRoutingCheckpointError(
            MainlandCapabilityRoutingCheckpointFailureReason.ROLLOUT_MODE_MISMATCH
        )
    raise MainlandCapabilityRoutingCheckpointError(
        MainlandCapabilityRoutingCheckpointFailureReason.PLAN_MISMATCH
    )


def start_new_mainland_capability_routing_run(
    *,
    active_plan: MainlandCapabilityRoutingPlan,
    checkpoint_plan: Mapping[str, Any] | MainlandCapabilityRoutingPlan,
    previous_run_id: str,
    new_run_id: str,
) -> MainlandCapabilityRoutingCheckpointCompatibility:
    """Explicitly classify a distinct run after a typed qualified mismatch."""

    mismatch_reason: MainlandCapabilityRoutingCheckpointFailureReason
    try:
        validate_mainland_capability_routing_checkpoint(
            active_plan=active_plan,
            checkpoint_plan=checkpoint_plan,
        )
    except MainlandCapabilityRoutingCheckpointError as exc:
        if exc.reason not in {
            MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH,
            MainlandCapabilityRoutingCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH,
            MainlandCapabilityRoutingCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH,
        }:
            raise
        mismatch_reason = exc.reason
    else:
        raise ValueError("START_NEW requires a financial eligibility-policy mismatch")
    try:
        restored = MainlandCapabilityRoutingPlan.model_validate(checkpoint_plan)
    except (TypeError, ValueError) as exc:
        raise MainlandCapabilityRoutingCheckpointError(
            MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
        ) from exc
    return MainlandCapabilityRoutingCheckpointCompatibility(
        action=MainlandCapabilityRoutingCheckpointAction.START_NEW,
        reason=mismatch_reason,
        active=MainlandCapabilityRoutingRunProjection.from_plan(active_plan),
        checkpoint=MainlandCapabilityRoutingRunProjection.from_plan(restored),
        previous_run_id=previous_run_id,
        new_run_id=new_run_id,
    )


class MainlandCapabilityRoutingFailure(BaseModel):
    """Typed, payload-free configuration blocker for Evidence Preflight."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = "1.0"
    reason: MainlandCapabilityRoutingFailureReason
    diagnostic_code: MainlandCapabilityRoutingFailureReason
    analysis_outcome: AnalysisOutcome

    @model_validator(mode="after")
    def _validate_failure(self) -> MainlandCapabilityRoutingFailure:
        if self.reason is not self.diagnostic_code:
            raise ValueError("routing failure diagnostic must match its reason")
        if self.analysis_outcome.reason is not AnalysisOutcomeReason.PREFLIGHT_BLOCKED:
            raise ValueError("routing failure must publish a preflight Analysis Outcome")
        return self


class MainlandCapabilityRoutingPreflightError(ValueError):
    """Typed pre-construction stop carrying only the safe failure contract."""

    def __init__(
        self,
        failure: MainlandCapabilityRoutingFailure,
        *,
        message: str | None = None,
    ) -> None:
        self.failure = failure
        super().__init__(message or failure.reason.value)


class MainlandCapabilityRoutingPreflight(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = "1.0"
    passed: bool
    plan: MainlandCapabilityRoutingPlan | None = None
    failure: MainlandCapabilityRoutingFailure | None = None

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> MainlandCapabilityRoutingPreflight:
        if self.passed != (self.plan is not None and self.failure is None):
            raise ValueError("routing preflight must contain exactly one terminal result")
        return self


def _plan_signature(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{_PLAN_SIGNATURE_PREFIX}{sha256(encoded).hexdigest()}"


def is_mainland_equity_configuration(
    asset_configuration: RunAssetConfiguration | None,
) -> bool:
    return (
        asset_configuration is not None
        and
        asset_configuration.instrument_kind is InstrumentKind.EQUITY
        and asset_configuration.reference_market in {"XSHG", "XSHE"}
    )


def capability_routing_configuration_is_explicit(
    config: Mapping[str, Any],
) -> bool:
    return (
        str(config.get("mainland_capability_routing_mode", "legacy")) != "legacy"
        or bool(config.get("tushare_enabled_capabilities", ()))
    )


def _enabled_capabilities(value: object) -> tuple[TushareCapability, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        raw_values = tuple(item.strip() for item in value.split(",") if item.strip())
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_values = tuple(value)
    else:
        raise ValueError("Tushare capability enablement must be a collection")
    enabled = {TushareCapability(str(value)) for value in raw_values}
    return tuple(capability for capability in TushareCapability if capability in enabled)


def _positive_rate(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    if not 1 <= value <= 200:
        raise ValueError(f"{key} must be between 1 and 200")
    return value


def _blocked(
    reason: MainlandCapabilityRoutingFailureReason,
) -> MainlandCapabilityRoutingPreflight:
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
        diagnostic_codes=(AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID,),
    )
    return MainlandCapabilityRoutingPreflight(
        passed=False,
        failure=MainlandCapabilityRoutingFailure(
            reason=reason,
            diagnostic_code=reason,
            analysis_outcome=outcome,
        ),
    )


def mainland_capability_routing_failure(
    reason: MainlandCapabilityRoutingFailureReason,
) -> MainlandCapabilityRoutingFailure:
    """Build one public, typed, secret-free activation failure."""

    result = _blocked(reason)
    if result.failure is None:  # pragma: no cover - closed model invariant
        raise RuntimeError("routing failure projection is unavailable")
    return result.failure


def _routes(
    mode: MainlandCapabilityRoutingMode,
    enabled: tuple[TushareCapability, ...],
    *,
    plan_version: str,
) -> tuple[MainlandCapabilityRoute, ...]:
    if mode is MainlandCapabilityRoutingMode.LEGACY:
        providers = {
            MainlandCapability.DAILY_MARKET_SNAPSHOT: (
                "akshare",
                "baostock",
                "yfinance",
            ),
            MainlandCapability.BALANCE_SHEET: ("akshare", "yfinance", "baostock"),
            MainlandCapability.INCOME_STATEMENT: (
                "akshare",
                "yfinance",
                "baostock",
            ),
            MainlandCapability.CASH_FLOW: ("akshare", "yfinance", "baostock"),
            MainlandCapability.FINANCIAL_INDICATORS: (
                "akshare",
                "yfinance",
                "baostock",
            ),
            MainlandCapability.ADJUSTMENT_FACTORS: (
                "baostock",
                "akshare",
                "yfinance_derived",
            ),
            MainlandCapability.SUSPENSION_STATUS: ("baostock",),
            MainlandCapability.NAME_EVENTS: ("akshare",),
            MainlandCapability.ISSUER_LIFECYCLE: ("baostock",),
        }
    else:
        statements = TushareCapability.STATEMENTS in enabled
        indicators = TushareCapability.FINANCIAL_INDICATORS in enabled
        factors = TushareCapability.ADJUSTMENT_FACTORS in enabled
        names = TushareCapability.NAME_EVENTS in enabled
        providers = {
            MainlandCapability.DAILY_MARKET_SNAPSHOT: (
                "akshare",
                "baostock",
                "yfinance",
            ),
            MainlandCapability.BALANCE_SHEET: (
                *(("tushare",) if statements else ()),
                "akshare_sina",
                "yfinance",
            ),
            MainlandCapability.INCOME_STATEMENT: (
                *(("tushare",) if statements else ()),
                "akshare_sina",
                "yfinance",
            ),
            MainlandCapability.CASH_FLOW: (
                *(("tushare",) if statements else ()),
                "akshare_sina",
                "yfinance",
            ),
            MainlandCapability.FINANCIAL_INDICATORS: (
                *(("tushare",) if indicators else ()),
                "akshare",
                "baostock_qualified_families",
                "yfinance",
            ),
            MainlandCapability.ADJUSTMENT_FACTORS: (
                "baostock",
                *(("tushare",) if factors else ()),
                "akshare",
                "yfinance_derived",
            ),
            MainlandCapability.SUSPENSION_STATUS: ("baostock",),
            MainlandCapability.NAME_EVENTS: (
                *(("tushare",) if names else ()),
                "akshare",
            ),
            MainlandCapability.ISSUER_LIFECYCLE: ("baostock",),
        }
        if plan_version in {
            CAPABILITY_ROUTING_PLAN_VERSION_V2,
            CAPABILITY_ROUTING_PLAN_VERSION,
            CAPABILITY_ROUTING_PLAN_VERSION_1_3,
            CAPABILITY_ROUTING_PLAN_VERSION_1_4,
        }:
            providers[MainlandCapability.FINANCIAL_COMPANY_TYPE_EVIDENCE] = (
                "tushare",
                "akshare_sina",
                "yfinance",
            )
    capabilities = _capabilities_for_plan_version(plan_version)
    return tuple(
        MainlandCapabilityRoute(capability=capability, providers=providers[capability])
        for capability in capabilities
    )


def _endpoint_ids(
    enabled: tuple[TushareCapability, ...],
) -> tuple[str, ...]:
    endpoints: set[str] = set()
    if TushareCapability.STATEMENTS in enabled:
        endpoints.update(("income", "balancesheet", "cashflow"))
    if TushareCapability.FINANCIAL_INDICATORS in enabled:
        endpoints.add("fina_indicator")
    if TushareCapability.ADJUSTMENT_FACTORS in enabled:
        endpoints.add("adj_factor")
    if TushareCapability.NAME_EVENTS in enabled:
        endpoints.add("namechange")
    return tuple(sorted(endpoints))


def _build_plan(
    *,
    mode: MainlandCapabilityRoutingMode,
    enabled: tuple[TushareCapability, ...],
    qualification_profile: str,
    account_scope_label: str,
    calls_per_minute: int,
    operator_calls_per_minute: int,
    plan_version: str | None = None,
) -> MainlandCapabilityRoutingPlan:
    effective_plan_version = plan_version or (
        CAPABILITY_ROUTING_PLAN_VERSION_V1
        if mode is MainlandCapabilityRoutingMode.LEGACY
        else CAPABILITY_ROUTING_PLAN_VERSION
    )
    plan_capabilities = _capabilities_for_plan_version(effective_plan_version)
    normalizer_version, completeness_policy_version, selection_contract_version = (
        _contract_versions_for_mode(mode)
    )
    payload: dict[str, Any] = {
        "plan_version": effective_plan_version,
        "policy_version": CAPABILITY_ROUTING_POLICY_VERSION,
        "mode": mode.value,
        "qualification_profile": qualification_profile,
        "routes": [
            route.model_dump(mode="json")
            for route in _routes(
                mode,
                enabled,
                plan_version=effective_plan_version,
            )
        ],
        "enabled_tushare_capabilities": [item.value for item in enabled],
        "normalizer_version": normalizer_version,
        "completeness_policy_version": completeness_policy_version,
        "account_scope_label": account_scope_label,
        "endpoint_pacing_identities": [
            TushareEndpointPacingIdentity(
                endpoint_id=endpoint_id,
                calls_per_minute=calls_per_minute,
                operator_calls_per_minute=operator_calls_per_minute,
            ).model_dump(mode="json")
            for endpoint_id in _endpoint_ids(enabled)
        ],
        "request_budget_identities": [
            MainlandRequestBudgetIdentity(
                capability=capability,
                budget_identity=f"foreground-{capability.value}-budget-v1",
            ).model_dump(mode="json")
            for capability in plan_capabilities
        ],
        "artifact_contract_version": "source-artifact-v1",
        "manifest_contract_version": (
            "financial-manifest-v2"
            if mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
            else "financial-manifest-v1"
        ),
        "selection_contract_version": selection_contract_version,
        "degradation_contract_version": "financial-degradation-v1",
    }
    if mode is MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW:
        eligibility_policy = build_mainland_financial_eligibility_policy_v2()
        eligibility_binding = (
            MainlandFinancialEligibilityProjectionBinding.from_policy(
                eligibility_policy
            )
        )
        payload.update(
            eligibility_policy_version=(
                eligibility_policy.eligibility_policy_version
            ),
            financial_eligibility_policy=eligibility_policy.model_dump(mode="json"),
            financial_eligibility_binding=eligibility_binding.model_dump(mode="json"),
        )
    if effective_plan_version in {
        CAPABILITY_ROUTING_PLAN_VERSION,
        CAPABILITY_ROUTING_PLAN_VERSION_1_3,
        CAPABILITY_ROUTING_PLAN_VERSION_1_4,
    }:
        (
            field_semantics_version,
            metadata_compatibility_version,
            indicator_manifest_version,
            indicator_selection_version,
        ) = _indicator_contract_bindings_for_plan_version(effective_plan_version)
        payload.update(
            indicator_field_semantics_version=field_semantics_version,
            indicator_metadata_compatibility_version=metadata_compatibility_version,
            indicator_manifest_contract_version=indicator_manifest_version,
            indicator_selection_contract_version=indicator_selection_version,
        )
    if effective_plan_version in {
        CAPABILITY_ROUTING_PLAN_VERSION_1_3,
        CAPABILITY_ROUTING_PLAN_VERSION_1_4,
    }:
        bindings = (
            MainlandTemporalContractBindingsV2()
            if effective_plan_version == CAPABILITY_ROUTING_PLAN_VERSION_1_4
            else MainlandTemporalContractBindings()
        )
        payload["temporal_contract_bindings"] = bindings.model_dump(mode="json")
    return MainlandCapabilityRoutingPlan(
        **payload,
        plan_signature=_plan_signature(payload),
    )


def prepare_mainland_temporal_capability_routing_plan(
    predecessor: MainlandCapabilityRoutingPlan,
) -> MainlandCapabilityRoutingPlan:
    """Prepare latest plan 1.4 bindings without acquisition or persistence."""

    if (
        predecessor.plan_version != CAPABILITY_ROUTING_PLAN_VERSION
        or predecessor.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
    ):
        raise ValueError("temporal plan preparation requires qualified plan 1.2")
    payload = predecessor.signature_payload()
    payload["plan_version"] = CAPABILITY_ROUTING_PLAN_VERSION_1_4
    payload["temporal_contract_bindings"] = (
        MainlandTemporalContractBindingsV2().model_dump(mode="json")
    )
    return MainlandCapabilityRoutingPlan(
        **payload,
        plan_signature=_plan_signature(payload),
    )


def prepare_mainland_temporal_capability_routing_plan_v1(
    predecessor: MainlandCapabilityRoutingPlan,
) -> MainlandCapabilityRoutingPlan:
    """Reconstruct frozen plan 1.3 for historical contract tests/readers."""

    if (
        predecessor.plan_version != CAPABILITY_ROUTING_PLAN_VERSION
        or predecessor.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW
    ):
        raise ValueError("temporal v1 plan preparation requires qualified plan 1.2")
    payload = predecessor.signature_payload()
    payload["plan_version"] = CAPABILITY_ROUTING_PLAN_VERSION_1_3
    payload["temporal_contract_bindings"] = (
        MainlandTemporalContractBindings().model_dump(mode="json")
    )
    return MainlandCapabilityRoutingPlan(
        **payload,
        plan_signature=_plan_signature(payload),
    )


def _legacy_plan_signature() -> str:
    return _build_plan(
        mode=MainlandCapabilityRoutingMode.LEGACY,
        enabled=(),
        qualification_profile="legacy",
        account_scope_label="not-applicable",
        calls_per_minute=40,
        operator_calls_per_minute=40,
        plan_version=CAPABILITY_ROUTING_PLAN_VERSION_V1,
    ).plan_signature


def preflight_mainland_capability_routing(
    asset_configuration: RunAssetConfiguration | None,
    *,
    config: Mapping[str, Any],
    environment: Mapping[str, str] | None = None,
) -> MainlandCapabilityRoutingPreflight:
    """Resolve a network-free routing plan after authoritative identity."""

    try:
        mode = MainlandCapabilityRoutingMode(
            str(config.get("mainland_capability_routing_mode", "legacy"))
        )
    except ValueError:
        return _blocked(
            MainlandCapabilityRoutingFailureReason.INVALID_ROUTING_MODE
        )
    try:
        enabled = _enabled_capabilities(
            config.get("tushare_enabled_capabilities", ())
        )
    except (TypeError, ValueError):
        return _blocked(
            MainlandCapabilityRoutingFailureReason.UNSUPPORTED_TUSHARE_CAPABILITY
        )
    if mode is MainlandCapabilityRoutingMode.LEGACY and enabled:
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_REQUIRES_QUALIFIED_V1
        )
    if not is_mainland_equity_configuration(asset_configuration):
        return _blocked(
            MainlandCapabilityRoutingFailureReason.INSTRUMENT_NOT_MAINLAND_EQUITY
        )
    if mode is MainlandCapabilityRoutingMode.QUALIFIED_V1:
        return _blocked(
            MainlandCapabilityRoutingFailureReason.QUALIFIED_V1_AUTHORITY_DISABLED
        )
    if mode is MainlandCapabilityRoutingMode.LEGACY:
        return MainlandCapabilityRoutingPreflight(
            passed=True,
            plan=_build_plan(
                mode=mode,
                enabled=(),
                qualification_profile="legacy",
                account_scope_label="not-applicable",
                calls_per_minute=40,
                operator_calls_per_minute=40,
            )
        )

    qualification_profile = str(config.get("tushare_qualification_profile", ""))
    if qualification_profile != QUALIFICATION_PROFILE:
        return _blocked(
            MainlandCapabilityRoutingFailureReason.QUALIFICATION_PROFILE_MISMATCH
        )
    effective_environment = os.environ if environment is None else environment
    token_value = str(effective_environment.get("TUSHARE_TOKEN", "")).strip()
    if enabled and not token_value:
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_TOKEN_MISSING
        )
    if enabled and importlib.util.find_spec("tushare") is None:
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_DEPENDENCY_MISSING
        )
    if enabled and str(config.get("data_usage_mode")) != "personal_research":
        return _blocked(
            MainlandCapabilityRoutingFailureReason.DATA_USAGE_MODE_NOT_PERSONAL_RESEARCH
        )
    if enabled and "tushare_calls_per_minute" not in config:
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_PACING_INVALID
        )
    if (
        enabled
        and "tushare_operator_safety_ceiling_calls_per_minute" not in config
    ):
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_OPERATOR_SAFETY_CEILING_INVALID
        )
    try:
        calls_per_minute = _positive_rate(
            config,
            "tushare_calls_per_minute",
            40,
        )
    except (TypeError, ValueError):
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_PACING_INVALID
        )
    try:
        operator_calls_per_minute = _positive_rate(
            config,
            "tushare_operator_safety_ceiling_calls_per_minute",
            40,
        )
    except (TypeError, ValueError):
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_OPERATOR_SAFETY_CEILING_INVALID
        )
    if operator_calls_per_minute > calls_per_minute:
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_OPERATOR_SAFETY_CEILING_INVALID
        )
    account_scope_label = str(
        config.get("tushare_account_scope_label", "personal-research-default")
    )
    normalized_scope = account_scope_label.casefold()
    if (
        re.fullmatch(_IDENTITY_PATTERN, account_scope_label) is None
        or len(account_scope_label) > 64
        or any(
            marker in normalized_scope
            for marker in _RESERVED_ACCOUNT_SCOPE_MARKERS
        )
        or bool(token_value and token_value in account_scope_label)
    ):
        return _blocked(
            MainlandCapabilityRoutingFailureReason.TUSHARE_ACCOUNT_SCOPE_INVALID
        )
    return MainlandCapabilityRoutingPreflight(
        passed=True,
        plan=_build_plan(
            mode=mode,
            enabled=enabled,
            qualification_profile=qualification_profile,
            account_scope_label=account_scope_label,
            calls_per_minute=calls_per_minute,
            operator_calls_per_minute=operator_calls_per_minute,
        )
    )


__all__ = [
    "CAPABILITY_ROUTING_PLAN_VERSION",
    "CAPABILITY_ROUTING_PLAN_VERSION_V1",
    "CAPABILITY_ROUTING_PLAN_VERSION_V2",
    "CAPABILITY_ROUTING_PLAN_VERSION_1_3",
    "CAPABILITY_ROUTING_PLAN_VERSION_1_4",
    "MainlandCapability",
    "MainlandCapabilityRoutingCheckpointAction",
    "MainlandCapabilityRoutingCheckpointCompatibility",
    "MainlandCapabilityRoutingCheckpointError",
    "MainlandCapabilityRoutingCheckpointFailureReason",
    "MainlandCapabilityRoutingDisposition",
    "MainlandCapabilityRoutingFailure",
    "MainlandCapabilityRoutingFailureReason",
    "MainlandCapabilityRoutingMode",
    "MainlandCapabilityRoutingPlan",
    "MainlandCapabilityRoutingPreflight",
    "MainlandCapabilityRoutingPreflightError",
    "MainlandCapabilityRoutingRunProjection",
    "MainlandCapabilityRoutingShadowFailure",
    "MainlandTemporalContractBindings",
    "MainlandTemporalContractBindingsV2",
    "MainlandRequestBudgetIdentity",
    "TushareCapability",
    "TushareEndpointPacingIdentity",
    "capability_routing_configuration_is_explicit",
    "is_mainland_equity_configuration",
    "mainland_capability_routing_failure",
    "preflight_mainland_capability_routing",
    "prepare_mainland_temporal_capability_routing_plan",
    "prepare_mainland_temporal_capability_routing_plan_v1",
    "start_new_mainland_capability_routing_run",
    "validate_mainland_capability_routing_checkpoint",
]

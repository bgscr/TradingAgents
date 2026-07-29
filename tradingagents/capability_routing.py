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
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcome,
    AnalysisOutcomeReason,
    EvidenceReadiness,
    InstrumentKind,
)

CAPABILITY_ROUTING_PLAN_VERSION = "1.0"
CAPABILITY_ROUTING_POLICY_VERSION = "mainland-capability-routing-policy-v1"
QUALIFICATION_PROFILE = "cn-a-2000-20260729-v1"
NORMALIZER_VERSION = "mainland-financial-normalizer-v1"
COMPLETENESS_POLICY_VERSION = "mainland-financial-completeness-v1"

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
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
    QUALIFIED_V1 = "qualified_v1"


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


class TushareCapability(str, Enum):
    STATEMENTS = "statements"
    FINANCIAL_INDICATORS = "financial_indicators"
    ADJUSTMENT_FACTORS = "adjustment_factors"
    NAME_EVENTS = "name_events"


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

    plan_version: Literal["1.0"] = CAPABILITY_ROUTING_PLAN_VERSION
    policy_version: Literal[
        "mainland-capability-routing-policy-v1"
    ] = CAPABILITY_ROUTING_POLICY_VERSION
    mode: MainlandCapabilityRoutingMode
    qualification_profile: Literal["legacy", "cn-a-2000-20260729-v1"]
    routes: tuple[MainlandCapabilityRoute, ...]
    enabled_tushare_capabilities: tuple[TushareCapability, ...] = ()
    normalizer_version: Literal[
        "mainland-financial-normalizer-v1"
    ] = NORMALIZER_VERSION
    completeness_policy_version: Literal[
        "mainland-financial-completeness-v1"
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
        "financial-manifest-v1"
    ] = "financial-manifest-v1"
    selection_contract_version: Literal[
        "financial-period-selection-v1"
    ] = "financial-period-selection-v1"
    degradation_contract_version: Literal[
        "financial-degradation-v1"
    ] = "financial-degradation-v1"
    plan_signature: str = Field(pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_closed_plan(self) -> MainlandCapabilityRoutingPlan:
        expected_profile = (
            "legacy"
            if self.mode is MainlandCapabilityRoutingMode.LEGACY
            else QUALIFICATION_PROFILE
        )
        if self.qualification_profile != expected_profile:
            raise ValueError("routing mode and qualification profile contradict")
        if (
            self.mode is MainlandCapabilityRoutingMode.LEGACY
            and self.enabled_tushare_capabilities
        ):
            raise ValueError("legacy routing cannot enable Tushare capabilities")
        route_capabilities = tuple(route.capability for route in self.routes)
        if route_capabilities != tuple(MainlandCapability):
            raise ValueError("capability routes must be complete and canonical")
        if self.enabled_tushare_capabilities != tuple(
            sorted(
                set(self.enabled_tushare_capabilities),
                key=tuple(TushareCapability).index,
            )
        ):
            raise ValueError("enabled Tushare capabilities must be canonical")
        if self.routes != _routes(self.mode, self.enabled_tushare_capabilities):
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
        if budget_capabilities != tuple(MainlandCapability):
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

    def __init__(self, failure: MainlandCapabilityRoutingFailure) -> None:
        self.failure = failure
        super().__init__(failure.reason.value)


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


def _routes(
    mode: MainlandCapabilityRoutingMode,
    enabled: tuple[TushareCapability, ...],
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
    return tuple(
        MainlandCapabilityRoute(capability=capability, providers=providers[capability])
        for capability in MainlandCapability
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
) -> MainlandCapabilityRoutingPlan:
    payload: dict[str, Any] = {
        "plan_version": CAPABILITY_ROUTING_PLAN_VERSION,
        "policy_version": CAPABILITY_ROUTING_POLICY_VERSION,
        "mode": mode.value,
        "qualification_profile": qualification_profile,
        "routes": [route.model_dump(mode="json") for route in _routes(mode, enabled)],
        "enabled_tushare_capabilities": [item.value for item in enabled],
        "normalizer_version": NORMALIZER_VERSION,
        "completeness_policy_version": COMPLETENESS_POLICY_VERSION,
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
            for capability in MainlandCapability
        ],
        "artifact_contract_version": "source-artifact-v1",
        "manifest_contract_version": "financial-manifest-v1",
        "selection_contract_version": "financial-period-selection-v1",
        "degradation_contract_version": "financial-degradation-v1",
    }
    return MainlandCapabilityRoutingPlan(
        **payload,
        plan_signature=_plan_signature(payload),
    )


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
    "MainlandCapability",
    "MainlandCapabilityRoutingFailure",
    "MainlandCapabilityRoutingFailureReason",
    "MainlandCapabilityRoutingMode",
    "MainlandCapabilityRoutingPlan",
    "MainlandCapabilityRoutingPreflight",
    "MainlandCapabilityRoutingPreflightError",
    "MainlandRequestBudgetIdentity",
    "TushareCapability",
    "TushareEndpointPacingIdentity",
    "capability_routing_configuration_is_explicit",
    "is_mainland_equity_configuration",
    "preflight_mainland_capability_routing",
]

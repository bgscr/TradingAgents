"""Deterministic run-scoped acquisition for company financial tools."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    Timeout as RequestsTimeout,
)

from tradingagents.asset_configuration import (
    RunAssetConfiguration,
    RunAssetConfigurationProjection,
)
from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingPlan,
)
from tradingagents.dataflows.acquisition import (
    AcquisitionController,
    AcquisitionFailure,
    AcquisitionRequest,
    RetryPolicy,
)
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorRateLimitError,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialIndicatorRoutingResult,
    FinancialStatementRoutingRequest,
    FinancialStatementRoutingResult,
    MainlandFinancialCapabilityRouter,
    finalize_indicator_routing_result,
    finalize_statement_routing_result,
    financial_indicator_evidence_disposition,
    financial_statement_evidence_disposition,
    render_indicator_routing_result,
    render_statement_routing_result,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAcquisitionManifest,
    FinancialAcquisitionOutcome,
    FinancialAggregateCompleteness,
    FinancialAvailabilityBasis,
    FinancialAvailabilityScope,
    FinancialCapability,
    FinancialCompanyType,
    FinancialCompanyTypeResolutionMethod,
    FinancialConsolidationScope,
    FinancialCriticalValueConflict,
    FinancialEvidenceDisposition,
    FinancialHistoryCompletenessAssessment,
    FinancialIndicatorAggregateAssessment,
    FinancialIndicatorEvidenceRequirements,
    FinancialIndicatorFamilyAssessment,
    FinancialIndicatorMetadataAssessmentBinding,
    FinancialIndicatorMetadataRejectionBinding,
    FinancialPeriodCandidate,
    FinancialPeriodCompletenessAssessment,
    FinancialPeriodOverlapFinding,
    FinancialPeriodRejectionReason,
    FinancialPeriodSelection,
    FinancialRatioFamily,
    FinancialRatioHistoryCompletenessAssessment,
    FinancialRejectedPeriod,
    FinancialRenderedArtifactIdentity,
    FinancialReportingFrequency,
    FinancialRevisionSelection,
    FinancialSinceListingException,
    FinancialStatementType,
    assert_safe_financial_contract_payload,
    assert_safe_financial_projection,
    assess_financial_history_coverage,
    assess_financial_period_candidate,
    assess_financial_ratio_history_coverage,
)
from tradingagents.dataflows.financial_eligibility_acquisition import (
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V4,
    FinancialEligibilityAcquisitionContext,
    FinancialEligibilityRequestBinding,
    FinancialEligibilityTemporalContextBinding,
    validate_financial_eligibility_acquisition_context_binding,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MainlandFinancialEligibilityPolicyError,
    MainlandFinancialEligibilityProjectionBinding,
    MainlandFinancialPeriodTargetConstruction,
    restore_mainland_financial_eligibility_projection_binding,
)
from tradingagents.dataflows.financial_temporal_eligibility import (
    FinancialTemporalEligibilityCheckpointProjection,
)
from tradingagents.dataflows.listing_publication_evidence import (
    CompositeListingTemporalEligibilityDecision,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestAttemptEvent,
    ProviderSubrequestCache,
    ProviderSubrequestCacheCheckpoint,
    ProviderSubrequestCacheError,
)
from tradingagents.evidence import (
    ACQUISITION_TOKEN_PATTERN,
    AcquisitionUnavailableReason,
    EvidenceState,
    FinancialFamilyAggregateEvidenceAssessment,
    FinancialFamilyEvidenceAssessment,
    FinancialRevisionObservationEvidence,
    FinancialRevisionSelectionEvidence,
    InstrumentIdentityEvidence,
    InstrumentKind,
    ProviderPhysicalAttemptEvidence,
    SourceAcquisitionAvailable,
    SourceAcquisitionOutcome,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    SourceFact,
    SourceFactAvailabilityLineage,
    stable_acquisition_source_ref,
    stable_source_fact_id,
)
from tradingagents.market_history import PhysicalAttemptFailure, PhysicalAttemptOutcome

FINANCIAL_REQUEST_KEY_VERSION = "1.0"
FINANCIAL_PROVIDER_CHAIN_VERSION = "1.0"
FINANCIAL_TOOL_MESSAGE_ENVELOPE_VERSION = "1.0"
FINANCIAL_DISPATCH_LEDGER_VERSION = "1.0"
FINANCIAL_DISPATCH_QUALIFIED_LEDGER_VERSION = "1.1"
FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION = "1.2"
FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION = "1.3"
FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION = "1.4"
FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION = "1.5"
FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION = "1.6"
FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION = "1.7"
FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION = "1.8"
FINANCIAL_DISPATCH_AUDIT_VERSION = "1.0"
FINANCIAL_DISPATCH_MANIFEST_AUDIT_VERSION = "2.0"
FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_AUDIT_VERSION = "3.0"
FINANCIAL_DISPATCH_TEMPORAL_AUDIT_VERSION = "4.0"
FINANCIAL_DISPATCH_WINDOWED_AUDIT_VERSION = "5.0"
FINANCIAL_DISPATCH_COMPOSITE_AUDIT_VERSION = "6.0"
DEFAULT_FINANCIAL_ACQUISITION_POLICY_VERSION = "financial-acquisition:v1"

_CLOSED_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    hide_input_in_errors=True,
)


def _is_machine_local_absolute_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _assert_safe_indicator_operational_projection(value: object) -> None:
    """Apply 05B's stricter payload-free boundary only to forward projections."""

    assert_safe_financial_projection(value)
    projected = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else value
    )
    if isinstance(projected, Mapping):
        for key, item in projected.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in {
                "local_path",
                "provider_payload",
                "raw_text",
                "token_digest",
            }:
                raise ValueError(
                    "indicator operational projection contains forbidden data"
                )
            _assert_safe_indicator_operational_projection(item)
        return
    if isinstance(projected, Sequence) and not isinstance(
        projected,
        (str, bytes, bytearray),
    ):
        for item in projected:
            _assert_safe_indicator_operational_projection(item)
        return
    if isinstance(projected, str) and _is_machine_local_absolute_path(projected):
        raise ValueError("indicator operational projection contains an unsafe locator")
_ACQUISITION_TOKEN = re.compile(ACQUISITION_TOKEN_PATTERN)
_RESERVED_MATERIAL_ARGUMENTS = frozenset(
    {
        "as_of_date",
        "_acquired",
        "_physical_request",
        "_request_key",
        "curr_date",
        "dispatched_at",
        "frequency",
        "graph_message_id",
        "indicator_request_arguments",
        "period",
        "process_id",
        "provider",
        "statement_type",
        "symbol",
        "ticker",
        "tool_call_id",
        "tool_name",
        "variant",
        "vendor",
    }
)
_SYSTEMIC_CIRCUIT_FAILURES = frozenset(
    {
        AcquisitionUnavailableReason.PERMISSION_DENIED,
        AcquisitionUnavailableReason.RATE_LIMITED,
        AcquisitionUnavailableReason.TIMEOUT,
        AcquisitionUnavailableReason.DISCONNECT,
        AcquisitionUnavailableReason.NOT_CONFIGURED,
        AcquisitionUnavailableReason.AUTHENTICATION,
        AcquisitionUnavailableReason.PROVIDER_ERROR,
        AcquisitionUnavailableReason.UPSTREAM_BUSY,
        AcquisitionUnavailableReason.USAGE_NOT_ENTITLED,
        AcquisitionUnavailableReason.REGISTRY_NOT_CONFIGURED,
        AcquisitionUnavailableReason.REGISTRY_UNAVAILABLE,
        AcquisitionUnavailableReason.INTEGRITY_FAILURE,
    }
)


_FINANCIAL_TOOL_SPECS = {
    "get_fundamentals": (
        FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frozenset({FinancialReportingFrequency.NOT_APPLICABLE}),
    ),
    "get_balance_sheet": (
        FinancialStatementType.BALANCE_SHEET,
        frozenset(
            {
                FinancialReportingFrequency.ANNUAL,
                FinancialReportingFrequency.QUARTERLY,
            }
        ),
    ),
    "get_cashflow": (
        FinancialStatementType.CASH_FLOW,
        frozenset(
            {
                FinancialReportingFrequency.ANNUAL,
                FinancialReportingFrequency.QUARTERLY,
            }
        ),
    ),
    "get_income_statement": (
        FinancialStatementType.INCOME_STATEMENT,
        frozenset(
            {
                FinancialReportingFrequency.ANNUAL,
                FinancialReportingFrequency.QUARTERLY,
            }
        ),
    ),
}


class FinancialIndicatorRequestArguments(BaseModel):
    """Caller-owned indicator families and their existing evidence requirements."""

    model_config = _CLOSED_MODEL_CONFIG

    requested_families: tuple[FinancialRatioFamily, ...] = Field(min_length=1)
    family_evidence_requirements: FinancialIndicatorEvidenceRequirements
    remaining_evidence_decision_ready: bool = False

    @model_validator(mode="after")
    def _validate_arguments(self) -> FinancialIndicatorRequestArguments:
        canonical = tuple(
            family for family in FinancialRatioFamily if family in self.requested_families
        )
        if (
            self.requested_families != canonical
            or self.family_evidence_requirements.requested_families != canonical
            or self.remaining_evidence_decision_ready
        ):
            raise ValueError("indicator request arguments are inconsistent")
        return self


class FinancialToolRequest(BaseModel):
    """One logical request; runtime correlation fields are deliberately separate."""

    model_config = _CLOSED_MODEL_CONFIG

    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    statement_type: FinancialStatementType
    frequency: FinancialReportingFrequency
    as_of_date: date
    material_arguments: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    graph_message_id: str | None = Field(default=None, min_length=1, pattern=r".*\S.*")
    process_id: int | None = None
    dispatched_at: datetime | None = None
    financial_eligibility_context_binding: (
        FinancialEligibilityRequestBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    indicator_request_arguments: FinancialIndicatorRequestArguments | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    def canonical_material_arguments(self) -> dict[str, Any]:
        """Include typed qualified arguments in existing material identity."""

        material = dict(self.material_arguments)
        if self.indicator_request_arguments is not None:
            material["indicator_request_arguments"] = (
                self.indicator_request_arguments.model_dump(mode="json")
            )
        return material

    @model_validator(mode="after")
    def _validate_logical_tool_contract(self) -> FinancialToolRequest:
        try:
            expected_statement, allowed_frequencies = _FINANCIAL_TOOL_SPECS[
                self.tool_name
            ]
        except KeyError as exc:
            raise ValueError("unsupported financial tool") from exc
        if self.statement_type is not expected_statement:
            raise ValueError("financial statement type contradicts the tool name")
        if self.frequency not in allowed_frequencies:
            raise ValueError("financial reporting frequency contradicts the tool name")
        if (
            self.indicator_request_arguments is not None
            and self.statement_type
            is not FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
        ):
            raise ValueError("statement tool cannot carry indicator request arguments")
        reserved = tuple(
            sorted(
                key
                for key in self.material_arguments
                if key.strip().casefold() in _RESERVED_MATERIAL_ARGUMENTS
            )
        )
        if reserved:
            raise ValueError(
                "financial material arguments contain reserved routing fields: "
                + ", ".join(reserved)
            )
        _canonical_json(self.material_arguments, label="material arguments")
        return self


@dataclass(frozen=True)
class FinancialProviderVariant:
    """One policy-declared, semantically equivalent provider operation."""

    variant_id: str
    invoke: Callable[[FinancialToolRequest], object]

    def __post_init__(self) -> None:
        if _ACQUISITION_TOKEN.fullmatch(self.variant_id) is None:
            raise ValueError("financial provider variant ID is malformed")
        if not callable(self.invoke):
            raise TypeError("financial provider variant must be callable")


@dataclass(frozen=True)
class FinancialProvider:
    """One configured provider and its ordered equivalent operations."""

    name: str
    variants: tuple[FinancialProviderVariant, ...]

    def __post_init__(self) -> None:
        if _ACQUISITION_TOKEN.fullmatch(self.name) is None:
            raise ValueError("financial provider name is malformed")
        if not self.variants:
            raise ValueError("financial provider requires at least one variant")
        variant_ids = tuple(variant.variant_id for variant in self.variants)
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("financial provider variant IDs must be unique")


class CanonicalFinancialInstrumentIdentity(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_REQUEST_KEY_VERSION
    canonical_symbol: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    instrument_kind: InstrumentKind
    currency: str = Field(min_length=1)
    provenance_provider: str = Field(min_length=1)
    provenance_source_ref: str = Field(min_length=1)
    provenance_retrieved_at: str = Field(min_length=1)
    identity_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class CanonicalFinancialRequestKey(BaseModel):
    """Versioned identity for one complete deterministic acquisition plan."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_REQUEST_KEY_VERSION
    instrument_identity: CanonicalFinancialInstrumentIdentity
    financial_capability: Literal["company_financials"] = "company_financials"
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    statement_type: FinancialStatementType
    frequency: FinancialReportingFrequency
    as_of_date: date
    material_arguments_json: str
    provider_chain_identity: str = Field(
        pattern=r"^financial-provider-chain:v1:[0-9a-f]{64}$"
    )
    acquisition_policy_version: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    capability_routing_plan_signature: str | None = Field(
        default=None,
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$",
    )
    request_key: str = Field(pattern=r"^financial-request:v1:[0-9a-f]{64}$")

    @model_serializer(mode="wrap")
    def _serialize_optional_routing_identity(self, handler) -> dict[str, Any]:
        payload = handler(self)
        if self.capability_routing_plan_signature is None:
            payload.pop("capability_routing_plan_signature", None)
        return payload


class FinancialPlanOutcome(BaseModel):
    """One typed attempt outcome bound to its deterministic plan step."""

    model_config = _CLOSED_MODEL_CONFIG

    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_order: int = Field(ge=0)
    variant_id: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    variant_order: int = Field(ge=0)
    outcome: SourceAcquisitionAvailable | SourceAcquisitionUnavailable = Field(
        discriminator="outcome"
    )

    @model_validator(mode="after")
    def _validate_outcome_binding(self) -> FinancialPlanOutcome:
        if self.provider != self.outcome.provider:
            raise ValueError("financial plan provider does not match its outcome")
        if self.provider_order != self.outcome.provider_order:
            raise ValueError("financial plan provider order does not match its outcome")
        return self


class FinancialTerminalUnavailableEnvelope(BaseModel):
    """Complete bounded content permitted at the model-visible failure boundary."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_TOOL_MESSAGE_ENVELOPE_VERSION
    request_ref: str = Field(pattern=r"^financial-request:v1:[0-9a-f]{64}$")
    capability: Literal["company_financials"] = "company_financials"
    reason: AcquisitionUnavailableReason


class FinancialToolMessageAuditEnvelope(BaseModel):
    """Operational provenance hidden from model-visible ToolMessage content."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_TOOL_MESSAGE_ENVELOPE_VERSION
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    request_ref: str = Field(pattern=r"^financial-request:v1:[0-9a-f]{64}$")
    capability: Literal["company_financials"] = "company_financials"
    disposition: Literal["executed", "duplicate_suppressed"]
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    acquisition_outcomes: tuple[SourceAcquisitionOutcome, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_result_references(self) -> FinancialToolMessageAuditEnvelope:
        available = tuple(
            outcome
            for outcome in self.acquisition_outcomes
            if isinstance(outcome, SourceAcquisitionAvailable)
        )
        for outcome in self.acquisition_outcomes:
            if outcome.source_ref != self.request_ref:
                raise ValueError("financial outcome source_ref does not match envelope")
            if outcome.capability != self.capability:
                raise ValueError("financial outcome capability does not match envelope")
        if self.artifact_sha256 is None:
            if available:
                raise ValueError("unavailable financial envelope cannot reference an artifact")
        elif len(available) != 1 or (
            available[0].artifact.artifact_sha256 != self.artifact_sha256
        ):
            raise ValueError("financial artifact digest does not match available outcome")
        return self


class FinancialDispatchResult(BaseModel):
    """Terminal internal result; unavailable results cannot carry an artifact."""

    model_config = _CLOSED_MODEL_CONFIG

    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    disposition: Literal["executed", "duplicate_suppressed"]
    request_key: CanonicalFinancialRequestKey
    value: str | None
    artifact: SourceArtifact | None
    plan_outcomes: tuple[FinancialPlanOutcome, ...] = Field(min_length=1)
    provider: str | None = None
    variant_id: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_result(self) -> FinancialDispatchResult:
        available = tuple(
            item.outcome
            for item in self.plan_outcomes
            if isinstance(item.outcome, SourceAcquisitionAvailable)
        )
        if self.artifact is None:
            if self.value is not None or self.provider is not None or self.variant_id is not None:
                raise ValueError("unavailable financial result cannot carry available data")
            if available:
                raise ValueError("unavailable financial result cannot carry an available outcome")
        else:
            if self.value is None or self.provider is None or self.variant_id is None:
                raise ValueError("available financial result is incomplete")
            if len(available) != 1 or available[0].artifact != self.artifact:
                raise ValueError("available financial result must bind exactly one artifact")
        return self

    @property
    def outcomes(self) -> tuple[SourceAcquisitionOutcome, ...]:
        return tuple(item.outcome for item in self.plan_outcomes)

    def to_tool_message(self) -> ToolMessage:
        """Render one correlation wrapper after deterministic policy terminates."""

        audit = FinancialToolMessageAuditEnvelope(
            tool_call_id=self.tool_call_id,
            tool_name=self.request_key.tool_name,
            request_ref=self.request_key.request_key,
            disposition=self.disposition,
            artifact_sha256=(
                self.artifact.artifact_sha256 if self.artifact is not None else None
            ),
            acquisition_outcomes=self.outcomes,
        )
        if self.artifact is not None:
            content = self.value
            status = "success"
        else:
            terminal = self.plan_outcomes[-1].outcome
            if not isinstance(terminal, SourceAcquisitionUnavailable):
                raise ValueError("unavailable financial result lacks a terminal reason")
            content = _canonical_json(
                FinancialTerminalUnavailableEnvelope(
                    request_ref=self.request_key.request_key,
                    reason=terminal.reason,
                ).model_dump(mode="json", exclude={"contract_version"}),
                label="financial terminal unavailable envelope",
            )
            status = "error"
        return ToolMessage(
            content=content,
            tool_call_id=self.tool_call_id,
            name=self.request_key.tool_name,
            artifact=audit.model_dump(mode="json"),
            status=status,
        )


def _financial_selection_as_of_date(
    request: FinancialStatementRoutingRequest | FinancialIndicatorRoutingRequest,
) -> date:
    """Return the provider-period date while retaining any bound run session."""

    as_of_date = request.as_of_date or request.requested_session
    if as_of_date is None:  # pragma: no cover - closed request models enforce the shape
        raise ValueError("financial selection as-of date is unavailable")
    return as_of_date


class FinancialStatementSelectionDispatchResult(BaseModel):
    """Qualified-v1 terminal selection owned by the dispatcher single-flight."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-statement-selection-dispatch-v1",
        "financial-statement-selection-dispatch-v2",
        "financial-statement-selection-dispatch-v3",
        "financial-statement-selection-dispatch-v4",
    ] = (
        "financial-statement-selection-dispatch-v1"
    )
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    disposition: Literal["executed", "duplicate_suppressed"]
    request_key: CanonicalFinancialRequestKey
    selection_request: FinancialStatementRoutingRequest
    routing_result: FinancialStatementRoutingResult
    rendered_value: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_result(self) -> FinancialStatementSelectionDispatchResult:
        selection_as_of_date = _financial_selection_as_of_date(self.selection_request)
        if (
            self.request_key.statement_type is not self.selection_request.statement_type
            or self.request_key.as_of_date != selection_as_of_date
            or self.routing_result.manifest.instrument_identity
            != self.selection_request.instrument_identity
            or self.request_key.capability_routing_plan_signature
            != self.routing_result.manifest.capability_routing_plan_signature
            or (
                self.selection_request.financial_eligibility_binding is not None
                and self.selection_request.financial_eligibility_binding
                != self.routing_result.financial_eligibility_binding
            )
            or self.rendered_value
            != render_statement_routing_result(self.routing_result)
        ):
            raise ValueError("qualified financial selection binding is invalid")
        expected_result_version = {
            "financial-statement-selection-dispatch-v1": (
                "financial-statement-routing-result-v1"
            ),
            "financial-statement-selection-dispatch-v2": (
                "financial-statement-routing-result-v2"
            ),
            "financial-statement-selection-dispatch-v3": (
                "financial-statement-routing-result-v3"
            ),
            "financial-statement-selection-dispatch-v4": (
                "financial-statement-routing-result-v1"
            ),
        }[self.contract_version]
        if self.routing_result.contract_version != expected_result_version:
            raise ValueError("statement selection temporal binding is invalid")
        return self

    def to_tool_message(self) -> ToolMessage:
        insufficient = bool(
            self.routing_result.missing_annual_period_ends
            or self.routing_result.missing_reporting_period_ends
            or self.routing_result.conflicted_annual_period_ends
            or self.routing_result.conflicted_reporting_period_ends
            or (
                self.routing_result.publication_target_construction is not None
                and self.routing_result.publication_target_construction.failure_reason
                is not None
            )
        )
        manifest = self.routing_result.manifest
        artifact = (
            _financial_tool_manifest_envelope(
                dispatch_result=self,
            ).model_dump(mode="json")
            if manifest.contract_version
            in {"financial-manifest-v2", "financial-manifest-v4", "financial-manifest-v5"}
            else {
                "contract_version": "financial-statement-selection-dispatch-v1",
                "request_ref": self.request_key.request_key,
                "disposition": self.disposition,
                "manifest_identity": manifest.manifest_identity,
                "physical_attempt_count": len(
                    self.routing_result.physical_attempt_ids
                ),
            }
        )
        return ToolMessage(
            content=self.rendered_value,
            tool_call_id=self.tool_call_id,
            name=self.request_key.tool_name,
            artifact=artifact,
            status="error" if insufficient else "success",
        )


class FinancialIndicatorSelectionDispatchResult(BaseModel):
    """Qualified-v1 indicator selection owned by dispatcher single-flight."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-indicator-selection-dispatch-v1",
        "financial-indicator-selection-dispatch-v2",
        "financial-indicator-selection-dispatch-v3",
        "financial-indicator-selection-dispatch-v4",
        "financial-indicator-selection-dispatch-v5",
    ] = (
        "financial-indicator-selection-dispatch-v1"
    )
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    disposition: Literal["executed", "duplicate_suppressed"]
    request_key: CanonicalFinancialRequestKey
    selection_request: FinancialIndicatorRoutingRequest
    routing_result: FinancialIndicatorRoutingResult
    rendered_value: str = Field(min_length=1)
    capability_routing_plan_signature: str | None = Field(
        default=None,
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    financial_eligibility_context_contract_version: Literal[
        "financial-eligibility-acquisition-context-v1",
        "financial-eligibility-acquisition-context-v2",
        "financial-eligibility-acquisition-context-v3",
        "financial-eligibility-acquisition-context-v4",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    financial_eligibility_context_identity: str | None = Field(
        default=None,
        pattern=r"^financial-eligibility-acquisition-context:v[1234]:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    manifest_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    metadata_assessment_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    provider_artifact_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    candidate_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def _validate_result(self) -> FinancialIndicatorSelectionDispatchResult:
        selection_as_of_date = _financial_selection_as_of_date(self.selection_request)
        expected_eligibility_policy = (
            self.selection_request.eligibility_policy_version
            if self.selection_request.requested_analysis_scope is not None
            else None
        )
        if (
            self.routing_result.requested_analysis_scope
            is not self.selection_request.requested_analysis_scope
            or self.routing_result.eligibility_policy_version
            != expected_eligibility_policy
            or (
                self.selection_request.financial_eligibility_binding is not None
                and self.selection_request.financial_eligibility_binding
                != self.routing_result.financial_eligibility_binding
            )
        ):
            raise ValueError(
                "qualified indicator selection analysis scope binding is invalid"
            )
        aggregate = self.routing_result.aggregate_family_assessment
        requirements = self.selection_request.family_evidence_requirements
        if (aggregate is None) != (requirements is None):
            raise ValueError("qualified indicator family contract binding is incomplete")
        if aggregate is not None and requirements is not None and (
            aggregate.company_type is not self.selection_request.company_type
            or aggregate.requested_families != self.selection_request.ratio_families
            or aggregate.requirements_identity != requirements.requirements_identity
        ):
            raise ValueError("qualified indicator family contract binding is invalid")
        signatures = {
            manifest.capability_routing_plan_signature
            for manifest in self.routing_result.family_manifests
        }
        if (
            self.request_key.statement_type
            is not FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
            or self.request_key.as_of_date != selection_as_of_date
            or any(
                manifest.instrument_identity
                != self.selection_request.instrument_identity
                for manifest in self.routing_result.family_manifests
            )
            or signatures != {self.request_key.capability_routing_plan_signature}
            or self.rendered_value
            != render_indicator_routing_result(self.routing_result)
        ):
            raise ValueError("qualified indicator selection binding is invalid")
        v2_values = (
            self.capability_routing_plan_signature,
            self.financial_eligibility_context_contract_version,
            self.financial_eligibility_context_identity,
        )
        if self.contract_version in {
            "financial-indicator-selection-dispatch-v2",
            "financial-indicator-selection-dispatch-v3",
            "financial-indicator-selection-dispatch-v4",
            "financial-indicator-selection-dispatch-v5",
        }:
            context_binding = (
                self.selection_request.financial_eligibility_context_binding
            )
            expected_manifests = tuple(
                manifest.manifest_identity
                for manifest in self.routing_result.family_manifests
            )
            expected_assessments = tuple(
                item.assessment_identity
                for item in self.routing_result.metadata_compatibility_assessments
            )
            expected_artifacts = tuple(
                item.artifact_identity
                for item in self.routing_result.retained_artifacts
            )
            expected_candidates = tuple(
                sorted(
                    item.candidate.candidate_identity
                    for manifest in self.routing_result.family_manifests
                    for item in manifest.provider_candidates
                )
            )
            if (
                self.routing_result.contract_version
                != {
                    "financial-indicator-selection-dispatch-v2": (
                        "financial-indicator-routing-result-v3"
                    ),
                    "financial-indicator-selection-dispatch-v3": (
                        "financial-indicator-routing-result-v4"
                    ),
                    "financial-indicator-selection-dispatch-v4": (
                        "financial-indicator-routing-result-v5"
                    ),
                    "financial-indicator-selection-dispatch-v5": (
                        "financial-indicator-routing-result-v3"
                    ),
                }[self.contract_version]
                or not self.routing_result.family_manifests
                or any(
                    manifest.contract_version
                    != (
                        {
                            "financial-indicator-selection-dispatch-v2": (
                                "financial-manifest-v3"
                            ),
                            "financial-indicator-selection-dispatch-v3": (
                                "financial-manifest-v4"
                            ),
                            "financial-indicator-selection-dispatch-v4": (
                                "financial-manifest-v5"
                            ),
                            "financial-indicator-selection-dispatch-v5": (
                                "financial-manifest-v3"
                            ),
                        }[self.contract_version]
                    )
                    for manifest in self.routing_result.family_manifests
                )
                or context_binding is None
                or self.capability_routing_plan_signature
                != self.request_key.capability_routing_plan_signature
                or self.financial_eligibility_context_contract_version
                != context_binding.context_contract_version
                or self.financial_eligibility_context_identity
                != context_binding.context_identity
                or self.manifest_identities != expected_manifests
                or self.metadata_assessment_identities != expected_assessments
                or self.provider_artifact_identities != expected_artifacts
                or self.candidate_identities != expected_candidates
            ):
                raise ValueError("indicator dispatch cross-binding is invalid")
        elif (
            any(value is not None for value in v2_values)
            or self.manifest_identities
            or self.metadata_assessment_identities
            or self.provider_artifact_identities
            or self.candidate_identities
        ):
            raise ValueError("historical indicator dispatch carries v2 bindings")
        return self

    def to_tool_message(self) -> ToolMessage:
        forward = bool(self.routing_result.family_manifests) and all(
            manifest.contract_version
            in {
                "financial-manifest-v2",
                "financial-manifest-v3",
                "financial-manifest-v4",
                "financial-manifest-v5",
            }
            for manifest in self.routing_result.family_manifests
        )
        artifact = (
            _financial_tool_manifest_envelope(
                dispatch_result=self,
            ).model_dump(mode="json")
            if forward
            else {
                "contract_version": "financial-indicator-selection-dispatch-v1",
                "request_ref": self.request_key.request_key,
                "disposition": self.disposition,
                "manifest_identities": [
                    manifest.manifest_identity
                    for manifest in self.routing_result.family_manifests
                ],
                "physical_attempt_count": len(
                    self.routing_result.physical_attempt_ids
                ),
            }
        )
        aggregate = self.routing_result.aggregate_family_assessment
        operational_failure = (
            False
            if aggregate is not None
            else bool(
                self.routing_result.missing_periods_by_family
                or any(
                    manifest.disposition is FinancialEvidenceDisposition.INSUFFICIENT
                    for manifest in self.routing_result.family_manifests
                )
                or (
                    self.routing_result.publication_target_construction is not None
                    and self.routing_result.publication_target_construction.failure_reason
                    is not None
                )
            )
        )
        return ToolMessage(
            content=self.rendered_value,
            tool_call_id=self.tool_call_id,
            name=self.request_key.tool_name,
            artifact=artifact,
            status="error" if operational_failure else "success",
        )


class FinancialDispatchCheckpointFailureReason(str, Enum):
    MALFORMED = "financial_dispatch_checkpoint_malformed"
    UNSAFE_BOUNDARY = "financial_dispatch_checkpoint_unsafe_boundary"
    POLICY_MISMATCH = "financial_dispatch_checkpoint_policy_mismatch"
    PROVIDER_CHAIN_MISMATCH = "financial_dispatch_checkpoint_provider_chain_mismatch"
    ASSET_CONFIGURATION_MISMATCH = (
        "financial_dispatch_checkpoint_asset_configuration_mismatch"
    )
    CANONICAL_REQUEST_MISMATCH = (
        "financial_dispatch_checkpoint_canonical_request_mismatch"
    )
    ARTIFACT_REFERENCE_INVALID = (
        "financial_dispatch_checkpoint_artifact_reference_invalid"
    )
    BUDGET_PROGRESS_INVALID = (
        "financial_dispatch_checkpoint_budget_progress_invalid"
    )
    TERMINAL_OUTCOME_INVALID = (
        "financial_dispatch_checkpoint_terminal_outcome_invalid"
    )
    PROVIDER_SUBREQUEST_CACHE_INVALID = (
        "financial_dispatch_checkpoint_provider_subrequest_cache_invalid"
    )
    QUALIFIED_SELECTION_INVALID = (
        "financial_dispatch_checkpoint_qualified_selection_invalid"
    )
    MANIFEST_INVALID = "financial_dispatch_checkpoint_manifest_invalid"
    ROUTING_PLAN_MISMATCH = (
        "financial_dispatch_checkpoint_routing_plan_mismatch"
    )
    RUN_SCOPE_MISMATCH = "financial_dispatch_checkpoint_run_scope_mismatch"
    ELIGIBILITY_POLICY_MISMATCH = "financial_eligibility_policy_mismatch"
    ELIGIBILITY_CONTEXT_MISSING = "financial_eligibility_context_missing"
    ELIGIBILITY_CONTEXT_VERSION_UNSUPPORTED = (
        "financial_eligibility_context_version_unsupported"
    )
    ELIGIBILITY_CONTEXT_IDENTITY_MISMATCH = (
        "financial_eligibility_context_identity_mismatch"
    )
    ELIGIBILITY_CONTEXT_INVALID = "financial_eligibility_context_invalid"
    ELIGIBILITY_CONTEXT_BINDING_MISMATCH = (
        "financial_eligibility_context_binding_mismatch"
    )
    ELIGIBILITY_CONTEXT_CACHE_MISMATCH = (
        "financial_eligibility_context_cache_mismatch"
    )
    ELIGIBILITY_CONTEXT_DISPATCHER_MISMATCH = (
        "financial_eligibility_context_dispatcher_mismatch"
    )
    INDICATOR_METADATA_COMPATIBILITY_MISMATCH = (
        "financial_indicator_metadata_compatibility_mismatch"
    )
    TEMPORAL_COMPATIBILITY_MISMATCH = (
        "financial_temporal_compatibility_mismatch"
    )


class FinancialDispatchCheckpointError(ValueError):
    """Typed fail-closed validation error for restored dispatcher state."""

    def __init__(self, reason: FinancialDispatchCheckpointFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class FinancialProviderChainCheckpointBinding(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_chain_identity: str = Field(
        pattern=r"^financial-provider-chain:v1:[0-9a-f]{64}$"
    )


class FinancialProviderVariantProgress(BaseModel):
    """Completed outcomes and budget consumed at one provider/variant step."""

    model_config = _CLOSED_MODEL_CONFIG

    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_order: int = Field(ge=0)
    variant_id: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    variant_order: int = Field(ge=0)
    outcome_count: int = Field(ge=1)
    attempts_consumed: int = Field(ge=0)


class FinancialCircuitCheckpointState(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    is_open: Literal[True] = True


class FinancialDispatchTerminalCheckpointState(BaseModel):
    """Canonical terminal content stored independently of ToolMessage correlation."""

    model_config = _CLOSED_MODEL_CONFIG

    value: str | None
    artifact: SourceArtifact | None
    plan_outcomes: tuple[FinancialPlanOutcome, ...] = Field(min_length=1)
    terminal_outcome: SourceAcquisitionAvailable | SourceAcquisitionUnavailable = Field(
        discriminator="outcome"
    )
    provider: str | None = None
    variant_id: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> FinancialDispatchTerminalCheckpointState:
        if self.terminal_outcome != self.plan_outcomes[-1].outcome:
            raise ValueError("financial terminal outcome contradicts plan progress")
        available = tuple(
            item.outcome
            for item in self.plan_outcomes
            if isinstance(item.outcome, SourceAcquisitionAvailable)
        )
        if self.artifact is None:
            if self.value is not None or self.provider is not None or self.variant_id is not None:
                raise ValueError("unavailable financial terminal state carries available data")
            if available:
                raise ValueError("unavailable financial terminal state has an artifact outcome")
        else:
            if self.value is None or self.provider is None or self.variant_id is None:
                raise ValueError("available financial terminal state is incomplete")
            if len(available) != 1 or available[0].artifact != self.artifact:
                raise ValueError("available financial terminal state has a mismatched artifact")
        return self


class FinancialDispatchCheckpointEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    canonical_request_key: CanonicalFinancialRequestKey
    provider_variant_progress: tuple[FinancialProviderVariantProgress, ...] = Field(
        min_length=1
    )
    consumed_attempt_budget: int = Field(ge=0)
    terminal: FinancialDispatchTerminalCheckpointState
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reuse_count: int = Field(default=0, ge=0)


class FinancialStatementSelectionCheckpointEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-statement-selection-checkpoint-v1",
        "financial-statement-selection-checkpoint-v2",
        "financial-statement-selection-checkpoint-v3",
        "financial-statement-selection-checkpoint-v4",
    ] = (
        "financial-statement-selection-checkpoint-v1"
    )
    canonical_request_key: CanonicalFinancialRequestKey
    terminal: FinancialStatementSelectionDispatchResult
    reuse_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_entry(self) -> FinancialStatementSelectionCheckpointEntry:
        if self.terminal.request_key != self.canonical_request_key:
            raise ValueError("qualified selection checkpoint request key mismatch")
        expected_dispatch = {
            "financial-statement-selection-checkpoint-v1": (
                "financial-statement-selection-dispatch-v1"
            ),
            "financial-statement-selection-checkpoint-v2": (
                "financial-statement-selection-dispatch-v2"
            ),
            "financial-statement-selection-checkpoint-v3": (
                "financial-statement-selection-dispatch-v3"
            ),
            "financial-statement-selection-checkpoint-v4": (
                "financial-statement-selection-dispatch-v4"
            ),
        }[self.contract_version]
        if self.terminal.contract_version != expected_dispatch:
            raise ValueError("statement checkpoint temporal version mismatch")
        return self


class FinancialIndicatorSelectionCheckpointEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-indicator-selection-checkpoint-v1",
        "financial-indicator-selection-checkpoint-v2",
        "financial-indicator-selection-checkpoint-v3",
        "financial-indicator-selection-checkpoint-v4",
        "financial-indicator-selection-checkpoint-v5",
    ] = (
        "financial-indicator-selection-checkpoint-v1"
    )
    canonical_request_key: CanonicalFinancialRequestKey
    terminal: FinancialIndicatorSelectionDispatchResult
    reuse_count: int = Field(default=0, ge=0)
    provider_subrequest_cache_contract_version: Literal["1.1", "1.2", "1.3", "1.4"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    provider_subrequest_cache_run_scope_id: str | None = Field(
        default=None,
        pattern=ACQUISITION_TOKEN_PATTERN,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_entry(self) -> FinancialIndicatorSelectionCheckpointEntry:
        if self.terminal.request_key != self.canonical_request_key:
            raise ValueError("qualified indicator checkpoint request key mismatch")
        cache_values = (
            self.provider_subrequest_cache_contract_version,
            self.provider_subrequest_cache_run_scope_id,
        )
        if self.contract_version in {
            "financial-indicator-selection-checkpoint-v2",
            "financial-indicator-selection-checkpoint-v3",
            "financial-indicator-selection-checkpoint-v4",
            "financial-indicator-selection-checkpoint-v5",
        }:
            if (
                self.terminal.contract_version
                != {
                    "financial-indicator-selection-checkpoint-v2": (
                        "financial-indicator-selection-dispatch-v2"
                    ),
                    "financial-indicator-selection-checkpoint-v3": (
                        "financial-indicator-selection-dispatch-v3"
                    ),
                    "financial-indicator-selection-checkpoint-v4": (
                        "financial-indicator-selection-dispatch-v4"
                    ),
                    "financial-indicator-selection-checkpoint-v5": (
                        "financial-indicator-selection-dispatch-v5"
                    ),
                }[self.contract_version]
                or any(value is None for value in cache_values)
                or self.provider_subrequest_cache_run_scope_id
                != self.terminal.selection_request.financial_eligibility_context_binding.canonical_run_id
            ):
                raise ValueError("indicator checkpoint cross-binding is invalid")
        elif any(value is not None for value in cache_values):
            raise ValueError("historical indicator checkpoint carries v2 cache binding")
        return self


class FinancialDispatchCheckpointLedger(BaseModel):
    """Optional versioned state stored in the AgentState checkpoint channel."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8"
    ] = (
        FINANCIAL_DISPATCH_LEDGER_VERSION
    )
    acquisition_policy_version: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    retry_policy: RetryPolicy
    provider_chain_identities: tuple[
        FinancialProviderChainCheckpointBinding, ...
    ] = ()
    run_asset_configuration_version: str = Field(min_length=1)
    run_asset_configuration_signature: str = Field(
        pattern=r"^asset-config:v1:[0-9a-f]{64}$"
    )
    circuit_state: tuple[FinancialCircuitCheckpointState, ...] = ()
    provider_subrequest_cache: ProviderSubrequestCacheCheckpoint | None = None
    capability_routing_plan_signature: str | None = Field(
        default=None,
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$",
    )
    financial_manifest_version: Literal[
        "financial-manifest-v2",
        "financial-manifest-v4",
        "financial-manifest-v5",
    ] | None = None
    capability_routing_plan_version: Literal["1.2", "1.3", "1.4", "1.5"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    financial_indicator_manifest_version: Literal[
        "financial-manifest-v3",
        "financial-manifest-v4",
        "financial-manifest-v5",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_assessments: tuple[
        FinancialIndicatorMetadataAssessmentBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    indicator_metadata_rejections: tuple[
        FinancialIndicatorMetadataRejectionBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    indicator_provider_artifact_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    indicator_candidate_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    indicator_provider_topology: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    run_scope_id: str | None = Field(
        default=None,
        pattern=ACQUISITION_TOKEN_PATTERN,
    )
    checkpoint_thread_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{16}$",
        exclude_if=lambda value: value is None,
    )
    financial_eligibility_context: FinancialEligibilityAcquisitionContext | None = (
        Field(default=None, exclude_if=lambda value: value is None)
    )
    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection
        | FinancialEligibilityTemporalContextBinding
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    entries: tuple[FinancialDispatchCheckpointEntry, ...] = ()
    qualified_statement_selections: tuple[
        FinancialStatementSelectionCheckpointEntry,
        ...,
    ] = ()
    qualified_indicator_selections: tuple[
        FinancialIndicatorSelectionCheckpointEntry,
        ...,
    ] = ()

    @model_validator(mode="after")
    def _validate_unique_request_channels(
        self,
    ) -> FinancialDispatchCheckpointLedger:
        keys = [
            entry.canonical_request_key.request_key
            for entry in (
                *self.entries,
                *self.qualified_statement_selections,
                *self.qualified_indicator_selections,
            )
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("financial checkpoint request key crosses dispatch channels")
        if self.contract_version == FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION:
            context = self.financial_eligibility_context
            cache = self.provider_subrequest_cache
            temporal = self.temporal_eligibility_binding
            indicator_manifests = tuple(
                manifest
                for entry in self.qualified_indicator_selections
                for manifest in entry.terminal.routing_result.family_manifests
            )
            expected_assessments = tuple(
                assessment
                for manifest in indicator_manifests
                for assessment in manifest.metadata_compatibility_assessments
            )
            expected_rejections = tuple(
                rejection
                for manifest in indicator_manifests
                for rejection in manifest.metadata_incompatible_rejections
            )
            expected_artifacts = tuple(
                sorted(
                    {
                        artifact.artifact_identity
                        for entry in self.qualified_indicator_selections
                        for artifact in entry.terminal.routing_result.retained_artifacts
                    }
                )
            )
            expected_candidates = tuple(
                sorted(
                    {
                        wrapper.candidate.candidate_identity
                        for manifest in indicator_manifests
                        for wrapper in manifest.provider_candidates
                    }
                )
            )
            if (
                context is None
                or context.contract_version
                != FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V4
                or context.temporal_eligibility_binding is None
                or not isinstance(temporal, FinancialEligibilityTemporalContextBinding)
                or temporal.contract_version
                != "financial-eligibility-temporal-binding-v3"
                or temporal != context.temporal_eligibility_binding
                or self.capability_routing_plan_version != "1.5"
                or self.capability_routing_plan_signature
                != context.routing_plan_signature
                or self.financial_eligibility_binding
                != context.financial_eligibility_binding
                or self.run_scope_id != context.run_id
                or self.checkpoint_thread_id is None
                or context.run_id
                != "run:"
                + sha256(
                    f"checkpoint:{self.checkpoint_thread_id}".encode()
                ).hexdigest()
                or cache is None
                or cache.contract_version != "1.4"
                or cache.run_scope_id != context.run_id
                or cache.financial_eligibility_context_contract_version
                != context.contract_version
                or cache.financial_eligibility_context_identity
                != context.context_identity
                or self.financial_manifest_version != "financial-manifest-v2"
                or self.financial_indicator_manifest_version
                != "financial-manifest-v3"
                or self.indicator_field_semantics_version
                != "financial-indicator-field-semantics-v1"
                or self.indicator_metadata_compatibility_version
                != "financial-indicator-metadata-compatibility-v1"
                or self.indicator_metadata_assessments != expected_assessments
                or self.indicator_metadata_rejections != expected_rejections
                or self.indicator_provider_artifact_identities
                != expected_artifacts
                or self.indicator_candidate_identities != expected_candidates
                or not self.indicator_provider_topology
            ):
                raise ValueError("financial checkpoint 1.8 composite binding is invalid")
            return self
        statement_manifests = tuple(
            entry.terminal.routing_result.manifest
            for entry in self.qualified_statement_selections
        )
        indicator_manifests = tuple(
            manifest
            for entry in self.qualified_indicator_selections
            for manifest in entry.terminal.routing_result.family_manifests
        )
        manifests = statement_manifests + indicator_manifests
        manifest_versions = {manifest.contract_version for manifest in manifests}
        manifest_bindings = {
            json.dumps(
                manifest.financial_eligibility_binding.model_dump(mode="json"),
                separators=(",", ":"),
                sort_keys=True,
            )
            if manifest.financial_eligibility_binding is not None
            else None
            for manifest in manifests
        }
        has_context_projection = (
            self.checkpoint_thread_id is not None
            or self.financial_eligibility_context is not None
        )
        if has_context_projection and (
            self.checkpoint_thread_id is None
            or self.financial_eligibility_context is None
        ):
            raise ValueError("financial checkpoint context projection is incomplete")
        temporal_bound = (
            self.contract_version
            in {
                FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
                FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
            }
            or self.capability_routing_plan_version in {"1.3", "1.4"}
            or self.temporal_eligibility_binding is not None
        )
        windowed = (
            self.contract_version == FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION
            or self.capability_routing_plan_version == "1.4"
            or (
                self.financial_eligibility_context is not None
                and self.financial_eligibility_context.contract_version
                == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3
            )
        )
        if temporal_bound:
            context = self.financial_eligibility_context
            if (
                not has_context_projection
                or context is None
                or context.contract_version
                not in {
                    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
                    FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
                }
                or context.temporal_eligibility_binding is None
                or self.temporal_eligibility_binding is None
                or self.temporal_eligibility_binding
                != FinancialTemporalEligibilityCheckpointProjection.model_validate(
                    context.temporal_eligibility_binding.model_dump(mode="python")
                )
                or manifest_versions
                != {"financial-manifest-v5" if windowed else "financial-manifest-v4"}
            ):
                raise ValueError("financial checkpoint temporal binding is invalid")
            expected_version = (
                FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION
                if windowed
                else FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION
            )
        else:
            expected_version = None
        compatibility_bound = (
            self.capability_routing_plan_version == "1.2"
            or self.contract_version
            == FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION
        )
        if temporal_bound:
            pass
        elif compatibility_bound:
            if not has_context_projection:
                raise ValueError("financial checkpoint 1.5 requires context projection")
            expected_version = (
                FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION
            )
        elif has_context_projection:
            expected_version = FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION
        elif not manifests:
            expected_version = FINANCIAL_DISPATCH_LEDGER_VERSION
        elif manifest_versions == {"financial-manifest-v1"}:
            expected_version = FINANCIAL_DISPATCH_QUALIFIED_LEDGER_VERSION
        elif manifest_versions == {"financial-manifest-v2"}:
            if len(manifest_bindings) != 1:
                raise ValueError(
                    "financial checkpoint mixes eligibility projection bindings"
                )
            expected_version = (
                FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION
                if None not in manifest_bindings
                else FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION
            )
        else:
            raise ValueError("financial checkpoint mixes manifest contract versions")
        if self.contract_version != expected_version:
            raise ValueError(
                "financial checkpoint version contradicts qualified selections"
            )
        has_v2_identity = (
            self.capability_routing_plan_signature is not None
            or self.financial_manifest_version is not None
            or self.financial_eligibility_binding is not None
            or self.run_scope_id is not None
            or has_context_projection
        )
        if expected_version in {
            FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION,
            FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
            FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
            FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
        }:
            signatures = {
                manifest.capability_routing_plan_signature
                for manifest in manifests
            }
            if (
                self.financial_manifest_version
                != (
                    "financial-manifest-v5"
                    if windowed
                    else "financial-manifest-v4"
                    if temporal_bound
                    else "financial-manifest-v2"
                )
                or self.run_scope_id is None
                or signatures != {self.capability_routing_plan_signature}
            ):
                raise ValueError("financial checkpoint v2 identity binding is incomplete")
            if expected_version in {
                FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
                FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
                FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
                FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
                FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
                FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
            }:
                if self.financial_eligibility_binding is None or any(
                    manifest.financial_eligibility_binding
                    != self.financial_eligibility_binding
                    for manifest in manifests
                ):
                    raise ValueError(
                        "financial checkpoint eligibility binding is incomplete"
                    )
            elif self.financial_eligibility_binding is not None:
                raise ValueError(
                    "historical v2 checkpoint carries a corrected eligibility binding"
                )
        elif has_v2_identity:
            raise ValueError("legacy financial checkpoint carries v2 identity fields")
        indicator_v15_values = (
            self.capability_routing_plan_version,
            self.financial_indicator_manifest_version,
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
        )
        if expected_version in {
            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
            FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
        }:
            expected_assessments = tuple(
                binding
                for manifest in indicator_manifests
                for binding in manifest.metadata_compatibility_assessments
            )
            expected_rejections = tuple(
                rejection
                for manifest in indicator_manifests
                for rejection in manifest.metadata_incompatible_rejections
            )
            expected_artifacts = tuple(
                sorted(
                    {
                        artifact.artifact_identity
                        for entry in self.qualified_indicator_selections
                        for artifact in entry.terminal.routing_result.retained_artifacts
                    }
                )
            )
            expected_candidates = tuple(
                sorted(
                    {
                        wrapper.candidate.candidate_identity
                        for manifest in indicator_manifests
                        for wrapper in manifest.provider_candidates
                    }
                )
            )
            expected_topology = tuple(
                dict.fromkeys(
                    provider_id
                    for entry in self.qualified_indicator_selections
                    for provider_id in entry.terminal.routing_result.route
                )
            )
            if (
                any(value is None for value in indicator_v15_values)
                or self.capability_routing_plan_version
                != ("1.4" if windowed else "1.3" if temporal_bound else "1.2")
                or any(
                    manifest.contract_version
                    != (
                        "financial-manifest-v5"
                        if windowed
                        else "financial-manifest-v4"
                        if temporal_bound
                        else "financial-manifest-v2"
                    )
                    for manifest in statement_manifests
                )
                or any(
                    manifest.contract_version
                    != (
                        "financial-manifest-v5"
                        if windowed
                        else "financial-manifest-v4"
                        if temporal_bound
                        else "financial-manifest-v3"
                    )
                    for manifest in indicator_manifests
                )
                or any(
                    entry.contract_version
                    != (
                        "financial-indicator-selection-checkpoint-v4"
                        if windowed
                        else "financial-indicator-selection-checkpoint-v3"
                        if temporal_bound
                        else "financial-indicator-selection-checkpoint-v2"
                    )
                    or entry.terminal.contract_version
                    != (
                        "financial-indicator-selection-dispatch-v4"
                        if windowed
                        else "financial-indicator-selection-dispatch-v3"
                        if temporal_bound
                        else "financial-indicator-selection-dispatch-v2"
                    )
                    or entry.terminal.routing_result.contract_version
                    != (
                        "financial-indicator-routing-result-v5"
                        if windowed
                        else "financial-indicator-routing-result-v4"
                        if temporal_bound
                        else "financial-indicator-routing-result-v3"
                    )
                    for entry in self.qualified_indicator_selections
                )
                or self.indicator_metadata_assessments != expected_assessments
                or self.indicator_metadata_rejections != expected_rejections
                or self.indicator_provider_artifact_identities != expected_artifacts
                or self.indicator_candidate_identities != expected_candidates
                or not self.indicator_provider_topology
                or (
                    bool(expected_topology)
                    and self.indicator_provider_topology != expected_topology
                )
            ):
                raise ValueError("financial checkpoint 1.5 indicator binding is invalid")
        elif (
            any(value is not None for value in indicator_v15_values)
            or self.indicator_metadata_assessments
            or self.indicator_metadata_rejections
            or self.indicator_provider_artifact_identities
            or self.indicator_candidate_identities
            or self.indicator_provider_topology
        ):
            raise ValueError("historical financial checkpoint carries 1.5 bindings")
        if expected_version in {
            FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION,
            FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
            FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
        }:
            if expected_version in {
                FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
                FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
                FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
                FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
            }:
                context = self.financial_eligibility_context
                cache = self.provider_subrequest_cache
                assert context is not None
                assert self.checkpoint_thread_id is not None
                expected_run_scope_id = "run:" + sha256(
                    f"checkpoint:{self.checkpoint_thread_id}".encode()
                ).hexdigest()
                expected_request_binding = FinancialEligibilityRequestBinding.from_context(
                    context
                )
                selection_bindings = tuple(
                    entry.terminal.selection_request.financial_eligibility_context_binding
                    for entry in (
                        *self.qualified_statement_selections,
                        *self.qualified_indicator_selections,
                    )
                )
                try:
                    validate_financial_eligibility_acquisition_context_binding(
                        context,
                        canonical_run_id=self.run_scope_id,
                        asset_configuration_signature=(
                            self.run_asset_configuration_signature
                        ),
                        financial_eligibility_binding=(
                            self.financial_eligibility_binding
                        ),
                        routing_plan_signature=(
                            self.capability_routing_plan_signature
                        ),
                    )
                except ValueError:
                    raise ValueError(
                        "financial checkpoint context cross-binding is invalid"
                    ) from None
                if (
                    self.run_scope_id != expected_run_scope_id
                    or not selection_bindings
                    or any(
                        binding != expected_request_binding
                        for binding in selection_bindings
                    )
                    or cache is None
                    or cache.contract_version
                    != ("1.3" if windowed else "1.2" if temporal_bound else "1.1")
                    or cache.run_scope_id != context.run_id
                    or cache.financial_eligibility_context_contract_version
                    != context.contract_version
                    or cache.financial_eligibility_context_identity
                    != context.context_identity
                    or (
                        temporal_bound
                        and (
                            cache.capability_routing_plan_version
                            != ("1.4" if windowed else "1.3")
                            or cache.calendar_eligibility_binding
                            != self.temporal_eligibility_binding.calendar_binding
                            or cache.temporal_decisions
                            != (
                                self.temporal_eligibility_binding.company_type_temporal_decision,
                                self.temporal_eligibility_binding.listing_temporal_decision,
                            )
                        )
                    )
                ):
                    raise ValueError(
                        "financial checkpoint context cross-binding is invalid"
                    )
            assert_safe_financial_contract_payload(self.model_dump(mode="json"))
        return self


class FinancialDispatchAuditOutcome(BaseModel):
    """Bounded operational projection of one deterministic plan outcome."""

    model_config = _CLOSED_MODEL_CONFIG

    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    provider_order: int = Field(ge=0)
    outcome: Literal["available", "unavailable"]
    attempt: int = Field(ge=1)
    retryable: bool
    reason: AcquisitionUnavailableReason | None = None
    error_code: str | None = Field(default=None, pattern=ACQUISITION_TOKEN_PATTERN)
    http_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FinancialDispatchAuditRequest(BaseModel):
    """One canonical request without model correlation or provider payload text."""

    model_config = _CLOSED_MODEL_CONFIG

    request_ref: str = Field(pattern=r"^financial-request:v1:[0-9a-f]{64}$")
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    statement_type: FinancialStatementType
    frequency: FinancialReportingFrequency
    as_of_date: date
    acquisition_attempt_count: int = Field(ge=0)
    duplicate_suppressed_count: int = Field(ge=0)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcomes: tuple[FinancialDispatchAuditOutcome, ...] = Field(min_length=1)


class FinancialDispatchAuditProjectionV1(BaseModel):
    """Safe immutable-audit projection of the run-scoped dispatch ledger."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["1.0"] = FINANCIAL_DISPATCH_AUDIT_VERSION
    request_count: int = Field(ge=0)
    acquisition_attempt_count: int = Field(ge=0)
    duplicate_suppressed_count: int = Field(ge=0)
    requests: tuple[FinancialDispatchAuditRequest, ...] = ()

    @model_validator(mode="after")
    def _validate_totals(self) -> FinancialDispatchAuditProjectionV1:
        if self.request_count != len(self.requests):
            raise ValueError("financial dispatch audit request count is inconsistent")
        if self.acquisition_attempt_count != sum(
            item.acquisition_attempt_count for item in self.requests
        ):
            raise ValueError("financial dispatch audit attempt count is inconsistent")
        if self.duplicate_suppressed_count != sum(
            item.duplicate_suppressed_count for item in self.requests
        ):
            raise ValueError("financial dispatch audit duplicate count is inconsistent")
        return self


class FinancialCompletenessAuditRow(BaseModel):
    """Payload-free deterministic completeness row for one normalized candidate."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-completeness-row-v3",
        "financial-completeness-row-v4",
        "financial-completeness-row-v5",
        "financial-completeness-row-v6",
    ] = (
        "financial-completeness-row-v3"
    )
    manifest_identity: str = Field(
        pattern=r"^financial-manifest:v[2345]:[0-9a-f]{64}$"
    )
    capability: FinancialCapability
    statement_or_ratio_family: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    period: date
    frequency: FinancialReportingFrequency
    provider: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    company_type: FinancialCompanyType
    company_type_resolution_method: FinancialCompanyTypeResolutionMethod | None
    company_type_provenance: tuple[str, ...] = ()
    provider_declared_type: FinancialCompanyType | None = None
    provider_declaration_qualified: bool
    metadata_coverage: Decimal = Field(ge=0, le=1)
    ann_date: date | None = None
    f_ann_date: date | None = None
    f_ann_date_state: Literal["present", "absent"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requested_analysis_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_basis: FinancialAvailabilityBasis | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    availability_reason: FinancialPeriodRejectionReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    report_type: str | None = Field(default=None, max_length=64)
    provider_comp_type: str | None = Field(default=None, max_length=64)
    update_flag: str | None = Field(default=None, max_length=64)
    revision_observed_at: datetime | None = None
    retrieved_at: datetime | None = None
    local_provider_revision_identity: str | None = Field(
        default=None,
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$",
    )
    provider_filing_revision_id: str | None = Field(
        default=None,
        pattern=ACQUISITION_TOKEN_PATTERN,
    )
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    declared_normalized_unit: str | None = Field(default=None, max_length=64)
    observed_normalized_units: tuple[str, ...] = ()
    declared_critical_fields: tuple[str, ...] = ()
    declared_core_fields: tuple[str, ...] = ()
    mapped_normalized_fields: tuple[str, ...] = ()
    present_critical_fields: tuple[str, ...] = ()
    present_core_fields: tuple[str, ...] = ()
    missing_critical_fields: tuple[str, ...] = ()
    missing_core_fields: tuple[str, ...] = ()
    critical_coverage: Decimal = Field(ge=0, le=1)
    critical_threshold: Decimal = Field(ge=0, le=1)
    core_coverage: Decimal = Field(ge=0, le=1)
    core_threshold: Decimal = Field(ge=0, le=1)
    disposition: Literal[
        "selected",
        "current_only",
        "unselected",
        "unresolved",
        "rejected",
        "conflicted",
    ]
    typed_reason: tuple[str, ...] = ()
    pit_eligible: bool
    artifact_identity: str | None = Field(
        default=None,
        pattern=r"^financial-provider-artifact:v1:[0-9a-f]{64}$"
    )
    provider_attempt_count: int = Field(ge=0)
    since_listing_exception: bool = False
    listing_date: date | None = None
    listing_provider: str | None = Field(
        default=None,
        pattern=ACQUISITION_TOKEN_PATTERN,
    )
    listing_source_ref: str | None = Field(
        default=None,
        pattern=ACQUISITION_TOKEN_PATTERN,
    )
    compatibility_state: Literal[
        "compatible",
        "incompatible",
        "unresolved",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_compatibility_assessment_identity: str | None = Field(
        default=None,
        pattern=r"^financial-indicator-metadata-compatibility:v1:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    metadata_typed_reasons: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection | None
    ) = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _validate_listing_binding(self) -> FinancialCompletenessAuditRow:
        canonical_field_sets = (
            self.declared_critical_fields,
            self.declared_core_fields,
            self.mapped_normalized_fields,
            self.present_critical_fields,
            self.present_core_fields,
            self.missing_critical_fields,
            self.missing_core_fields,
            self.observed_normalized_units,
        )
        if any(
            values != tuple(sorted(set(values)))
            for values in canonical_field_sets
        ):
            raise ValueError("financial completeness fields must be canonical")
        listing_values = (
            self.listing_date,
            self.listing_provider,
            self.listing_source_ref,
        )
        if self.since_listing_exception != all(
            item is not None for item in listing_values
        ):
            raise ValueError("financial completeness listing provenance is incomplete")
        revision_values = (
            self.revision_observed_at,
            self.local_provider_revision_identity,
        )
        if (
            self.artifact_identity is None
            and any(item is not None for item in revision_values)
        ) or (
            self.artifact_identity is not None
            and any(item is None for item in revision_values)
        ):
            raise ValueError("financial completeness revision lineage is incomplete")
        if (
            self.local_provider_revision_identity is not None
            and self.local_provider_revision_identity != self.artifact_identity
        ):
            raise ValueError("financial completeness revision identity is contradictory")
        if self.eligibility_policy_version is not None:
            if self.f_ann_date_state != (
                "present" if self.f_ann_date is not None else "absent"
            ):
                raise ValueError("financial completeness availability is incomplete")
            if self.capability is FinancialCapability.RATIO_FAMILY and (
                self.requested_analysis_scope is None
                or (self.availability_scope is None)
                != (self.availability_basis is None)
                or (
                    self.contract_version == "financial-completeness-row-v3"
                    and self.availability_scope is None
                )
                or (
                    self.contract_version
                    in {
                        "financial-completeness-row-v4",
                        "financial-completeness-row-v5",
                        "financial-completeness-row-v6",
                    }
                    and self.disposition in {"selected", "current_only"}
                    and self.availability_scope is None
                )
            ):
                raise ValueError("financial completeness availability is incomplete")
        metadata_values = (
            self.compatibility_state,
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
        )
        metadata_forward = (
            self.contract_version
            in {
                "financial-completeness-row-v4",
                "financial-completeness-row-v5",
                "financial-completeness-row-v6",
            }
            and self.capability is FinancialCapability.RATIO_FAMILY
        )
        if metadata_forward:
            expected_manifest_prefix = (
                "financial-manifest:v5:"
                if self.contract_version == "financial-completeness-row-v6"
                else (
                    "financial-manifest:v4:"
                    if self.contract_version == "financial-completeness-row-v5"
                    else "financial-manifest:v3:"
                )
            )
            if (
                not self.manifest_identity.startswith(expected_manifest_prefix)
                or any(value is None for value in metadata_values)
                or (
                    self.compatibility_state != "unresolved"
                    and self.metadata_compatibility_assessment_identity is None
                )
                or (
                    self.compatibility_state == "incompatible"
                    and not self.metadata_typed_reasons
                )
                or (
                    self.compatibility_state == "compatible"
                    and self.metadata_typed_reasons
                )
            ):
                raise ValueError("financial completeness row v4 binding is incomplete")
        elif self.contract_version in {
            "financial-completeness-row-v5",
            "financial-completeness-row-v6",
        }:
            expected_manifest_prefix = (
                "financial-manifest:v5:"
                if self.contract_version == "financial-completeness-row-v6"
                else "financial-manifest:v4:"
            )
            if (
                not self.manifest_identity.startswith(expected_manifest_prefix)
                or any(value is not None for value in metadata_values)
                or self.metadata_compatibility_assessment_identity is not None
                or self.metadata_typed_reasons
            ):
                raise ValueError("financial completeness row v5 binding is incomplete")
        elif (
            not self.manifest_identity.startswith("financial-manifest:v2:")
            or any(value is not None for value in metadata_values)
            or self.metadata_compatibility_assessment_identity is not None
            or self.metadata_typed_reasons
        ):
            raise ValueError("financial completeness row v3 carries indicator metadata")
        temporal = self.contract_version in {
            "financial-completeness-row-v5",
            "financial-completeness-row-v6",
        }
        if temporal != (self.temporal_eligibility_binding is not None):
            raise ValueError("financial completeness temporal binding is incomplete")
        if temporal and self.temporal_eligibility_binding is not None and (
            self.temporal_eligibility_binding.contract_version
            != (
                "financial-eligibility-temporal-binding-v2"
                if self.contract_version == "financial-completeness-row-v6"
                else "financial-eligibility-temporal-binding-v1"
            )
        ):
            raise ValueError("financial completeness temporal version is inconsistent")
        if self.contract_version in {
            "financial-completeness-row-v4",
            "financial-completeness-row-v5",
            "financial-completeness-row-v6",
        }:
            _assert_safe_indicator_operational_projection(self)
        else:
            assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


class FinancialAggregateCompletenessProjection(BaseModel):
    """Exact completeness reference without authoritative identity provenance."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-aggregate-completeness-v2",
        "financial-aggregate-completeness-v3",
        "financial-aggregate-completeness-v4",
        "financial-aggregate-completeness-v5",
    ] = (
        "financial-aggregate-completeness-v2"
    )
    source_contract_version: Literal[
        "financial-period-selection-v1",
        "financial-period-selection-v3",
    ] = (
        "financial-period-selection-v1"
    )
    assessment_ref: str = Field(
        pattern=r"^financial-completeness-assessment=sha256:[0-9a-f]{64}$"
    )
    capability: FinancialCapability
    statement_type: FinancialStatementType | None = None
    ratio_family: FinancialRatioFamily | None = None
    company_type: FinancialCompanyType
    consolidation_scope: FinancialConsolidationScope
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_annual_period_ends: tuple[date, ...] = ()
    target_reporting_period_ends: tuple[date, ...]
    covered_annual_period_ends: tuple[date, ...] = ()
    covered_reporting_period_ends: tuple[date, ...]
    missing_annual_period_ends: tuple[date, ...] = ()
    missing_reporting_period_ends: tuple[date, ...]
    since_listing_exception: FinancialSinceListingException | None = None
    rejection_reasons: tuple[FinancialPeriodRejectionReason, ...]
    complete: bool
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_assessment_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    compatibility_states: tuple[Literal["compatible", "incompatible"], ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    family_disposition: Literal["selected", "unresolved"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection | None
    ) = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _validate_capability_shape(
        self,
    ) -> FinancialAggregateCompletenessProjection:
        if self.capability is FinancialCapability.STATEMENT:
            if self.statement_type is None or self.ratio_family is not None:
                raise ValueError(
                    "statement completeness projection has contradictory family"
                )
        elif (
            self.statement_type is not None
            or self.ratio_family is None
            or self.target_annual_period_ends
            or self.covered_annual_period_ends
            or self.missing_annual_period_ends
            or self.since_listing_exception is not None
        ):
            raise ValueError("ratio completeness projection has contradictory family")
        metadata_values = (
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
            self.family_disposition,
        )
        metadata_forward = (
            self.contract_version
            in {
                "financial-aggregate-completeness-v3",
                "financial-aggregate-completeness-v4",
                "financial-aggregate-completeness-v5",
            }
            and self.capability is FinancialCapability.RATIO_FAMILY
        )
        if metadata_forward:
            if (
                self.capability is not FinancialCapability.RATIO_FAMILY
                or self.source_contract_version != "financial-period-selection-v3"
                or any(value is None for value in metadata_values)
                or (
                    not self.metadata_assessment_identities
                    and self.family_disposition != "unresolved"
                )
                or len(self.metadata_assessment_identities)
                != len(self.compatibility_states)
                or (self.family_disposition == "selected") != self.complete
            ):
                raise ValueError("financial aggregate completeness v3 binding is incomplete")
        elif (
            self.source_contract_version != "financial-period-selection-v1"
            or any(value is not None for value in metadata_values)
            or self.metadata_assessment_identities
            or self.compatibility_states
        ):
            raise ValueError("financial aggregate completeness v2 carries indicator metadata")
        temporal = self.contract_version in {
            "financial-aggregate-completeness-v4",
            "financial-aggregate-completeness-v5",
        }
        if temporal != (self.temporal_eligibility_binding is not None):
            raise ValueError("financial aggregate temporal binding is incomplete")
        if temporal and self.temporal_eligibility_binding is not None and (
            self.temporal_eligibility_binding.contract_version
            != (
                "financial-eligibility-temporal-binding-v2"
                if self.contract_version == "financial-aggregate-completeness-v5"
                else "financial-eligibility-temporal-binding-v1"
            )
        ):
            raise ValueError("financial aggregate temporal version is inconsistent")
        if self.contract_version in {
            "financial-aggregate-completeness-v3",
            "financial-aggregate-completeness-v4",
            "financial-aggregate-completeness-v5",
        }:
            _assert_safe_indicator_operational_projection(self)
        else:
            assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


class FinancialManifestAuditProjection(BaseModel):
    """Identity-only manifest projection; normalized provider values stay excluded."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-manifest-audit-v2",
        "financial-manifest-audit-v3",
        "financial-manifest-audit-v4",
        "financial-manifest-audit-v5",
    ] = (
        "financial-manifest-audit-v2"
    )
    manifest_identity: str = Field(
        pattern=r"^financial-manifest:v[2345]:[0-9a-f]{64}$"
    )
    capability_routing_plan_signature: str = Field(
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$"
    )
    financial_manifest_version: Literal[
        "financial-manifest-v2",
        "financial-manifest-v3",
        "financial-manifest-v4",
        "financial-manifest-v5",
    ] = (
        "financial-manifest-v2"
    )
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    capability: FinancialCapability
    statement_or_ratio_family: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    disposition: FinancialEvidenceDisposition
    requested_analysis_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_basis: FinancialAvailabilityBasis | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    availability_reason: FinancialPeriodRejectionReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    final_rendered_artifact_identity: str = Field(
        pattern=r"^financial-rendered-artifact:v1:[0-9a-f]{64}$"
    )
    artifact_identities: tuple[str, ...]
    acquisition_outcomes: tuple[FinancialAcquisitionOutcome, ...]
    physical_attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]
    candidate_identities: tuple[str, ...]
    selections: tuple[FinancialPeriodSelection, ...]
    rejected_periods: tuple[FinancialRejectedPeriod, ...]
    overlaps: tuple[FinancialPeriodOverlapFinding, ...]
    conflicts: tuple[FinancialCriticalValueConflict, ...]
    revision_selections: tuple[FinancialRevisionSelection, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    aggregate_completeness: FinancialAggregateCompletenessProjection
    publication_target_construction: (
        MainlandFinancialPeriodTargetConstruction | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_compatibility_assessments: tuple[
        FinancialIndicatorMetadataAssessmentBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    metadata_incompatible_rejections: tuple[
        FinancialIndicatorMetadataRejectionBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    selected_families: tuple[FinancialRatioFamily, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    unselected_families: tuple[FinancialRatioFamily, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    unresolved_families: tuple[FinancialRatioFamily, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection | None
    ) = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _validate_safe_projection(self) -> FinancialManifestAuditProjection:
        metadata_values = (
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
        )
        windowed = self.contract_version == "financial-manifest-audit-v5"
        temporal = self.contract_version in {
            "financial-manifest-audit-v4",
            "financial-manifest-audit-v5",
        }
        metadata_forward = (
            self.contract_version == "financial-manifest-audit-v3"
            or temporal
            and self.capability is FinancialCapability.RATIO_FAMILY
        )
        if metadata_forward:
            expected_manifest_version = (
                "financial-manifest-v5"
                if windowed
                else "financial-manifest-v4"
                if temporal
                else "financial-manifest-v3"
            )
            expected_aggregate_version = (
                "financial-aggregate-completeness-v5"
                if windowed
                else "financial-aggregate-completeness-v4"
                if temporal
                else "financial-aggregate-completeness-v3"
            )
            if (
                self.financial_manifest_version != expected_manifest_version
                or not self.manifest_identity.startswith(
                    "financial-manifest:v5:"
                    if windowed
                    else "financial-manifest:v4:"
                    if temporal
                    else "financial-manifest:v3:"
                )
                or self.capability is not FinancialCapability.RATIO_FAMILY
                or any(value is None for value in metadata_values)
                or (
                    bool(self.candidate_identities)
                    and not self.metadata_compatibility_assessments
                )
                or self.aggregate_completeness.contract_version
                != expected_aggregate_version
                or (*self.selected_families, *self.unselected_families, *self.unresolved_families)
                != (FinancialRatioFamily(self.statement_or_ratio_family),)
            ):
                raise ValueError("financial manifest audit v3 binding is incomplete")
        elif temporal:
            expected_manifest_version = (
                "financial-manifest-v5" if windowed else "financial-manifest-v4"
            )
            expected_aggregate_version = (
                "financial-aggregate-completeness-v5"
                if windowed
                else "financial-aggregate-completeness-v4"
            )
            if (
                self.financial_manifest_version != expected_manifest_version
                or not self.manifest_identity.startswith(
                    "financial-manifest:v5:"
                    if windowed
                    else "financial-manifest:v4:"
                )
                or self.aggregate_completeness.contract_version
                != expected_aggregate_version
                or any(value is not None for value in metadata_values)
                or self.metadata_compatibility_assessments
                or self.metadata_incompatible_rejections
                or self.selected_families
                or self.unselected_families
                or self.unresolved_families
            ):
                raise ValueError("financial manifest audit v4 binding is incomplete")
        elif (
            self.financial_manifest_version != "financial-manifest-v2"
            or not self.manifest_identity.startswith("financial-manifest:v2:")
            or any(value is not None for value in metadata_values)
            or self.metadata_compatibility_assessments
            or self.metadata_incompatible_rejections
            or self.selected_families
            or self.unselected_families
            or self.unresolved_families
        ):
            raise ValueError("financial manifest audit v2 carries indicator metadata")
        if temporal != (self.temporal_eligibility_binding is not None):
            raise ValueError("financial manifest audit temporal binding is incomplete")
        if temporal and (
            self.aggregate_completeness.temporal_eligibility_binding
            != self.temporal_eligibility_binding
        ):
            raise ValueError("financial manifest audit temporal binding is inconsistent")
        if temporal and self.temporal_eligibility_binding is not None and (
            self.temporal_eligibility_binding.contract_version
            != (
                "financial-eligibility-temporal-binding-v2"
                if windowed
                else "financial-eligibility-temporal-binding-v1"
            )
        ):
            raise ValueError("financial manifest audit temporal version is inconsistent")
        if self.contract_version in {
            "financial-manifest-audit-v3",
            "financial-manifest-audit-v4",
            "financial-manifest-audit-v5",
        }:
            _assert_safe_indicator_operational_projection(self)
        else:
            assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


class FinancialManifestAuditRequest(BaseModel):
    """One logical qualified request with distinct physical-attempt accounting."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-manifest-request-audit-v2",
        "financial-manifest-request-audit-v3",
        "financial-manifest-request-audit-v4",
        "financial-manifest-request-audit-v5",
    ] = (
        "financial-manifest-request-audit-v2"
    )
    request_ref: str = Field(pattern=r"^financial-request:v1:[0-9a-f]{64}$")
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    statement_type: FinancialStatementType
    frequency: FinancialReportingFrequency
    as_of_date: date
    logical_provider_count: int = Field(ge=0)
    logical_candidate_count: int = Field(ge=0)
    physical_request_count: int = Field(ge=0)
    duplicate_suppressed_count: int = Field(ge=0)
    manifests: tuple[FinancialManifestAuditProjection, ...] = Field(min_length=1)
    family_assessments: tuple[FinancialIndicatorFamilyAssessment, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    aggregate_family_assessment: FinancialIndicatorAggregateAssessment | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_assessment_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection | None
    ) = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _validate_manifest_versions(self) -> FinancialManifestAuditRequest:
        metadata_values = (
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
        )
        expected_assessment_identities = tuple(
            item.assessment_identity
            for manifest in self.manifests
            for item in manifest.metadata_compatibility_assessments
        )
        if self.contract_version in {
            "financial-manifest-request-audit-v4",
            "financial-manifest-request-audit-v5",
        }:
            windowed = self.contract_version == "financial-manifest-request-audit-v5"
            expected_manifest_version = (
                "financial-manifest-audit-v5"
                if windowed
                else "financial-manifest-audit-v4"
            )
            if (
                any(
                    manifest.contract_version != expected_manifest_version
                    or manifest.temporal_eligibility_binding
                    != self.temporal_eligibility_binding
                    for manifest in self.manifests
                )
                or self.temporal_eligibility_binding is None
                or self.temporal_eligibility_binding.contract_version
                != (
                    "financial-eligibility-temporal-binding-v2"
                    if windowed
                    else "financial-eligibility-temporal-binding-v1"
                )
                or self.as_of_date
                != self.temporal_eligibility_binding.requested_session
                or self.metadata_assessment_identities
                != expected_assessment_identities
            ):
                raise ValueError("financial manifest request audit v4 is incomplete")
        elif self.contract_version == "financial-manifest-request-audit-v3":
            if (
                any(
                    manifest.contract_version != "financial-manifest-audit-v3"
                    for manifest in self.manifests
                )
                or any(value is None for value in metadata_values)
                or self.metadata_assessment_identities
                != expected_assessment_identities
            ):
                raise ValueError("financial manifest request audit v3 is incomplete")
        elif (
            any(
                manifest.contract_version != "financial-manifest-audit-v2"
                for manifest in self.manifests
            )
            or any(value is not None for value in metadata_values)
            or self.metadata_assessment_identities
        ):
            raise ValueError("financial manifest request audit v2 carries indicator metadata")
        if self.contract_version not in {
            "financial-manifest-request-audit-v4",
            "financial-manifest-request-audit-v5",
        } and (
            self.temporal_eligibility_binding is not None
        ):
            raise ValueError("historical manifest audit carries temporal binding")
        return self


class FinancialDispatchAuditProjectionV2(BaseModel):
    """Ticket 11 audit projection for v2 financial manifests."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["2.0"] = FINANCIAL_DISPATCH_MANIFEST_AUDIT_VERSION
    capability_routing_plan_signature: str = Field(
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$"
    )
    financial_manifest_version: Literal["financial-manifest-v2"] = (
        "financial-manifest-v2"
    )
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    request_count: int = Field(ge=0)
    logical_provider_count: int = Field(ge=0)
    logical_candidate_count: int = Field(ge=0)
    acquisition_attempt_count: int = Field(ge=0)
    duplicate_suppressed_count: int = Field(ge=0)
    requests: tuple[FinancialDispatchAuditRequest, ...] = ()
    manifest_requests: tuple[FinancialManifestAuditRequest, ...] = ()
    completeness_rows: tuple[FinancialCompletenessAuditRow, ...] = ()

    @model_validator(mode="after")
    def _validate_totals(self) -> FinancialDispatchAuditProjectionV2:
        manifest_bindings = tuple(
            manifest.financial_eligibility_binding
            for request in self.manifest_requests
            for manifest in request.manifests
        )
        if manifest_bindings and any(
            binding != self.financial_eligibility_binding
            for binding in manifest_bindings
        ):
            raise ValueError("financial audit eligibility binding is inconsistent")
        if self.request_count != len(self.requests) + len(self.manifest_requests):
            raise ValueError("financial manifest audit request count is inconsistent")
        if self.logical_provider_count != sum(
            item.logical_provider_count for item in self.manifest_requests
        ):
            raise ValueError("financial manifest logical provider count is inconsistent")
        if self.logical_candidate_count != sum(
            item.logical_candidate_count for item in self.manifest_requests
        ):
            raise ValueError("financial manifest logical candidate count is inconsistent")
        if self.acquisition_attempt_count != (
            sum(item.acquisition_attempt_count for item in self.requests)
            + sum(item.physical_request_count for item in self.manifest_requests)
        ):
            raise ValueError("financial manifest physical request count is inconsistent")
        if self.duplicate_suppressed_count != (
            sum(item.duplicate_suppressed_count for item in self.requests)
            + sum(
                item.duplicate_suppressed_count for item in self.manifest_requests
            )
        ):
            raise ValueError("financial manifest duplicate count is inconsistent")
        return self


class FinancialDispatchAuditProjectionV3(FinancialDispatchAuditProjectionV2):
    """05B audit projection for plan-1.2 indicator compatibility state."""

    contract_version: Literal["3.0"] = (
        FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_AUDIT_VERSION
    )
    financial_indicator_manifest_version: Literal["financial-manifest-v3"] = (
        "financial-manifest-v3"
    )
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] = "financial-indicator-field-semantics-v1"
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] = "financial-indicator-metadata-compatibility-v1"
    metadata_assessment_identities: tuple[str, ...]
    metadata_compatibility_states: tuple[
        Literal["compatible", "incompatible"], ...
    ]
    metadata_incompatibility_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_indicator_bindings(self) -> FinancialDispatchAuditProjectionV3:
        indicator_requests = tuple(
            request
            for request in self.manifest_requests
            if request.contract_version == "financial-manifest-request-audit-v3"
        )
        indicator_manifests = tuple(
            manifest
            for request in indicator_requests
            for manifest in request.manifests
        )
        assessments = tuple(
            item
            for manifest in indicator_manifests
            for item in manifest.metadata_compatibility_assessments
        )
        expected_reasons = tuple(
            sorted(
                {
                    reason.reason
                    for manifest in indicator_manifests
                    for rejection in manifest.metadata_incompatible_rejections
                    for reason in rejection.typed_reasons
                }
            )
        )
        if (
            not indicator_requests
            or any(
                request.contract_version
                not in {
                    "financial-manifest-request-audit-v2",
                    "financial-manifest-request-audit-v3",
                }
                for request in self.manifest_requests
            )
            or self.metadata_assessment_identities
            != tuple(item.assessment_identity for item in assessments)
            or self.metadata_compatibility_states
            != tuple(item.compatibility_state for item in assessments)
            or self.metadata_incompatibility_reasons != expected_reasons
            or any(
                row.contract_version != "financial-completeness-row-v4"
                for row in self.completeness_rows
                if row.capability is FinancialCapability.RATIO_FAMILY
            )
        ):
            raise ValueError("financial dispatch audit 3.0 binding is incomplete")
        _assert_safe_indicator_operational_projection(self)
        return self


class FinancialDispatchAuditProjectionV4(FinancialDispatchAuditProjectionV2):
    """05C audit projection for plan-1.3 temporal replay state."""

    contract_version: Literal["4.0"] = FINANCIAL_DISPATCH_TEMPORAL_AUDIT_VERSION
    financial_manifest_version: Literal["financial-manifest-v4"] = (
        "financial-manifest-v4"
    )
    financial_indicator_manifest_version: Literal["financial-manifest-v4"] = (
        "financial-manifest-v4"
    )
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] = "financial-indicator-field-semantics-v1"
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] = "financial-indicator-metadata-compatibility-v1"
    metadata_assessment_identities: tuple[str, ...] = ()
    metadata_compatibility_states: tuple[
        Literal["compatible", "incompatible"], ...
    ] = ()
    metadata_incompatibility_reasons: tuple[str, ...] = ()
    temporal_eligibility_binding: FinancialTemporalEligibilityCheckpointProjection

    @model_validator(mode="after")
    def _validate_temporal_bindings(self) -> FinancialDispatchAuditProjectionV4:
        windowed = self.contract_version == FINANCIAL_DISPATCH_WINDOWED_AUDIT_VERSION
        expected_request_version = (
            "financial-manifest-request-audit-v5"
            if windowed
            else "financial-manifest-request-audit-v4"
        )
        expected_manifest_version = (
            "financial-manifest-audit-v5"
            if windowed
            else "financial-manifest-audit-v4"
        )
        expected_row_version = (
            "financial-completeness-row-v6"
            if windowed
            else "financial-completeness-row-v5"
        )
        manifests = tuple(
            manifest
            for request in self.manifest_requests
            for manifest in request.manifests
        )
        assessments = tuple(
            assessment
            for manifest in manifests
            for assessment in manifest.metadata_compatibility_assessments
        )
        expected_reasons = tuple(
            sorted(
                {
                    reason.reason
                    for manifest in manifests
                    for rejection in manifest.metadata_incompatible_rejections
                    for reason in rejection.typed_reasons
                }
            )
        )
        if (
            not self.manifest_requests
            or any(
                request.contract_version
                != expected_request_version
                or request.temporal_eligibility_binding
                != self.temporal_eligibility_binding
                for request in self.manifest_requests
            )
            or any(
                manifest.contract_version != expected_manifest_version
                or manifest.temporal_eligibility_binding
                != self.temporal_eligibility_binding
                for manifest in manifests
            )
            or any(
                row.contract_version != expected_row_version
                or row.temporal_eligibility_binding
                != self.temporal_eligibility_binding
                for row in self.completeness_rows
            )
            or self.metadata_assessment_identities
            != tuple(item.assessment_identity for item in assessments)
            or self.metadata_compatibility_states
            != tuple(item.compatibility_state for item in assessments)
            or self.metadata_incompatibility_reasons != expected_reasons
            or self.temporal_eligibility_binding.contract_version
            != (
                "financial-eligibility-temporal-binding-v2"
                if windowed
                else "financial-eligibility-temporal-binding-v1"
            )
        ):
            raise ValueError("financial dispatch temporal audit binding is incomplete")
        _assert_safe_indicator_operational_projection(self)
        return self


class FinancialDispatchAuditProjectionV5(FinancialDispatchAuditProjectionV4):
    """02I audit projection for plan-1.4 acquisition-window replay state."""

    contract_version: Literal["5.0"] = FINANCIAL_DISPATCH_WINDOWED_AUDIT_VERSION
    financial_manifest_version: Literal["financial-manifest-v5"] = (
        "financial-manifest-v5"
    )
    financial_indicator_manifest_version: Literal["financial-manifest-v5"] = (
        "financial-manifest-v5"
    )


class FinancialDispatchAuditProjectionV6(FinancialDispatchAuditProjectionV2):
    """Plan-1.5 diagnostic retaining the multi-source listing proof identity."""

    contract_version: Literal["6.0"] = FINANCIAL_DISPATCH_COMPOSITE_AUDIT_VERSION
    financial_manifest_version: Literal["financial-manifest-v2"] = (
        "financial-manifest-v2"
    )
    financial_indicator_manifest_version: Literal["financial-manifest-v3"] = (
        "financial-manifest-v3"
    )
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] = "financial-indicator-field-semantics-v1"
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] = "financial-indicator-metadata-compatibility-v1"
    metadata_assessment_identities: tuple[str, ...] = ()
    metadata_compatibility_states: tuple[
        Literal["compatible", "incompatible"], ...
    ] = ()
    metadata_incompatibility_reasons: tuple[str, ...] = ()
    temporal_eligibility_binding: FinancialEligibilityTemporalContextBinding
    listing_temporal_proof_identity: str = Field(
        pattern=r"^financial-listing-temporal-proof:v2:[0-9a-f]{64}$"
    )
    effective_artifact_identity: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:=/-]{0,255}$"
    )
    effective_row_identity: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:=/-]{0,255}$"
    )
    security_lookup_artifact_identity: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:=/-]{0,255}$"
    )
    publication_artifact_identity: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:=/-]{0,255}$"
    )
    publication_announcement_identity: str = Field(
        pattern=r"^cninfo-announcement:v1:[0-9]+$"
    )
    document_artifact_identity: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:=/-]{0,255}$",
        exclude_if=lambda value: value is None,
    )
    document_request_identity: str | None = Field(
        default=None,
        pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_composite_listing_projection(self) -> FinancialDispatchAuditProjectionV6:
        binding = self.temporal_eligibility_binding
        decision = binding.listing_temporal_decision
        proof = binding.composite_listing_temporal_proof
        if (
            binding.contract_version != "financial-eligibility-temporal-binding-v3"
            or proof is None
            or not isinstance(decision, CompositeListingTemporalEligibilityDecision)
            or decision.result != "accepted"
            or decision.proof_identity != proof.proof_identity
            or self.listing_temporal_proof_identity != proof.proof_identity
            or self.effective_artifact_identity
            != decision.effective_artifact_identity
            or self.effective_row_identity != decision.effective_row_identity
            or self.security_lookup_artifact_identity
            != decision.security_lookup_artifact_identity
            or self.publication_artifact_identity
            != decision.publication_artifact_identity
            or self.publication_announcement_identity
            != decision.publication_announcement_identity
            or self.document_artifact_identity
            != decision.document_artifact_identity
            or self.document_request_identity
            != decision.document_request_identity
        ):
            raise ValueError("financial dispatch composite listing audit is inconsistent")
        _assert_safe_indicator_operational_projection(self)
        return self


class FinancialToolManifestOperationalProjection(BaseModel):
    """Tool-bound manifest references without coordinator or payload details."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-tool-manifest-v2",
        "financial-tool-manifest-v3",
        "financial-tool-manifest-v4",
        "financial-tool-manifest-v5",
    ] = (
        "financial-tool-manifest-v2"
    )
    manifest_identity: str = Field(
        pattern=r"^financial-manifest:v[2345]:[0-9a-f]{64}$"
    )
    capability_routing_plan_signature: str = Field(
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$"
    )
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    capability: FinancialCapability
    statement_or_ratio_family: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    disposition: FinancialEvidenceDisposition
    requested_analysis_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_scope: FinancialAvailabilityScope | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    availability_basis: FinancialAvailabilityBasis | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    eligibility_policy_version: Literal[
        "mainland-financial-period-eligibility-v2"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    availability_reason: FinancialPeriodRejectionReason | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    final_rendered_artifact_identity: str = Field(
        pattern=r"^financial-rendered-artifact:v1:[0-9a-f]{64}$"
    )
    artifact_identities: tuple[str, ...]
    acquisition_outcomes: tuple[FinancialAcquisitionOutcome, ...]
    physical_attempt_event_identities: tuple[str, ...]
    candidate_identities: tuple[str, ...]
    selections: tuple[FinancialPeriodSelection, ...]
    rejected_periods: tuple[FinancialRejectedPeriod, ...]
    overlaps: tuple[FinancialPeriodOverlapFinding, ...]
    conflicts: tuple[FinancialCriticalValueConflict, ...]
    revision_selections: tuple[FinancialRevisionSelection, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    aggregate_completeness: FinancialAggregateCompletenessProjection
    publication_target_construction: (
        MainlandFinancialPeriodTargetConstruction | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_compatibility_assessments: tuple[
        FinancialIndicatorMetadataAssessmentBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    metadata_incompatible_rejections: tuple[
        FinancialIndicatorMetadataRejectionBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    selected_families: tuple[FinancialRatioFamily, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    unresolved_families: tuple[FinancialRatioFamily, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection | None
    ) = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _validate_safe_projection(self) -> FinancialToolManifestOperationalProjection:
        metadata_values = (
            self.indicator_field_semantics_version,
            self.indicator_metadata_compatibility_version,
        )
        windowed = self.contract_version == "financial-tool-manifest-v5"
        temporal = self.contract_version in {
            "financial-tool-manifest-v4",
            "financial-tool-manifest-v5",
        }
        metadata_forward = (
            self.contract_version == "financial-tool-manifest-v3"
            or temporal
            and self.capability is FinancialCapability.RATIO_FAMILY
        )
        if metadata_forward:
            if (
                not self.manifest_identity.startswith(
                    "financial-manifest:v5:"
                    if windowed
                    else "financial-manifest:v4:"
                    if temporal
                    else "financial-manifest:v3:"
                )
                or any(value is None for value in metadata_values)
                or (
                    bool(self.candidate_identities)
                    and not self.metadata_compatibility_assessments
                )
                or bool(self.selected_families) == bool(self.unresolved_families)
            ):
                raise ValueError("financial tool manifest v3 binding is incomplete")
        elif temporal:
            expected_manifest_prefix = (
                "financial-manifest:v5:" if windowed else "financial-manifest:v4:"
            )
            if (
                not self.manifest_identity.startswith(expected_manifest_prefix)
                or any(value is not None for value in metadata_values)
                or self.metadata_compatibility_assessments
                or self.metadata_incompatible_rejections
                or self.selected_families
                or self.unresolved_families
            ):
                raise ValueError("financial tool manifest v4 binding is incomplete")
        elif (
            not self.manifest_identity.startswith("financial-manifest:v2:")
            or any(value is not None for value in metadata_values)
            or self.metadata_compatibility_assessments
            or self.metadata_incompatible_rejections
            or self.selected_families
            or self.unresolved_families
        ):
            raise ValueError("financial tool manifest v2 carries indicator metadata")
        if temporal != (self.temporal_eligibility_binding is not None):
            raise ValueError("financial tool manifest temporal binding is incomplete")
        if temporal and (
            self.aggregate_completeness.temporal_eligibility_binding
            != self.temporal_eligibility_binding
        ):
            raise ValueError("financial tool manifest temporal binding is inconsistent")
        if temporal and self.temporal_eligibility_binding is not None and (
            self.aggregate_completeness.contract_version
            != (
                "financial-aggregate-completeness-v5"
                if windowed
                else "financial-aggregate-completeness-v4"
            )
            or self.temporal_eligibility_binding.contract_version
            != (
                "financial-eligibility-temporal-binding-v2"
                if windowed
                else "financial-eligibility-temporal-binding-v1"
            )
        ):
            raise ValueError("financial tool manifest temporal version is inconsistent")
        if self.contract_version in {
            "financial-tool-manifest-v3",
            "financial-tool-manifest-v4",
            "financial-tool-manifest-v5",
        }:
            _assert_safe_indicator_operational_projection(self)
        else:
            assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


class FinancialToolMessageManifestEnvelope(BaseModel):
    """Closed operational ToolMessage envelope; model content stays separately bounded."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "financial-tool-message-envelope-v2",
        "financial-tool-message-envelope-v3",
        "financial-tool-message-envelope-v4",
        "financial-tool-message-envelope-v5",
    ] = (
        "financial-tool-message-envelope-v2"
    )
    tool_call_id: str = Field(min_length=1, pattern=r".*\S.*")
    tool_name: str = Field(pattern=ACQUISITION_TOKEN_PATTERN)
    request_ref: str = Field(pattern=r"^financial-request:v1:[0-9a-f]{64}$")
    capability: Literal["company_financials"] = "company_financials"
    disposition: Literal["executed", "duplicate_suppressed"]
    capability_routing_plan_signature: str = Field(
        pattern=r"^mainland-routing-plan:v1:[0-9a-f]{64}$"
    )
    financial_manifest_version: Literal[
        "financial-manifest-v2",
        "financial-manifest-v3",
        "financial-manifest-v4",
        "financial-manifest-v5",
    ] = (
        "financial-manifest-v2"
    )
    financial_eligibility_binding: (
        MainlandFinancialEligibilityProjectionBinding | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    logical_provider_count: int = Field(ge=0)
    logical_candidate_count: int = Field(ge=0)
    physical_request_count: int = Field(ge=0)
    manifests: tuple[FinancialToolManifestOperationalProjection, ...] = Field(
        min_length=1
    )
    completeness_rows: tuple[FinancialCompletenessAuditRow, ...]
    family_assessments: tuple[FinancialIndicatorFamilyAssessment, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    aggregate_family_assessment: FinancialIndicatorAggregateAssessment | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    indicator_field_semantics_version: Literal[
        "financial-indicator-field-semantics-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    indicator_metadata_compatibility_version: Literal[
        "financial-indicator-metadata-compatibility-v1"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    metadata_assessment_identities: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    incompatibility_summary: tuple[
        FinancialIndicatorMetadataRejectionBinding,
        ...,
    ] = Field(default=(), exclude_if=lambda value: not value)
    temporal_eligibility_binding: (
        FinancialTemporalEligibilityCheckpointProjection | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    selected_artifact: SourceArtifact | FinancialRenderedArtifactIdentity

    @model_validator(mode="after")
    def _validate_envelope(self) -> FinancialToolMessageManifestEnvelope:
        if {
            manifest.capability_routing_plan_signature
            for manifest in self.manifests
        } != {self.capability_routing_plan_signature}:
            raise ValueError("financial ToolMessage routing-plan binding is invalid")
        if any(
            manifest.financial_eligibility_binding
            != self.financial_eligibility_binding
            for manifest in self.manifests
        ):
            raise ValueError("financial ToolMessage eligibility binding is invalid")
        if self.contract_version in {
            "financial-tool-message-envelope-v4",
            "financial-tool-message-envelope-v5",
        }:
            windowed = self.contract_version == "financial-tool-message-envelope-v5"
            expected_manifest_version = (
                "financial-manifest-v5" if windowed else "financial-manifest-v4"
            )
            expected_tool_manifest_version = (
                "financial-tool-manifest-v5"
                if windowed
                else "financial-tool-manifest-v4"
            )
            expected_row_version = (
                "financial-completeness-row-v6"
                if windowed
                else "financial-completeness-row-v5"
            )
            expected_assessments = tuple(
                item.assessment_identity
                for manifest in self.manifests
                for item in manifest.metadata_compatibility_assessments
            )
            expected_rejections = tuple(
                item
                for manifest in self.manifests
                for item in manifest.metadata_incompatible_rejections
            )
            if (
                self.financial_manifest_version != expected_manifest_version
                or self.temporal_eligibility_binding is None
                or self.temporal_eligibility_binding.contract_version
                != (
                    "financial-eligibility-temporal-binding-v2"
                    if windowed
                    else "financial-eligibility-temporal-binding-v1"
                )
                or any(
                    manifest.contract_version != expected_tool_manifest_version
                    or manifest.temporal_eligibility_binding
                    != self.temporal_eligibility_binding
                    for manifest in self.manifests
                )
                or any(
                    row.contract_version != expected_row_version
                    or row.temporal_eligibility_binding
                    != self.temporal_eligibility_binding
                    for row in self.completeness_rows
                )
                or self.metadata_assessment_identities != expected_assessments
                or self.incompatibility_summary != expected_rejections
                or not isinstance(
                    self.selected_artifact,
                    FinancialRenderedArtifactIdentity,
                )
            ):
                raise ValueError("financial ToolMessage v4 binding is incomplete")
        elif self.contract_version == "financial-tool-message-envelope-v3":
            expected_assessments = tuple(
                item.assessment_identity
                for manifest in self.manifests
                for item in manifest.metadata_compatibility_assessments
            )
            expected_rejections = tuple(
                item
                for manifest in self.manifests
                for item in manifest.metadata_incompatible_rejections
            )
            if (
                self.financial_manifest_version != "financial-manifest-v3"
                or any(
                    manifest.contract_version != "financial-tool-manifest-v3"
                    for manifest in self.manifests
                )
                or self.indicator_field_semantics_version is None
                or self.indicator_metadata_compatibility_version is None
                or self.metadata_assessment_identities != expected_assessments
                or self.incompatibility_summary != expected_rejections
                or not isinstance(
                    self.selected_artifact,
                    FinancialRenderedArtifactIdentity,
                )
            ):
                raise ValueError("financial ToolMessage v3 binding is incomplete")
        elif (
            self.financial_manifest_version != "financial-manifest-v2"
            or any(
                manifest.contract_version != "financial-tool-manifest-v2"
                for manifest in self.manifests
            )
            or self.indicator_field_semantics_version is not None
            or self.indicator_metadata_compatibility_version is not None
            or self.metadata_assessment_identities
            or self.incompatibility_summary
            or not isinstance(self.selected_artifact, SourceArtifact)
        ):
            raise ValueError("financial ToolMessage v2 carries indicator metadata")
        if self.logical_candidate_count != len(
            {
                candidate_id
                for manifest in self.manifests
                for candidate_id in manifest.candidate_identities
            }
        ):
            raise ValueError("financial ToolMessage candidate count is inconsistent")
        attempt_ids = {
            event_id
            for manifest in self.manifests
            for event_id in manifest.physical_attempt_event_identities
        }
        if self.physical_request_count != len(attempt_ids):
            raise ValueError("financial ToolMessage attempt count is inconsistent")
        rendered_digests = {
            manifest.final_rendered_artifact_identity.rpartition(":")[2]
            for manifest in self.manifests
        }
        selected_digest = (
            self.selected_artifact.content_sha256
            if isinstance(self.selected_artifact, FinancialRenderedArtifactIdentity)
            else self.selected_artifact.artifact_sha256
        )
        source_binding_invalid = (
            False
            if isinstance(self.selected_artifact, FinancialRenderedArtifactIdentity)
            else self.selected_artifact.tool_call_id != self.tool_call_id
            or self.selected_artifact.tool_name != self.tool_name
            or self.selected_artifact.source_ref != self.request_ref
        )
        if source_binding_invalid or rendered_digests != {selected_digest}:
            raise ValueError("financial ToolMessage selected artifact is invalid")
        if self.contract_version in {
            "financial-tool-message-envelope-v3",
            "financial-tool-message-envelope-v4",
            "financial-tool-message-envelope-v5",
        }:
            _assert_safe_indicator_operational_projection(self)
        else:
            assert_safe_financial_projection(self.model_dump(mode="json"))
        return self


FinancialDispatchAuditProjection = (
    FinancialDispatchAuditProjectionV1
    | FinancialDispatchAuditProjectionV2
    | FinancialDispatchAuditProjectionV3
    | FinancialDispatchAuditProjectionV4
    | FinancialDispatchAuditProjectionV5
    | FinancialDispatchAuditProjectionV6
)


class FinancialToolDispatcher:
    """Run-scoped owner of financial request identity and acquisition policy."""

    def __init__(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        run_asset_configuration: (
            RunAssetConfiguration | RunAssetConfigurationProjection | None
        ) = None,
        provider_chains: Mapping[str, tuple[FinancialProvider, ...]],
        capability_routing_plan_signature: str | None = None,
        acquisition_policy_version: str = DEFAULT_FINANCIAL_ACQUISITION_POLICY_VERSION,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        checkpoint_ledger: (
            Mapping[str, Any] | FinancialDispatchCheckpointLedger | None
        ) = None,
        provider_subrequest_cache: ProviderSubrequestCache | None = None,
        qualified_statement_router: MainlandFinancialCapabilityRouter | None = None,
        run_scope_id: str | None = None,
        financial_eligibility_context: (
            FinancialEligibilityAcquisitionContext | None
        ) = None,
        checkpoint_thread_id: str | None = None,
    ) -> None:
        if not instrument_identity.is_authoritative:
            raise ValueError("financial dispatcher requires authoritative Instrument Identity")
        if (
            run_asset_configuration is not None
            and run_asset_configuration.instrument_identity != instrument_identity
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
            )
        if _ACQUISITION_TOKEN.fullmatch(acquisition_policy_version) is None:
            raise ValueError("financial acquisition-policy version is malformed")
        if (
            capability_routing_plan_signature is not None
            and re.fullmatch(
                r"mainland-routing-plan:v1:[0-9a-f]{64}",
                capability_routing_plan_signature,
            )
            is None
        ):
            raise ValueError("Capability Routing Plan signature is malformed")
        if run_scope_id is not None and _ACQUISITION_TOKEN.fullmatch(run_scope_id) is None:
            raise ValueError("financial run scope identity is malformed")
        if (
            checkpoint_thread_id is not None
            and re.fullmatch(r"[0-9a-f]{16}", checkpoint_thread_id) is None
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
            )
        copied_chains = {
            str(tool_name): tuple(providers)
            for tool_name, providers in provider_chains.items()
        }
        for tool_name, providers in copied_chains.items():
            if tool_name not in _FINANCIAL_TOOL_SPECS:
                raise ValueError(f"unsupported financial tool {tool_name!r}")
            if not providers:
                raise ValueError(f"financial tool {tool_name!r} has no configured providers")
            provider_names = tuple(provider.name for provider in providers)
            if len(provider_names) != len(set(provider_names)):
                raise ValueError("configured financial provider names must be unique")
        self._instrument_identity = instrument_identity
        self._run_asset_configuration = run_asset_configuration
        self._provider_chains = copied_chains
        self._acquisition_policy_version = acquisition_policy_version
        router_signature = (
            qualified_statement_router.routing_plan.plan_signature
            if qualified_statement_router is not None
            else None
        )
        if (
            capability_routing_plan_signature is not None
            and router_signature is not None
            and capability_routing_plan_signature != router_signature
        ):
            raise ValueError("qualified router contradicts Capability Routing Plan")
        self._capability_routing_plan_signature = (
            router_signature or capability_routing_plan_signature
        )
        self._financial_eligibility_binding = (
            qualified_statement_router.routing_plan.financial_eligibility_binding
            if qualified_statement_router is not None
            else None
        )
        self._run_scope_id = run_scope_id
        self._checkpoint_thread_id = checkpoint_thread_id
        self._financial_eligibility_context = financial_eligibility_context
        if financial_eligibility_context is not None:
            try:
                expected_run_scope_id = (
                    "run:"
                    + sha256(
                        f"checkpoint:{checkpoint_thread_id}".encode()
                    ).hexdigest()
                    if checkpoint_thread_id is not None
                    else None
                )
                if (
                    run_scope_id is None
                    or checkpoint_thread_id is None
                    or expected_run_scope_id != run_scope_id
                    or run_asset_configuration is None
                    or provider_subrequest_cache is None
                ):
                    raise ValueError("financial dispatcher context binding is invalid")
                context = validate_financial_eligibility_acquisition_context_binding(
                    financial_eligibility_context,
                    canonical_run_id=run_scope_id,
                    asset_configuration_signature=(
                        run_asset_configuration.asset_configuration_signature
                    ),
                    instrument_identity=instrument_identity,
                    financial_eligibility_binding=self._financial_eligibility_binding,
                    routing_plan_signature=self._capability_routing_plan_signature,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
                ) from exc
            try:
                provider_subrequest_cache.bind_financial_eligibility_context(
                    context_contract_version=context.contract_version,
                    context_identity=context.context_identity,
                    context=(
                        context
                        if context.contract_version
                        in {
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V4,
                        }
                        else None
                    ),
                    capability_routing_plan=(
                        qualified_statement_router.routing_plan
                        if context.contract_version
                        in {
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V4,
                        }
                        and qualified_statement_router is not None
                        else None
                    ),
                )
            except ProviderSubrequestCacheError as exc:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
                ) from exc
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or time.sleep
        self._retry_policy = retry_policy or RetryPolicy()
        self._open_circuits: set[tuple[str, str]] = set()
        self._dispatch_lock = threading.Lock()
        self._dispatch_results: dict[str, Future[FinancialDispatchResult]] = {}
        self._reuse_counts: dict[str, int] = {}
        self._qualified_statement_router = qualified_statement_router
        self._qualified_router_rebind_pending = bool(
            checkpoint_ledger is not None
            and qualified_statement_router is not None
            and not getattr(qualified_statement_router, "_statement_sources", None)
            and not getattr(qualified_statement_router, "_indicator_sources", None)
            and not getattr(qualified_statement_router, "_capability_sources", None)
        )
        self._statement_selection_results: dict[
            str,
            Future[FinancialStatementSelectionDispatchResult],
        ] = {}
        self._statement_selection_reuse_counts: dict[str, int] = {}
        self._indicator_selection_results: dict[
            str,
            Future[FinancialIndicatorSelectionDispatchResult],
        ] = {}
        self._indicator_selection_reuse_counts: dict[str, int] = {}
        self._provider_subrequest_cache = provider_subrequest_cache
        if checkpoint_ledger is not None:
            self._restore_checkpoint_ledger(checkpoint_ledger)

    def bind_qualified_statement_router(
        self,
        router: MainlandFinancialCapabilityRouter,
    ) -> None:
        """Replace a validation-only router without losing restored run state."""

        if not isinstance(router, MainlandFinancialCapabilityRouter):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
            )
        with self._dispatch_lock:
            if not self._qualified_router_rebind_pending:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.UNSAFE_BOUNDARY
                )
            current = self._qualified_statement_router
            if (
                current is None
                or current.routing_plan != router.routing_plan
                or self._capability_routing_plan_signature
                != router.routing_plan.plan_signature
                or self._financial_eligibility_binding
                != router.routing_plan.financial_eligibility_binding
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
                )
            futures = (
                *self._dispatch_results.values(),
                *self._statement_selection_results.values(),
                *self._indicator_selection_results.values(),
            )
            if any(not future.done() for future in futures):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.UNSAFE_BOUNDARY
                )
            self._qualified_statement_router = router
            self._qualified_router_rebind_pending = False

    @property
    def financial_eligibility_context(
        self,
    ) -> FinancialEligibilityAcquisitionContext | None:
        return self._financial_eligibility_context

    @property
    def financial_eligibility_context_identity(self) -> str | None:
        context = self._financial_eligibility_context
        return context.context_identity if context is not None else None

    def restored_statement_selection_request(
        self,
        request: FinancialToolRequest,
    ) -> FinancialStatementRoutingRequest | None:
        """Return the exact retained request for a completed statement selection."""

        canonical_key = self.canonical_request_key(request)
        with self._dispatch_lock:
            shared = self._statement_selection_results.get(
                canonical_key.request_key
            )
        if shared is None:
            return None
        try:
            return shared.result().selection_request
        except BaseException as exc:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
            ) from exc

    def restored_indicator_selection_request(
        self,
        request: FinancialToolRequest,
    ) -> FinancialIndicatorRoutingRequest | None:
        """Return the exact retained request for a completed indicator selection."""

        canonical_key = self.canonical_request_key(request)
        with self._dispatch_lock:
            shared = self._indicator_selection_results.get(
                canonical_key.request_key
            )
        if shared is None:
            return None
        try:
            return shared.result().selection_request
        except BaseException as exc:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
            ) from exc

    @classmethod
    def from_configured_vendors(
        cls,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        run_asset_configuration: (
            RunAssetConfiguration | RunAssetConfigurationProjection | None
        ) = None,
        config: Mapping[str, Any] | None = None,
        vendor_methods: Mapping[str, Mapping[str, object]] | None = None,
        acquisition_policy_version: str = DEFAULT_FINANCIAL_ACQUISITION_POLICY_VERSION,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        checkpoint_ledger: (
            Mapping[str, Any] | FinancialDispatchCheckpointLedger | None
        ) = None,
        provider_subrequest_cache: ProviderSubrequestCache | None = None,
        qualified_statement_router: MainlandFinancialCapabilityRouter | None = None,
        run_scope_id: str | None = None,
        financial_eligibility_context: (
            FinancialEligibilityAcquisitionContext | None
        ) = None,
        checkpoint_thread_id: str | None = None,
    ) -> FinancialToolDispatcher:
        """Snapshot existing vendor precedence into one immutable run plan."""

        if config is None:
            from tradingagents.dataflows.config import get_config

            config = get_config()
        builtin_vendors = vendor_methods is None
        if vendor_methods is None:
            from tradingagents.dataflows.interface import VENDOR_METHODS

            vendor_methods = VENDOR_METHODS
        if qualified_statement_router is not None:
            if str(
                config.get("mainland_capability_routing_mode", "legacy")
            ).strip().casefold() not in {"qualified_v1", "qualified_v1_shadow"}:
                raise ValueError(
                    "qualified financial router requires explicit qualified_v1 mode"
                )
            configured_signature = config.get(
                "mainland_capability_routing_plan_signature"
            )
            if (
                configured_signature is not None
                and str(configured_signature)
                != qualified_statement_router.routing_plan.plan_signature
            ):
                raise ValueError(
                    "configured Capability Routing Plan contradicts qualified router"
                )
        market = _market_from_authoritative_identity(instrument_identity)
        current_statements = None
        provider_chains: dict[str, tuple[FinancialProvider, ...]] = {}
        for tool_name in _FINANCIAL_TOOL_SPECS:
            available = vendor_methods.get(tool_name)
            if not isinstance(available, Mapping) or not available:
                raise ValueError(
                    f"no provider implementations are registered for {tool_name!r}"
                )
            configured = _configured_vendor_value(
                config,
                tool_name=tool_name,
                market=market,
            )
            requested = [token.strip() for token in configured.split(",")]
            explicit = [token for token in requested if token and token != "default"]
            tool_selections = config.get("tool_vendors", {})
            if (
                builtin_vendors
                and qualified_statement_router is None
                and config.get("mainland_capability_routing_mode", "legacy") == "legacy"
                and tool_name in {"get_balance_sheet", "get_income_statement", "get_cashflow"}
                and isinstance(tool_selections, Mapping)
                and tool_name in tool_selections
                and "tushare" in explicit
            ):
                from tradingagents.dataflows.current_tushare_statements import (
                    CurrentTushareStatements,
                )

                if current_statements is None:
                    current_statements = CurrentTushareStatements(
                        instrument_identity=instrument_identity, config=config,
                        clock=clock, sleeper=sleeper,
                    )
                available = {**available, "tushare": current_statements}
            if explicit:
                vendor_names = [name for name in explicit if name in available]
                if not vendor_names:
                    raise ValueError(
                        f"Configured vendor(s) {explicit} not available for "
                        f"{tool_name!r}. Available: {list(available)}."
                    )
            else:
                vendor_names = list(available)
            provider_chains[tool_name] = tuple(
                FinancialProvider(
                    name=provider_name,
                    variants=(FinancialProviderVariant(
                        variant_id=current_statements.variant_id,
                        invoke=current_statements,
                    ),),
                )
                if current_statements is not None and available[provider_name] is current_statements
                else _financial_provider_from_implementation(
                    provider_name,
                    tool_name=tool_name,
                    implementation=available[provider_name],
                    canonical_symbol=instrument_identity.symbol.strip().upper(),
                )
                for provider_name in vendor_names
            )
        return cls(
            instrument_identity=instrument_identity,
            run_asset_configuration=run_asset_configuration,
            provider_chains=provider_chains,
            capability_routing_plan_signature=(
                str(config["mainland_capability_routing_plan_signature"])
                if config.get("mainland_capability_routing_plan_signature") is not None
                else None
            ),
            acquisition_policy_version=acquisition_policy_version,
            retry_policy=retry_policy,
            clock=clock,
            sleeper=sleeper,
            checkpoint_ledger=checkpoint_ledger,
            provider_subrequest_cache=provider_subrequest_cache,
            qualified_statement_router=qualified_statement_router,
            run_scope_id=run_scope_id,
            financial_eligibility_context=financial_eligibility_context,
            checkpoint_thread_id=checkpoint_thread_id,
        )

    def canonical_request_key(
        self,
        request: FinancialToolRequest,
    ) -> CanonicalFinancialRequestKey:
        providers = self._provider_chains.get(request.tool_name)
        if providers is None:
            raise ValueError(
                f"financial tool {request.tool_name!r} has no configured provider chain"
            )
        identity = _canonical_financial_instrument_identity(
            self._instrument_identity
        )
        material_arguments_json = _canonical_json(
            request.canonical_material_arguments(),
            label="material arguments",
        )
        provider_chain_identity = _provider_chain_identity(
            request.tool_name,
            providers,
        )
        payload = {
            "contract_version": FINANCIAL_REQUEST_KEY_VERSION,
            "instrument_identity": identity.model_dump(mode="json"),
            "financial_capability": "company_financials",
            "tool_name": request.tool_name,
            "statement_type": request.statement_type.value,
            "frequency": request.frequency.value,
            "as_of_date": request.as_of_date.isoformat(),
            "material_arguments_json": material_arguments_json,
            "provider_chain_identity": provider_chain_identity,
            "acquisition_policy_version": self._acquisition_policy_version,
        }
        if self._capability_routing_plan_signature is not None:
            payload["capability_routing_plan_signature"] = (
                self._capability_routing_plan_signature
            )
        encoded = _canonical_json(payload, label="financial request key").encode("utf-8")
        return CanonicalFinancialRequestKey(
            **payload,
            request_key=f"financial-request:v1:{sha256(encoded).hexdigest()}",
        )

    def assess_period_candidate(
        self,
        request: FinancialToolRequest,
        candidate: FinancialPeriodCandidate,
        *,
        expected_consolidation_scope: FinancialConsolidationScope,
    ) -> FinancialPeriodCompletenessAssessment:
        """Assess a normalized period at the public dispatcher seam."""

        if request.statement_type is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS:
            raise ValueError(
                "comprehensive fundamentals has no full-statement period contract"
            )
        return assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=request.statement_type,
            requested_ratio_family=None,
            expected_instrument_identity=self._instrument_identity,
            expected_frequency=request.frequency,
            expected_currency=self._instrument_identity.currency,
            expected_consolidation_scope=expected_consolidation_scope,
            pit_as_of_date=request.as_of_date,
        )

    def dispatch(self, request: FinancialToolRequest) -> FinancialDispatchResult:
        """Share one in-flight or terminal result for each exact request key."""

        canonical_key = self.canonical_request_key(request)
        with self._dispatch_lock:
            shared = self._dispatch_results.get(canonical_key.request_key)
            is_leader = shared is None
            if shared is None:
                shared = Future()
                self._dispatch_results[canonical_key.request_key] = shared

        if not is_leader:
            original = shared.result()
            for provider in self._provider_chains[request.tool_name]:
                for variant in provider.variants:
                    validator = getattr(variant.invoke, "validate_reuse", None)
                    if callable(validator):
                        validator(request, original.value)
            with self._dispatch_lock:
                self._reuse_counts[canonical_key.request_key] = (
                    self._reuse_counts.get(canonical_key.request_key, 0) + 1
                )
            return original.model_copy(
                update={
                    "tool_call_id": request.tool_call_id,
                    "disposition": "duplicate_suppressed",
                }
            )

        try:
            result = self._execute_dispatch(request, canonical_key=canonical_key)
        except BaseException as exc:
            shared.set_exception(exc)
            raise
        shared.set_result(result)
        with self._dispatch_lock:
            self._reuse_counts.setdefault(canonical_key.request_key, 0)
        return result

    def dispatch_tool_message(self, request: FinancialToolRequest) -> ToolMessage:
        """Dispatch and render the independently correlated terminal ToolMessage."""

        return self.dispatch(request).to_tool_message()

    def dispatch_statement_selection(
        self,
        request: FinancialToolRequest,
        selection_request: FinancialStatementRoutingRequest,
    ) -> FinancialStatementSelectionDispatchResult:
        """Single-flight one qualified statement plan without synthetic attempts."""

        router = self._qualified_statement_router
        if router is None:
            raise ValueError("qualified financial statement router is not configured")
        binding = self._financial_eligibility_binding
        if binding is not None:
            if (
                selection_request.eligibility_policy_version
                != binding.eligibility_policy_version
                or (
                    selection_request.financial_eligibility_binding is not None
                    and selection_request.financial_eligibility_binding != binding
                )
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
                )
            if selection_request.financial_eligibility_binding is None:
                selection_request = FinancialStatementRoutingRequest.model_validate(
                    {
                        **selection_request.model_dump(mode="python"),
                        "financial_eligibility_binding": binding,
                    }
                )
        elif (
            selection_request.eligibility_policy_version is not None
            or selection_request.financial_eligibility_binding is not None
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
            )
        if (
            request.statement_type is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
            or request.statement_type is not selection_request.statement_type
            or request.as_of_date != _financial_selection_as_of_date(selection_request)
            or selection_request.instrument_identity != self._instrument_identity
        ):
            raise ValueError("financial tool and statement selection requests contradict")
        canonical_key = self.canonical_request_key(request)
        with self._dispatch_lock:
            shared = self._statement_selection_results.get(
                canonical_key.request_key
            )
            is_leader = shared is None
            if shared is None:
                shared = Future()
                self._statement_selection_results[canonical_key.request_key] = (
                    shared
                )

        if not is_leader:
            original = shared.result()
            if original.selection_request != selection_request:
                raise ValueError(
                    "one financial request key cannot bind two selection requests"
                )
            with self._dispatch_lock:
                self._statement_selection_reuse_counts[
                    canonical_key.request_key
                ] = (
                    self._statement_selection_reuse_counts.get(
                        canonical_key.request_key,
                        0,
                    )
                    + 1
                )
            return original.model_copy(
                update={
                    "tool_call_id": request.tool_call_id,
                    "disposition": "duplicate_suppressed",
                }
            )

        try:
            routing_result = router.route_statement(selection_request)
            if self._run_scope_id is not None:
                try:
                    attempt_events = _validate_statement_routing_semantics(
                        routing_result,
                        selection_request=selection_request,
                        router=router,
                        require_complete_attempt_events=True,
                    )
                    routing_result = finalize_statement_routing_result(
                        routing_result,
                        physical_attempt_events=attempt_events,
                        temporal_eligibility_binding=(
                            FinancialTemporalEligibilityCheckpointProjection.model_validate(
                                selection_request.financial_eligibility_context_binding.temporal_eligibility_binding.model_dump(
                                    mode="python"
                                )
                            )
                            if selection_request.contract_version
                            in {"financial-statement-routing-request-v4", "financial-statement-routing-request-v5"}
                            and selection_request.financial_eligibility_context_binding
                            is not None
                            and selection_request.financial_eligibility_context_binding.temporal_eligibility_binding
                            is not None
                            else None
                        ),
                    )
                except (TypeError, ValueError) as exc:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID
                    ) from exc
            result = FinancialStatementSelectionDispatchResult(
                contract_version=(
                    "financial-statement-selection-dispatch-v4"
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    and selection_request.financial_eligibility_context_binding.contract_version
                    == "financial-eligibility-request-binding-v4"
                    else {
                        "financial-statement-routing-result-v1": (
                            "financial-statement-selection-dispatch-v1"
                        ),
                        "financial-statement-routing-result-v2": (
                            "financial-statement-selection-dispatch-v2"
                        ),
                        "financial-statement-routing-result-v3": (
                            "financial-statement-selection-dispatch-v3"
                        ),
                    }[routing_result.contract_version]
                ),
                tool_call_id=request.tool_call_id,
                disposition="executed",
                request_key=canonical_key,
                selection_request=selection_request,
                routing_result=routing_result,
                rendered_value=render_statement_routing_result(routing_result),
            )
        except BaseException as exc:
            shared.set_exception(exc)
            raise
        shared.set_result(result)
        with self._dispatch_lock:
            self._statement_selection_reuse_counts.setdefault(
                canonical_key.request_key,
                0,
            )
        return result

    def dispatch_statement_selection_tool_message(
        self,
        request: FinancialToolRequest,
        selection_request: FinancialStatementRoutingRequest,
    ) -> ToolMessage:
        return self.dispatch_statement_selection(
            request,
            selection_request,
        ).to_tool_message()

    def dispatch_indicator_selection(
        self,
        request: FinancialToolRequest,
        selection_request: FinancialIndicatorRoutingRequest,
    ) -> FinancialIndicatorSelectionDispatchResult:
        """Single-flight one qualified indicator plan without synthetic attempts."""

        router = self._qualified_statement_router
        if router is None:
            raise ValueError("qualified financial indicator router is not configured")
        binding = self._financial_eligibility_binding
        if binding is not None:
            if (
                selection_request.eligibility_policy_version
                != binding.eligibility_policy_version
                or (
                    selection_request.financial_eligibility_binding is not None
                    and selection_request.financial_eligibility_binding != binding
                )
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
                )
            if selection_request.financial_eligibility_binding is None:
                selection_request = FinancialIndicatorRoutingRequest.model_validate(
                    {
                        **selection_request.model_dump(mode="python"),
                        "financial_eligibility_binding": binding,
                    }
                )
        elif (
            selection_request.eligibility_policy_version is not None
            or selection_request.financial_eligibility_binding is not None
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
            )
        if (
            request.statement_type
            is not FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS
            or request.as_of_date != _financial_selection_as_of_date(selection_request)
            or selection_request.instrument_identity != self._instrument_identity
        ):
            raise ValueError("financial tool and indicator selection requests contradict")
        canonical_key = self.canonical_request_key(request)
        with self._dispatch_lock:
            shared = self._indicator_selection_results.get(
                canonical_key.request_key
            )
            is_leader = shared is None
            if shared is None:
                shared = Future()
                self._indicator_selection_results[canonical_key.request_key] = (
                    shared
                )

        if not is_leader:
            original = shared.result()
            if original.selection_request != selection_request:
                raise ValueError(
                    "one financial request key cannot bind two indicator requests"
                )
            with self._dispatch_lock:
                self._indicator_selection_reuse_counts[
                    canonical_key.request_key
                ] = (
                    self._indicator_selection_reuse_counts.get(
                        canonical_key.request_key,
                        0,
                    )
                    + 1
                )
            return original.model_copy(
                update={
                    "tool_call_id": request.tool_call_id,
                    "disposition": "duplicate_suppressed",
                }
            )

        try:
            routing_result = router.route_indicators(selection_request)
            if self._run_scope_id is not None:
                try:
                    attempt_events = _validate_indicator_routing_semantics(
                        routing_result,
                        selection_request=selection_request,
                        router=router,
                        require_complete_attempt_events=True,
                    )
                    routing_result = finalize_indicator_routing_result(
                        routing_result,
                        request=selection_request,
                        physical_attempt_events=attempt_events,
                        temporal_eligibility_binding=(
                            FinancialTemporalEligibilityCheckpointProjection.model_validate(
                                selection_request.financial_eligibility_context_binding.temporal_eligibility_binding.model_dump(
                                    mode="python"
                                )
                            )
                            if selection_request.contract_version
                            in {"financial-indicator-routing-request-v7", "financial-indicator-routing-request-v8"}
                            and selection_request.financial_eligibility_context_binding
                            is not None
                            and selection_request.financial_eligibility_context_binding.temporal_eligibility_binding
                            is not None
                            else None
                        ),
                    )
                except (TypeError, ValueError) as exc:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID
                    ) from exc
            result = FinancialIndicatorSelectionDispatchResult(
                contract_version=(
                    "financial-indicator-selection-dispatch-v5"
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    and selection_request.financial_eligibility_context_binding.contract_version
                    == "financial-eligibility-request-binding-v4"
                    else "financial-indicator-selection-dispatch-v4"
                    if routing_result.contract_version
                    == "financial-indicator-routing-result-v5"
                    else "financial-indicator-selection-dispatch-v3"
                    if routing_result.contract_version
                    == "financial-indicator-routing-result-v4"
                    else "financial-indicator-selection-dispatch-v2"
                    if routing_result.contract_version
                    == "financial-indicator-routing-result-v3"
                    and selection_request.financial_eligibility_context_binding
                    is not None
                    else "financial-indicator-selection-dispatch-v1"
                ),
                tool_call_id=request.tool_call_id,
                disposition="executed",
                request_key=canonical_key,
                selection_request=selection_request,
                routing_result=routing_result,
                rendered_value=render_indicator_routing_result(routing_result),
                capability_routing_plan_signature=(
                    canonical_key.capability_routing_plan_signature
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    else None
                ),
                financial_eligibility_context_contract_version=(
                    selection_request.financial_eligibility_context_binding.context_contract_version
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    else None
                ),
                financial_eligibility_context_identity=(
                    selection_request.financial_eligibility_context_binding.context_identity
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    else None
                ),
                manifest_identities=(
                    tuple(
                        manifest.manifest_identity
                        for manifest in routing_result.family_manifests
                    )
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    else ()
                ),
                metadata_assessment_identities=(
                    tuple(
                        item.assessment_identity
                        for item in routing_result.metadata_compatibility_assessments
                    )
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    else ()
                ),
                provider_artifact_identities=(
                    tuple(
                        item.artifact_identity
                        for item in routing_result.retained_artifacts
                    )
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    else ()
                ),
                candidate_identities=(
                    tuple(
                        sorted(
                            item.candidate.candidate_identity
                            for manifest in routing_result.family_manifests
                            for item in manifest.provider_candidates
                        )
                    )
                    if selection_request.financial_eligibility_context_binding
                    is not None
                    else ()
                ),
            )
        except BaseException as exc:
            shared.set_exception(exc)
            raise
        shared.set_result(result)
        with self._dispatch_lock:
            self._indicator_selection_reuse_counts.setdefault(
                canonical_key.request_key,
                0,
            )
        return result

    def dispatch_indicator_selection_tool_message(
        self,
        request: FinancialToolRequest,
        selection_request: FinancialIndicatorRoutingRequest,
    ) -> ToolMessage:
        return self.dispatch_indicator_selection(
            request,
            selection_request,
        ).to_tool_message()

    def discard_failed_qualified_selection(
        self,
        request: FinancialToolRequest,
    ) -> None:
        """Remove one failed shadow-only terminal before checkpoint projection."""

        canonical_key = self.canonical_request_key(request)
        with self._dispatch_lock:
            for results, reuse_counts in (
                (
                    self._statement_selection_results,
                    self._statement_selection_reuse_counts,
                ),
                (
                    self._indicator_selection_results,
                    self._indicator_selection_reuse_counts,
                ),
            ):
                shared = results.get(canonical_key.request_key)
                if shared is None:
                    continue
                if not shared.done() or shared.exception() is None:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.UNSAFE_BOUNDARY
                    )
                del results[canonical_key.request_key]
                reuse_counts.pop(canonical_key.request_key, None)
                return
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.TERMINAL_OUTCOME_INVALID
        )

    def checkpoint_ledger(self) -> dict[str, Any]:
        """Return JSON-compatible terminal state at a normal checkpoint boundary."""

        asset_configuration = self._run_asset_configuration
        if asset_configuration is None:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
            )
        entries: list[FinancialDispatchCheckpointEntry] = []
        qualified_entries: list[FinancialStatementSelectionCheckpointEntry] = []
        qualified_indicator_entries: list[
            FinancialIndicatorSelectionCheckpointEntry
        ] = []
        with self._dispatch_lock:
            for request_key, shared in sorted(self._dispatch_results.items()):
                if not shared.done():
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.UNSAFE_BOUNDARY
                    )
                try:
                    result = shared.result()
                except BaseException as exc:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.TERMINAL_OUTCOME_INVALID
                    ) from exc
                progress = _financial_provider_variant_progress(result.plan_outcomes)
                entries.append(
                    FinancialDispatchCheckpointEntry(
                        canonical_request_key=result.request_key,
                        provider_variant_progress=progress,
                        consumed_attempt_budget=sum(
                            item.attempts_consumed for item in progress
                        ),
                        terminal=FinancialDispatchTerminalCheckpointState(
                            value=result.value,
                            artifact=result.artifact,
                            plan_outcomes=result.plan_outcomes,
                            terminal_outcome=result.plan_outcomes[-1].outcome,
                            provider=result.provider,
                            variant_id=result.variant_id,
                        ),
                        artifact_sha256=(
                            result.artifact.artifact_sha256
                            if result.artifact is not None
                            else None
                        ),
                        reuse_count=self._reuse_counts.get(request_key, 0),
                    )
                )
            for request_key, shared in sorted(
                self._statement_selection_results.items()
            ):
                if not shared.done():
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.UNSAFE_BOUNDARY
                    )
                try:
                    result = shared.result()
                except BaseException as exc:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                    ) from exc
                qualified_entries.append(
                    FinancialStatementSelectionCheckpointEntry(
                        contract_version=(
                            {
                                "financial-statement-selection-dispatch-v1": (
                                    "financial-statement-selection-checkpoint-v1"
                                ),
                                "financial-statement-selection-dispatch-v2": (
                                    "financial-statement-selection-checkpoint-v2"
                                ),
                                "financial-statement-selection-dispatch-v3": (
                                    "financial-statement-selection-checkpoint-v3"
                                ),
                                "financial-statement-selection-dispatch-v4": (
                                    "financial-statement-selection-checkpoint-v4"
                                ),
                            }[result.contract_version]
                        ),
                        canonical_request_key=result.request_key,
                        terminal=result.model_copy(
                            update={
                                "tool_call_id": "checkpoint-restored",
                                "disposition": "executed",
                            }
                        ),
                        reuse_count=self._statement_selection_reuse_counts.get(
                            request_key,
                            0,
                        ),
                    )
                )
            for request_key, shared in sorted(
                self._indicator_selection_results.items()
            ):
                if not shared.done():
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.UNSAFE_BOUNDARY
                    )
                try:
                    result = shared.result()
                except BaseException as exc:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                    ) from exc
                indicator_forward = result.contract_version in {
                    "financial-indicator-selection-dispatch-v2",
                    "financial-indicator-selection-dispatch-v3",
                    "financial-indicator-selection-dispatch-v4",
                    "financial-indicator-selection-dispatch-v5",
                }
                cache_checkpoint = (
                    ProviderSubrequestCacheCheckpoint.model_validate(
                        self._provider_subrequest_cache.checkpoint()
                    )
                    if indicator_forward
                    and self._provider_subrequest_cache is not None
                    else None
                )
                qualified_indicator_entries.append(
                    FinancialIndicatorSelectionCheckpointEntry(
                        contract_version=(
                            "financial-indicator-selection-checkpoint-v5"
                            if result.contract_version
                            == "financial-indicator-selection-dispatch-v5"
                            else "financial-indicator-selection-checkpoint-v4"
                            if result.contract_version
                            == "financial-indicator-selection-dispatch-v4"
                            else "financial-indicator-selection-checkpoint-v3"
                            if result.contract_version
                            == "financial-indicator-selection-dispatch-v3"
                            else (
                                "financial-indicator-selection-checkpoint-v2"
                                if indicator_forward
                                else "financial-indicator-selection-checkpoint-v1"
                            )
                        ),
                        canonical_request_key=result.request_key,
                        terminal=result.model_copy(
                            update={
                                "tool_call_id": "checkpoint-restored",
                                "disposition": "executed",
                            }
                        ),
                        reuse_count=self._indicator_selection_reuse_counts.get(
                            request_key,
                            0,
                        ),
                        provider_subrequest_cache_contract_version=(
                            cache_checkpoint.contract_version
                            if cache_checkpoint is not None
                            else None
                        ),
                        provider_subrequest_cache_run_scope_id=(
                            cache_checkpoint.run_scope_id
                            if cache_checkpoint is not None
                            else None
                        ),
                    )
                )
        statement_manifests = tuple(
            entry.terminal.routing_result.manifest
            for entry in qualified_entries
        )
        indicator_manifests = tuple(
            manifest
            for entry in qualified_indicator_entries
            for manifest in entry.terminal.routing_result.family_manifests
        )
        qualified_manifests = statement_manifests + indicator_manifests
        routing_plan = (
            self._qualified_statement_router.routing_plan
            if self._qualified_statement_router is not None
            else None
        )
        context = self._financial_eligibility_context
        writes_composite_v18 = (
            routing_plan is not None
            and routing_plan.plan_version == "1.5"
            and context is not None
            and context.contract_version
            == FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V4
        )
        writes_indicator_v15 = (
            routing_plan is not None
            and routing_plan.plan_version == "1.2"
            and bool(qualified_entries or qualified_indicator_entries)
        )
        writes_windowed_v17 = (
            routing_plan is not None
            and routing_plan.plan_version == "1.4"
            and bool(qualified_entries or qualified_indicator_entries)
        )
        writes_temporal_v16 = (
            routing_plan is not None
            and routing_plan.plan_version == "1.3"
            and bool(qualified_entries or qualified_indicator_entries)
        )
        writes_temporal = writes_temporal_v16 or writes_windowed_v17
        temporal_manifest_version = (
            "financial-manifest-v5"
            if writes_windowed_v17
            else "financial-manifest-v4"
        )
        if writes_indicator_v15 and any(
            entry.contract_version
            != "financial-indicator-selection-checkpoint-v2"
            for entry in qualified_indicator_entries
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
            )
        has_manifest_v2 = bool(qualified_manifests) and all(
            manifest.contract_version
            == (temporal_manifest_version if writes_temporal else "financial-manifest-v2")
            for manifest in statement_manifests
        ) and all(
            manifest.contract_version
            == (
                temporal_manifest_version
                if writes_temporal
                else ("financial-manifest-v3" if writes_indicator_v15 else "financial-manifest-v2")
            )
            for manifest in indicator_manifests
        )
        has_eligibility_binding = has_manifest_v2 and all(
            manifest.financial_eligibility_binding
            == self._financial_eligibility_binding
            and manifest.financial_eligibility_binding is not None
            for manifest in qualified_manifests
        )
        expected_context_binding = (
            FinancialEligibilityRequestBinding.from_context(context)
            if context is not None
            else None
        )
        selection_context_bindings = tuple(
            entry.terminal.selection_request.financial_eligibility_context_binding
            for entry in (*qualified_entries, *qualified_indicator_entries)
        )
        has_context_binding = writes_composite_v18 or (
            has_eligibility_binding
            and context is not None
            and bool(selection_context_bindings)
            and all(
                binding == expected_context_binding
                for binding in selection_context_bindings
            )
        )
        if context is not None and not has_context_binding:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
            )
        if has_manifest_v2 and self._run_scope_id is None:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
            )
        ledger = FinancialDispatchCheckpointLedger(
            contract_version=(
                FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION
                if writes_composite_v18
                else FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION
                if writes_windowed_v17 and has_context_binding
                else FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION
                if writes_temporal_v16 and has_context_binding
                else FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION
                if writes_indicator_v15 and has_context_binding
                else FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION
                if has_context_binding
                else (
                    FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION
                    if has_eligibility_binding
                    else (
                        FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION
                        if has_manifest_v2
                        else (
                            FINANCIAL_DISPATCH_QUALIFIED_LEDGER_VERSION
                            if qualified_entries or qualified_indicator_entries
                            else FINANCIAL_DISPATCH_LEDGER_VERSION
                        )
                    )
                )
            ),
            acquisition_policy_version=self._acquisition_policy_version,
            retry_policy=self._retry_policy,
            provider_chain_identities=self._checkpoint_provider_chain_bindings(),
            run_asset_configuration_version=(
                asset_configuration.asset_configuration_version
            ),
            run_asset_configuration_signature=(
                asset_configuration.asset_configuration_signature
            ),
            circuit_state=tuple(
                FinancialCircuitCheckpointState(provider=provider, tool_name=tool_name)
                for provider, tool_name in sorted(self._open_circuits)
            ),
            provider_subrequest_cache=(
                ProviderSubrequestCacheCheckpoint.model_validate(
                    self._provider_subrequest_cache.checkpoint()
                )
                if self._provider_subrequest_cache is not None
                else None
            ),
            capability_routing_plan_signature=(
                self._capability_routing_plan_signature
                if has_manifest_v2 or writes_composite_v18
                else None
            ),
            financial_manifest_version=(
                "financial-manifest-v2"
                if writes_composite_v18
                else (
                    temporal_manifest_version
                    if writes_temporal
                    else "financial-manifest-v2"
                )
                if has_manifest_v2
                else None
            ),
            capability_routing_plan_version=(
                routing_plan.plan_version
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else None
            ),
            financial_indicator_manifest_version=(
                routing_plan.indicator_manifest_contract_version
                if writes_composite_v18
                else (
                    temporal_manifest_version
                    if writes_temporal
                    else routing_plan.indicator_manifest_contract_version
                )
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else None
            ),
            indicator_field_semantics_version=(
                routing_plan.indicator_field_semantics_version
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else None
            ),
            indicator_metadata_compatibility_version=(
                routing_plan.indicator_metadata_compatibility_version
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else None
            ),
            indicator_metadata_assessments=(
                tuple(
                    binding
                    for manifest in indicator_manifests
                    for binding in manifest.metadata_compatibility_assessments
                )
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else ()
            ),
            indicator_metadata_rejections=(
                tuple(
                    rejection
                    for manifest in indicator_manifests
                    for rejection in manifest.metadata_incompatible_rejections
                )
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else ()
            ),
            indicator_provider_artifact_identities=(
                tuple(
                    sorted(
                        {
                            artifact.artifact_identity
                            for entry in qualified_indicator_entries
                            for artifact in entry.terminal.routing_result.retained_artifacts
                        }
                    )
                )
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else ()
            ),
            indicator_candidate_identities=(
                tuple(
                    sorted(
                        {
                            wrapper.candidate.candidate_identity
                            for manifest in indicator_manifests
                            for wrapper in manifest.provider_candidates
                        }
                    )
                )
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else ()
            ),
            indicator_provider_topology=(
                routing_plan.route_for(MainlandCapability.FINANCIAL_INDICATORS)
                if writes_indicator_v15 or writes_temporal or writes_composite_v18
                else ()
            ),
            financial_eligibility_binding=(
                self._financial_eligibility_binding
                if has_eligibility_binding or writes_composite_v18
                else None
            ),
            run_scope_id=(
                self._run_scope_id
                if has_manifest_v2 or writes_composite_v18
                else None
            ),
            checkpoint_thread_id=(
                self._checkpoint_thread_id if has_context_binding else None
            ),
            financial_eligibility_context=(
                context if has_context_binding else None
            ),
            temporal_eligibility_binding=(
                context.temporal_eligibility_binding
                if writes_composite_v18
                else FinancialTemporalEligibilityCheckpointProjection.model_validate(
                    context.temporal_eligibility_binding.model_dump(mode="python")
                )
                if writes_temporal
                and context is not None
                and context.temporal_eligibility_binding is not None
                else None
            ),
            entries=tuple(entries),
            qualified_statement_selections=tuple(qualified_entries),
            qualified_indicator_selections=tuple(qualified_indicator_entries),
        )
        payload = ledger.model_dump(mode="json")
        if ledger.provider_subrequest_cache is None:
            payload.pop("provider_subrequest_cache", None)
        if ledger.capability_routing_plan_signature is None:
            payload.pop("capability_routing_plan_signature", None)
        if ledger.financial_manifest_version is None:
            payload.pop("financial_manifest_version", None)
        if ledger.financial_eligibility_binding is None:
            payload.pop("financial_eligibility_binding", None)
        if ledger.run_scope_id is None:
            payload.pop("run_scope_id", None)
        if ledger.checkpoint_thread_id is None:
            payload.pop("checkpoint_thread_id", None)
        if ledger.financial_eligibility_context is None:
            payload.pop("financial_eligibility_context", None)
        if not ledger.qualified_statement_selections:
            payload.pop("qualified_statement_selections", None)
        if not ledger.qualified_indicator_selections:
            payload.pop("qualified_indicator_selections", None)
        return payload

    def _checkpoint_provider_chain_bindings(
        self,
    ) -> tuple[FinancialProviderChainCheckpointBinding, ...]:
        return tuple(
            FinancialProviderChainCheckpointBinding(
                tool_name=tool_name,
                provider_chain_identity=_provider_chain_identity(tool_name, providers),
            )
            for tool_name, providers in sorted(self._provider_chains.items())
        )

    def _restore_checkpoint_ledger(
        self,
        checkpoint_ledger: Mapping[str, Any] | FinancialDispatchCheckpointLedger,
    ) -> None:
        if self._financial_eligibility_binding is not None:
            raw_version = (
                checkpoint_ledger.get("contract_version")
                if isinstance(checkpoint_ledger, Mapping)
                else checkpoint_ledger.contract_version
            )
            raw_binding = (
                checkpoint_ledger.get("financial_eligibility_binding")
                if isinstance(checkpoint_ledger, Mapping)
                else checkpoint_ledger.financial_eligibility_binding
            )
            try:
                restored_binding = (
                    restore_mainland_financial_eligibility_projection_binding(
                        raw_binding
                    )
                )
            except MainlandFinancialEligibilityPolicyError as exc:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
                ) from exc
            if (
                raw_version
                not in {
                    FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
                    FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
                    FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
                    FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
                    FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
                    FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION,
                }
                or restored_binding != self._financial_eligibility_binding
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
                )
        try:
            ledger = FinancialDispatchCheckpointLedger.model_validate(checkpoint_ledger)
        except (TypeError, ValueError) as exc:
            raw_version = (
                checkpoint_ledger.get("contract_version")
                if isinstance(checkpoint_ledger, Mapping)
                else checkpoint_ledger.contract_version
            )
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
                if raw_version
                in {
                    FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
                    FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
                    FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION,
                }
                else FinancialDispatchCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
                if raw_version == FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION
                else (
                    FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID
                    if raw_version in {
                        FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION,
                        FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
                        FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
                    }
                    else FinancialDispatchCheckpointFailureReason.MALFORMED
                )
            ) from exc
        asset_configuration = self._run_asset_configuration
        if (
            asset_configuration is None
            or ledger.run_asset_configuration_version
            != asset_configuration.asset_configuration_version
            or ledger.run_asset_configuration_signature
            != asset_configuration.asset_configuration_signature
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
            )
        if (
            ledger.acquisition_policy_version != self._acquisition_policy_version
            or ledger.retry_policy != self._retry_policy
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.POLICY_MISMATCH
            )
        router = self._qualified_statement_router
        if (
            router is not None
            and router.routing_plan.plan_version == "1.2"
            and (
                ledger.contract_version
                != FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION
                or ledger.capability_routing_plan_version != "1.2"
                or ledger.financial_indicator_manifest_version
                != router.routing_plan.indicator_manifest_contract_version
                or ledger.indicator_field_semantics_version
                != router.routing_plan.indicator_field_semantics_version
                or ledger.indicator_metadata_compatibility_version
                != router.routing_plan.indicator_metadata_compatibility_version
                or ledger.indicator_provider_topology
                != router.routing_plan.route_for(
                    MainlandCapability.FINANCIAL_INDICATORS
                )
            )
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
            )
        if (
            router is not None
            and router.routing_plan.plan_version != "1.2"
            and ledger.contract_version
            == FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
            )
        if (
            router is not None
            and router.routing_plan.plan_version == "1.3"
            and (
                ledger.contract_version != FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION
                or ledger.capability_routing_plan_version != "1.3"
                or ledger.financial_manifest_version != "financial-manifest-v4"
                or ledger.financial_indicator_manifest_version
                != "financial-manifest-v4"
                or ledger.temporal_eligibility_binding is None
            )
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
            )
        if (
            router is not None
            and router.routing_plan.plan_version == "1.4"
            and (
                ledger.contract_version != FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION
                or ledger.capability_routing_plan_version != "1.4"
                or ledger.financial_manifest_version != "financial-manifest-v5"
                or ledger.financial_indicator_manifest_version
                != "financial-manifest-v5"
                or ledger.temporal_eligibility_binding is None
            )
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
            )
        if (
            router is not None
            and router.routing_plan.plan_version == "1.5"
            and (
                ledger.contract_version
                != FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION
                or ledger.capability_routing_plan_version != "1.5"
                or not isinstance(
                    ledger.temporal_eligibility_binding,
                    FinancialEligibilityTemporalContextBinding,
                )
                or ledger.temporal_eligibility_binding.contract_version
                != "financial-eligibility-temporal-binding-v3"
            )
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
            )
        if ledger.contract_version in {
            FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
            FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
            FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION,
        }:
            restored_context = ledger.financial_eligibility_context
            if restored_context is None or ledger.checkpoint_thread_id is None:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_MISSING
                )
            if (
                self._financial_eligibility_context is not None
                and self._financial_eligibility_context != restored_context
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_DISPATCHER_MISMATCH
                )
            if (
                self._checkpoint_thread_id is not None
                and self._checkpoint_thread_id != ledger.checkpoint_thread_id
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
                )
            self._financial_eligibility_context = restored_context
            self._checkpoint_thread_id = ledger.checkpoint_thread_id
            if self._provider_subrequest_cache is None:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_CACHE_MISMATCH
                )
            try:
                self._provider_subrequest_cache.bind_financial_eligibility_context(
                    context_contract_version=restored_context.contract_version,
                    context_identity=restored_context.context_identity,
                    context=(
                        restored_context
                        if restored_context.contract_version
                        in {
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V4,
                        }
                        else None
                    ),
                    capability_routing_plan=(
                        router.routing_plan
                        if restored_context.contract_version
                        in {
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V2,
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3,
                            FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V4,
                        }
                        and router is not None
                        else None
                    ),
                )
            except ProviderSubrequestCacheError as exc:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_CACHE_MISMATCH
                ) from exc
        elif self._financial_eligibility_context is not None:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ELIGIBILITY_CONTEXT_MISSING
            )
        if ledger.contract_version in {
            FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION,
            FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
            FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
            FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION,
        }:
            if ledger.run_scope_id != self._run_scope_id:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
                )
            if (
                ledger.capability_routing_plan_signature
                != self._capability_routing_plan_signature
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
                )
        elif (
            ledger.contract_version == FINANCIAL_DISPATCH_LEDGER_VERSION
            and self._qualified_statement_router is not None
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
            )
        restored_chains = {
            item.tool_name: item.provider_chain_identity
            for item in ledger.provider_chain_identities
        }
        configured_chains = {
            item.tool_name: item.provider_chain_identity
            for item in self._checkpoint_provider_chain_bindings()
        }
        if (
            len(restored_chains) != len(ledger.provider_chain_identities)
            or restored_chains != configured_chains
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.PROVIDER_CHAIN_MISMATCH
            )
        if ledger.provider_subrequest_cache is not None:
            if self._provider_subrequest_cache is None:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
                )
            try:
                active_cache = ProviderSubrequestCacheCheckpoint.model_validate(
                    self._provider_subrequest_cache.checkpoint()
                )
            except (ProviderSubrequestCacheError, TypeError, ValueError) as exc:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
                ) from exc
            if active_cache != ledger.provider_subrequest_cache:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.PROVIDER_SUBREQUEST_CACHE_INVALID
                )
        request_keys: set[str] = set()
        for entry in ledger.entries:
            request_key = entry.canonical_request_key.request_key
            if request_key in request_keys:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.CANONICAL_REQUEST_MISMATCH
                )
            request_keys.add(request_key)
            self._validate_checkpoint_entry(entry, ledger=ledger)
        self._validate_checkpoint_circuit_state(ledger)
        self._open_circuits = {
            (item.provider, item.tool_name) for item in ledger.circuit_state
        }
        for entry in ledger.entries:
            terminal = entry.terminal
            result = FinancialDispatchResult(
                tool_call_id="checkpoint-restored",
                disposition="executed",
                request_key=entry.canonical_request_key,
                value=terminal.value,
                artifact=terminal.artifact,
                plan_outcomes=terminal.plan_outcomes,
                provider=terminal.provider,
                variant_id=terminal.variant_id,
            )
            shared: Future[FinancialDispatchResult] = Future()
            shared.set_result(result)
            self._dispatch_results[entry.canonical_request_key.request_key] = shared
            self._reuse_counts[entry.canonical_request_key.request_key] = entry.reuse_count
        self._restore_qualified_statement_selections(ledger)
        self._restore_qualified_indicator_selections(ledger)

    def _restore_qualified_statement_selections(
        self,
        ledger: FinancialDispatchCheckpointLedger,
    ) -> None:
        entries = ledger.qualified_statement_selections
        if not entries:
            return
        router = self._qualified_statement_router
        if router is None:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
            )
        seen: set[str] = set()
        for entry in entries:
            key = entry.canonical_request_key
            result = entry.terminal
            if (
                key.request_key in seen
                or not _canonical_financial_request_key_is_valid(key)
                or key.capability_routing_plan_signature
                != router.routing_plan.plan_signature
                or result.routing_result.manifest.capability_routing_plan_signature
                != router.routing_plan.plan_signature
                or result.selection_request.instrument_identity
                != self._instrument_identity
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                )
            try:
                _validate_statement_routing_semantics(
                    result.routing_result,
                    selection_request=result.selection_request,
                    router=router,
                    require_complete_attempt_events=(
                        ledger.contract_version
                        in {
                            FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION,
                            FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
                            FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
                            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
                            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
                        }
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                ) from exc
            providers = self._provider_chains.get(key.tool_name)
            if (
                providers is None
                or key.provider_chain_identity
                != _provider_chain_identity(key.tool_name, providers)
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                )
            seen.add(key.request_key)
            shared: Future[FinancialStatementSelectionDispatchResult] = Future()
            shared.set_result(result)
            self._statement_selection_results[key.request_key] = shared
            self._statement_selection_reuse_counts[key.request_key] = (
                entry.reuse_count
            )

    def _restore_qualified_indicator_selections(
        self,
        ledger: FinancialDispatchCheckpointLedger,
    ) -> None:
        entries = ledger.qualified_indicator_selections
        if not entries:
            return
        router = self._qualified_statement_router
        if router is None:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
            )
        seen: set[str] = set()
        for entry in entries:
            key = entry.canonical_request_key
            result = entry.terminal
            signatures = {
                manifest.capability_routing_plan_signature
                for manifest in result.routing_result.family_manifests
            }
            if (
                key.request_key in seen
                or not _canonical_financial_request_key_is_valid(key)
                or key.capability_routing_plan_signature
                != router.routing_plan.plan_signature
                or signatures != {router.routing_plan.plan_signature}
                or result.selection_request.instrument_identity
                != self._instrument_identity
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                )
            try:
                _validate_indicator_routing_semantics(
                    result.routing_result,
                    selection_request=result.selection_request,
                    router=router,
                    require_complete_attempt_events=(
                        ledger.contract_version
                        in {
                            FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION,
                            FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
                            FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
                            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
                            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
                        }
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                ) from exc
            providers = self._provider_chains.get(key.tool_name)
            if (
                providers is None
                or key.provider_chain_identity
                != _provider_chain_identity(key.tool_name, providers)
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.QUALIFIED_SELECTION_INVALID
                )
            seen.add(key.request_key)
            shared: Future[FinancialIndicatorSelectionDispatchResult] = Future()
            shared.set_result(result)
            self._indicator_selection_results[key.request_key] = shared
            self._indicator_selection_reuse_counts[key.request_key] = (
                entry.reuse_count
            )

    def _validate_checkpoint_entry(
        self,
        entry: FinancialDispatchCheckpointEntry,
        *,
        ledger: FinancialDispatchCheckpointLedger,
    ) -> None:
        key = entry.canonical_request_key
        if (
            key.acquisition_policy_version != ledger.acquisition_policy_version
            or key.instrument_identity
            != _canonical_financial_instrument_identity(self._instrument_identity)
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.CANONICAL_REQUEST_MISMATCH
            )
        providers = self._provider_chains.get(key.tool_name)
        if (
            providers is None
            or key.provider_chain_identity
            != _provider_chain_identity(key.tool_name, providers)
            or not _canonical_financial_request_key_is_valid(key)
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.CANONICAL_REQUEST_MISMATCH
            )
        progress = _financial_provider_variant_progress(entry.terminal.plan_outcomes)
        consumed_budget = sum(item.attempts_consumed for item in progress)
        if (
            entry.provider_variant_progress != progress
            or entry.consumed_attempt_budget != consumed_budget
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
            )
        self._validate_checkpoint_plan_progress(
            entry.terminal.plan_outcomes,
            providers=providers,
            request_key=key.request_key,
            tool_name=key.tool_name,
            open_circuits=frozenset(
                (item.provider, item.tool_name) for item in ledger.circuit_state
            ),
        )
        terminal = entry.terminal
        terminal_outcome = terminal.plan_outcomes[-1]
        if terminal.artifact is None:
            if entry.artifact_sha256 is not None or not isinstance(
                terminal_outcome.outcome,
                SourceAcquisitionUnavailable,
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ARTIFACT_REFERENCE_INVALID
                )
        else:
            if (
                entry.artifact_sha256 != terminal.artifact.artifact_sha256
                or terminal.artifact.source_ref != key.request_key
                or terminal.artifact.tool_name != key.tool_name
                or terminal.value != terminal.artifact.raw_text
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.ARTIFACT_REFERENCE_INVALID
                )
            if (
                not isinstance(terminal_outcome.outcome, SourceAcquisitionAvailable)
                or terminal.provider != terminal_outcome.provider
                or terminal.variant_id != terminal_outcome.variant_id
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.TERMINAL_OUTCOME_INVALID
                )

    def _validate_checkpoint_plan_progress(
        self,
        plan_outcomes: tuple[FinancialPlanOutcome, ...],
        *,
        providers: tuple[FinancialProvider, ...],
        request_key: str,
        tool_name: str,
        open_circuits: frozenset[tuple[str, str]],
    ) -> None:
        last_provider_order = -1
        last_variant_order = -1
        consumed_by_provider: dict[int, list[int]] = {}
        grouped: dict[int, dict[int, list[FinancialPlanOutcome]]] = {}
        for item in plan_outcomes:
            if item.provider_order >= len(providers):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                )
            provider = providers[item.provider_order]
            if (
                item.provider != provider.name
                or item.variant_order >= len(provider.variants)
                or item.variant_id != provider.variants[item.variant_order].variant_id
                or item.outcome.source_ref != request_key
                or item.outcome.capability != "company_financials"
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                )
            if item.provider_order < last_provider_order or (
                item.provider_order == last_provider_order
                and (
                    item.variant_order < last_variant_order
                    or item.variant_order > last_variant_order + 1
                )
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                )
            if item.provider_order != last_provider_order:
                if item.provider_order != last_provider_order + 1:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                    )
                last_provider_order = item.provider_order
                last_variant_order = -1
            if item.variant_order > last_variant_order + 1:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                )
            last_variant_order = item.variant_order
            grouped.setdefault(item.provider_order, {}).setdefault(
                item.variant_order, []
            ).append(item)
            if not (
                isinstance(item.outcome, SourceAcquisitionUnavailable)
                and item.outcome.reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
            ):
                consumed_by_provider.setdefault(item.provider_order, []).append(
                    item.outcome.attempt
                )
        for attempts in consumed_by_provider.values():
            if attempts != list(range(1, len(attempts) + 1)) or len(attempts) > (
                self._retry_policy.max_attempts_per_provider
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                )
        for provider_order, provider in enumerate(providers):
            variant_groups = grouped.get(provider_order)
            if variant_groups is None:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                )
            provider_items = tuple(
                item for items in variant_groups.values() for item in items
            )
            circuit_outcomes = tuple(
                item
                for item in provider_items
                if isinstance(item.outcome, SourceAcquisitionUnavailable)
                and item.outcome.reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
            )
            if circuit_outcomes:
                if (
                    len(provider_items) != 1
                    or provider_items[0].variant_order != 0
                ):
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                    )
                if (provider.name, tool_name) not in open_circuits:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                    )
                continue

            consumed = 0
            variant_orders = tuple(variant_groups)
            for group_index, variant_order in enumerate(variant_orders):
                outcomes = variant_groups[variant_order]
                is_last_configured_variant = variant_order + 1 == len(provider.variants)
                if not is_last_configured_variant and len(outcomes) != 1:
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                    )
                for prior in outcomes[:-1]:
                    if not isinstance(
                        prior.outcome, SourceAcquisitionUnavailable
                    ) or not prior.outcome.retryable:
                        raise FinancialDispatchCheckpointError(
                            FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                        )
                consumed += len(outcomes)
                terminal = outcomes[-1]
                if isinstance(terminal.outcome, SourceAcquisitionAvailable):
                    if terminal is not plan_outcomes[-1]:
                        raise FinancialDispatchCheckpointError(
                            FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                        )
                    return
                remaining = self._retry_policy.max_attempts_per_provider - consumed
                stops_provider = (
                    not terminal.outcome.retryable
                    and terminal.outcome.reason
                    not in {
                        AcquisitionUnavailableReason.NO_DATA,
                        AcquisitionUnavailableReason.MALFORMED_RESPONSE,
                    }
                )
                expects_next_variant = (
                    not stops_provider
                    and remaining > 0
                    and not is_last_configured_variant
                )
                has_next_variant = group_index + 1 < len(variant_orders)
                if expects_next_variant:
                    if (
                        not has_next_variant
                        or variant_orders[group_index + 1] != variant_order + 1
                    ):
                        raise FinancialDispatchCheckpointError(
                            FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                        )
                elif has_next_variant or (
                    terminal.outcome.retryable and remaining > 0
                ):
                    raise FinancialDispatchCheckpointError(
                        FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                    )
            if provider_order + 1 < len(providers) and provider_order + 1 not in grouped:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
                )

    def _validate_checkpoint_circuit_state(
        self,
        ledger: FinancialDispatchCheckpointLedger,
    ) -> None:
        circuit_keys = tuple(
            (item.provider, item.tool_name) for item in ledger.circuit_state
        )
        if len(circuit_keys) != len(set(circuit_keys)):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
            )
        justified: set[tuple[str, str]] = set()
        for entry in ledger.entries:
            terminal_by_provider: dict[int, FinancialPlanOutcome] = {}
            for item in entry.terminal.plan_outcomes:
                terminal_by_provider[item.provider_order] = item
            for item in terminal_by_provider.values():
                if isinstance(
                    item.outcome, SourceAcquisitionUnavailable
                ) and item.outcome.reason in _SYSTEMIC_CIRCUIT_FAILURES:
                    justified.add(
                        (item.provider, entry.canonical_request_key.tool_name)
                    )
        if set(circuit_keys) != justified:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.BUDGET_PROGRESS_INVALID
            )

    def _execute_dispatch(
        self,
        request: FinancialToolRequest,
        *,
        canonical_key: CanonicalFinancialRequestKey,
    ) -> FinancialDispatchResult:
        """Execute the complete ordered plan before publishing a shared result."""

        providers = self._provider_chains[request.tool_name]
        plan_outcomes: list[FinancialPlanOutcome] = []
        acquisition_request = AcquisitionRequest(
            capability="company_financials",
            source_ref=canonical_key.request_key,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
        )
        for provider_order, provider in enumerate(providers):
            circuit_key = (provider.name, request.tool_name)
            if circuit_key in self._open_circuits:
                variant = provider.variants[0]
                plan_outcomes.append(
                    FinancialPlanOutcome(
                        provider=provider.name,
                        provider_order=provider_order,
                        variant_id=variant.variant_id,
                        variant_order=0,
                        outcome=SourceAcquisitionUnavailable(
                            provider=provider.name,
                            provider_order=provider_order,
                            capability=acquisition_request.capability,
                            source_ref=acquisition_request.source_ref,
                            attempt=1,
                            retrieved_at=self._timestamp(),
                            retryable=False,
                            reason=AcquisitionUnavailableReason.CIRCUIT_OPEN,
                        ),
                    )
                )
                continue

            remaining_attempts = self._retry_policy.max_attempts_per_provider
            provider_attempt_offset = 0
            for variant_order, variant in enumerate(provider.variants):
                if remaining_attempts == 0:
                    break
                variants_remaining = len(provider.variants) - variant_order
                variant_attempts = (
                    remaining_attempts if variants_remaining == 1 else 1
                )

                def invoke(
                    _acquisition_request: AcquisitionRequest,
                    *,
                    selected_variant: FinancialProviderVariant = variant,
                ) -> object:
                    canonical_request = request.model_copy(
                        update={
                            "material_arguments": json.loads(
                                canonical_key.material_arguments_json
                            )
                        }
                    )
                    return _invoke_financial_variant(
                        selected_variant,
                        canonical_request,
                        canonical_request_key=canonical_key.request_key,
                        provider_subrequest_cache=self._provider_subrequest_cache,
                    )

                controller = AcquisitionController(
                    providers=(),
                    clock=self._clock,
                    sleeper=self._sleeper,
                    retry_policy=RetryPolicy(
                        max_attempts_per_provider=variant_attempts,
                        backoff_seconds=self._retry_policy.backoff_seconds,
                    ),
                )
                acquired = controller.acquire(
                    acquisition_request,
                    providers=((provider.name, invoke),),
                    validator=lambda value: _validate_financial_payload(
                        value,
                        request=request,
                        canonical_symbol=(
                            canonical_key.instrument_identity.canonical_symbol
                        ),
                    ),
                    serializer=lambda value: value,
                )
                for outcome in acquired.outcomes:
                    normalized = outcome.model_copy(
                        update={
                            "provider": provider.name,
                            "provider_order": provider_order,
                            "attempt": outcome.attempt + provider_attempt_offset,
                        }
                    )
                    plan_outcomes.append(
                        FinancialPlanOutcome(
                            provider=provider.name,
                            provider_order=provider_order,
                            variant_id=variant.variant_id,
                            variant_order=variant_order,
                            outcome=normalized,
                        )
                    )
                consumed_attempts = len(acquired.outcomes)
                provider_attempt_offset += consumed_attempts
                remaining_attempts -= consumed_attempts
                if acquired.artifact is not None:
                    return FinancialDispatchResult(
                        tool_call_id=request.tool_call_id,
                        disposition="executed",
                        request_key=canonical_key,
                        value=acquired.value,
                        artifact=acquired.artifact,
                        plan_outcomes=tuple(plan_outcomes),
                        provider=provider.name,
                        variant_id=variant.variant_id,
                    )
                terminal_outcome = acquired.outcomes[-1]
                if (
                    isinstance(terminal_outcome, SourceAcquisitionUnavailable)
                    and not terminal_outcome.retryable
                    and terminal_outcome.reason
                    not in {
                        AcquisitionUnavailableReason.NO_DATA,
                        AcquisitionUnavailableReason.MALFORMED_RESPONSE,
                    }
                ):
                    break
                has_next_variant = variant_order + 1 < len(provider.variants)
                if (
                    remaining_attempts > 0
                    and has_next_variant
                    and isinstance(terminal_outcome, SourceAcquisitionUnavailable)
                    and terminal_outcome.retryable
                ):
                    delay = (
                        terminal_outcome.retry_after_seconds
                        if terminal_outcome.retry_after_seconds is not None
                        else self._retry_policy.backoff_seconds
                    )
                    self._sleeper(delay)
            if (
                isinstance(terminal_outcome, SourceAcquisitionUnavailable)
                and terminal_outcome.reason in _SYSTEMIC_CIRCUIT_FAILURES
            ):
                self._open_circuits.add(circuit_key)
        return FinancialDispatchResult(
            tool_call_id=request.tool_call_id,
            disposition="executed",
            request_key=canonical_key,
            value=None,
            artifact=None,
            plan_outcomes=tuple(plan_outcomes),
        )

    def _timestamp(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )


def _provider_chain_identity(
    tool_name: str,
    providers: tuple[FinancialProvider, ...],
) -> str:
    payload = {
        "contract_version": FINANCIAL_PROVIDER_CHAIN_VERSION,
        "tool_name": tool_name,
        "providers": [
            {
                "name": provider.name,
                "variants": [variant.variant_id for variant in provider.variants],
            }
            for provider in providers
        ],
    }
    encoded = _canonical_json(payload, label="financial provider chain").encode("utf-8")
    return f"financial-provider-chain:v1:{sha256(encoded).hexdigest()}"


def _canonical_financial_instrument_identity(
    instrument_identity: InstrumentIdentityEvidence,
) -> CanonicalFinancialInstrumentIdentity:
    provenance = instrument_identity.provenance
    if provenance is None:
        raise ValueError("financial dispatcher identity revision is unavailable")
    return CanonicalFinancialInstrumentIdentity(
        canonical_symbol=instrument_identity.symbol.strip().upper(),
        venue=instrument_identity.venue.strip(),
        instrument_kind=instrument_identity.instrument_kind,
        currency=instrument_identity.currency.strip().upper(),
        provenance_provider=provenance.provider,
        provenance_source_ref=provenance.source_ref,
        provenance_retrieved_at=provenance.retrieved_at,
        identity_revision=provenance.artifact_sha256,
    )


def _canonical_financial_request_key_is_valid(
    key: CanonicalFinancialRequestKey,
) -> bool:
    spec = _FINANCIAL_TOOL_SPECS.get(key.tool_name)
    if spec is None or key.statement_type is not spec[0] or key.frequency not in spec[1]:
        return False
    try:
        material_arguments = json.loads(key.material_arguments_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(material_arguments, dict) or _canonical_json(
        material_arguments,
        label="material arguments",
    ) != key.material_arguments_json:
        return False
    payload = key.model_dump(
        mode="json",
        exclude={"request_key"},
        exclude_none=True,
    )
    encoded = _canonical_json(payload, label="financial request key").encode("utf-8")
    return key.request_key == f"financial-request:v1:{sha256(encoded).hexdigest()}"


_STATEMENT_ROUTING_CAPABILITY = {
    FinancialStatementType.BALANCE_SHEET: MainlandCapability.BALANCE_SHEET,
    FinancialStatementType.CASH_FLOW: MainlandCapability.CASH_FLOW,
    FinancialStatementType.INCOME_STATEMENT: MainlandCapability.INCOME_STATEMENT,
}


def _validated_provider_call_attempts(
    provider_calls: tuple[Any, ...],
    *,
    expected_attempt_ids: tuple[str, ...],
    require_complete_attempt_events: bool,
) -> tuple[ProviderSubrequestAttemptEvent, ...]:
    call_attempt_ids = tuple(
        attempt_id
        for call in provider_calls
        for attempt_id in call.physical_attempt_ids
    )
    call_events = tuple(
        event
        for call in provider_calls
        for event in call.physical_attempt_events
    )
    event_ids = tuple(event.attempt_event_id for event in call_events)
    if (
        len(call_attempt_ids) != len(set(call_attempt_ids))
        or set(call_attempt_ids) != set(expected_attempt_ids)
        or len(event_ids) != len(set(event_ids))
    ):
        raise ValueError("financial provider-call attempt binding is invalid")
    if require_complete_attempt_events:
        if set(event_ids) != set(expected_attempt_ids) or any(
            set(call.physical_attempt_ids)
            != {
                event.attempt_event_id
                for event in call.physical_attempt_events
            }
            for call in provider_calls
        ):
            raise ValueError("financial provider-call attempt events are incomplete")
    elif call_events and set(event_ids) != set(expected_attempt_ids):
        raise ValueError("financial provider-call attempt events are contradictory")
    return tuple(sorted(call_events, key=lambda item: item.attempt_event_id))


def _validate_statement_routing_semantics(
    result: FinancialStatementRoutingResult,
    *,
    selection_request: FinancialStatementRoutingRequest,
    router: MainlandFinancialCapabilityRouter,
    require_complete_attempt_events: bool,
) -> tuple[ProviderSubrequestAttemptEvent, ...]:
    capability = _STATEMENT_ROUTING_CAPABILITY[selection_request.statement_type]
    expected_route = router.routing_plan.route_for(capability)
    provider_ids = tuple(call.provider_id for call in result.provider_calls)
    if (
        result.route != expected_route
        or provider_ids != expected_route[: len(provider_ids)]
    ):
        raise ValueError("statement routing result contradicts the active plan")
    manifest = result.manifest
    selected_ids = {
        selection.candidate_identity for selection in manifest.selections
    }
    selected_assessments = tuple(
        item.assessment
        for item in manifest.provider_candidates
        if item.candidate.candidate_identity in selected_ids
    )
    target_construction = result.publication_target_construction
    expected_annual_periods = (
        target_construction.annual_target_period_ends
        if target_construction is not None
        else tuple(
            sorted(
                set(selection_request.eligible_annual_period_ends),
                reverse=True,
            )[:5]
        )
    )
    expected_reporting_periods = (
        target_construction.reporting_target_period_ends
        if target_construction is not None
        else tuple(
            sorted(
                set(selection_request.eligible_reporting_period_ends),
                reverse=True,
            )[:8]
        )
    )
    expected_aggregate = assess_financial_history_coverage(
        instrument_identity=selection_request.instrument_identity,
        statement_type=selection_request.statement_type,
        company_type=selection_request.company_type,
        consolidation_scope=selection_request.consolidation_scope,
        currency=selection_request.currency,
        assessments=selected_assessments,
        eligible_annual_period_ends=expected_annual_periods,
        eligible_reporting_period_ends=expected_reporting_periods,
        listing_date=selection_request.listing_date,
        listing_provenance=selection_request.listing_provenance,
    )
    if (
        manifest.aggregate_completeness != expected_aggregate
        or result.missing_annual_period_ends
        != expected_aggregate.missing_annual_period_ends
        or result.missing_reporting_period_ends
        != expected_aggregate.missing_reporting_period_ends
        or (
            manifest.contract_version == "financial-manifest-v2"
            and manifest.disposition
            is not financial_statement_evidence_disposition(result)
        )
    ):
        raise ValueError("statement routing completeness binding is invalid")
    events = _validated_provider_call_attempts(
        result.provider_calls,
        expected_attempt_ids=result.physical_attempt_ids,
        require_complete_attempt_events=require_complete_attempt_events,
    )
    if manifest.contract_version == "financial-manifest-v2":
        manifest_events = {
            event.attempt_event_id: event
            for event in manifest.physical_attempt_events
        }
        call_events = {event.attempt_event_id: event for event in events}
        if manifest_events != call_events:
            raise ValueError("statement manifest attempt events are contradictory")
    return events


def _validate_indicator_routing_semantics(
    result: FinancialIndicatorRoutingResult,
    *,
    selection_request: FinancialIndicatorRoutingRequest,
    router: MainlandFinancialCapabilityRouter,
    require_complete_attempt_events: bool,
) -> tuple[ProviderSubrequestAttemptEvent, ...]:
    expected_route = router.routing_plan.route_for(
        MainlandCapability.FINANCIAL_INDICATORS
    )
    provider_ids = tuple(call.provider_id for call in result.provider_calls)
    if (
        result.route != expected_route
        or provider_ids != expected_route[: len(provider_ids)]
        or tuple(
            manifest.requested_ratio_family
            for manifest in result.family_manifests
        )
        != selection_request.ratio_families
    ):
        raise ValueError("indicator routing result contradicts the active plan")
    expected_periods = (
        result.publication_target_construction.reporting_target_period_ends
        if result.publication_target_construction is not None
        else tuple(
            sorted(
                set(selection_request.eligible_reporting_period_ends),
                reverse=True,
            )[:8]
        )
    )
    expected_missing: list[
        tuple[FinancialRatioFamily, tuple[date, ...]]
    ] = []
    expected_complete: list[FinancialRatioFamily] = []
    for manifest in result.family_manifests:
        family = manifest.requested_ratio_family
        assert family is not None
        selected_ids = {
            selection.candidate_identity
            for selection in manifest.selections
        }
        selected_assessments = tuple(
            item.assessment
            for item in manifest.provider_candidates
            if item.candidate.candidate_identity in selected_ids
        )
        selected_currencies = {
            assessment.currency for assessment in selected_assessments
        }
        aggregate_currency = (
            next(iter(selected_currencies))
            if len(selected_currencies) == 1
            else selection_request.currency
        )
        expected_aggregate = assess_financial_ratio_history_coverage(
            instrument_identity=selection_request.instrument_identity,
            ratio_family=family,
            company_type=selection_request.company_type,
            consolidation_scope=selection_request.consolidation_scope,
            currency=aggregate_currency,
            assessments=selected_assessments,
            eligible_reporting_period_ends=expected_periods,
        )
        if manifest.aggregate_completeness != expected_aggregate:
            raise ValueError("indicator routing completeness binding is invalid")
        if expected_aggregate.complete:
            expected_complete.append(family)
        if expected_aggregate.missing_reporting_period_ends:
            expected_missing.append(
                (
                    family,
                    expected_aggregate.missing_reporting_period_ends,
                )
            )
        if (
            manifest.contract_version
            in {"financial-manifest-v2", "financial-manifest-v3"}
            and manifest.disposition
            is not financial_indicator_evidence_disposition(
                manifest,
                result,
                selection_request,
            )
        ):
            raise ValueError("indicator routing disposition binding is invalid")
    if (
        result.complete_families != tuple(expected_complete)
        or result.missing_periods_by_family != tuple(expected_missing)
    ):
        raise ValueError("indicator routing family summary is invalid")
    events = _validated_provider_call_attempts(
        result.provider_calls,
        expected_attempt_ids=result.physical_attempt_ids,
        require_complete_attempt_events=require_complete_attempt_events,
    )
    result_events = {
        event.attempt_event_id: event
        for event in result.physical_attempt_events
    }
    call_events = {event.attempt_event_id: event for event in events}
    if result_events and result_events != call_events:
        raise ValueError("indicator routing attempt events are contradictory")
    for manifest in result.family_manifests:
        if manifest.contract_version in {
            "financial-manifest-v2",
            "financial-manifest-v3",
        } and {
            event.attempt_event_id: event
            for event in manifest.physical_attempt_events
        } != call_events:
            raise ValueError("indicator manifest attempt events are contradictory")
    return events


def _financial_provider_variant_progress(
    plan_outcomes: tuple[FinancialPlanOutcome, ...],
) -> tuple[FinancialProviderVariantProgress, ...]:
    progress: dict[tuple[int, int], FinancialProviderVariantProgress] = {}
    for item in plan_outcomes:
        key = (item.provider_order, item.variant_order)
        current = progress.get(key)
        attempts_consumed = int(
            not (
                isinstance(item.outcome, SourceAcquisitionUnavailable)
                and item.outcome.reason is AcquisitionUnavailableReason.CIRCUIT_OPEN
            )
        )
        if current is None:
            progress[key] = FinancialProviderVariantProgress(
                provider=item.provider,
                provider_order=item.provider_order,
                variant_id=item.variant_id,
                variant_order=item.variant_order,
                outcome_count=1,
                attempts_consumed=attempts_consumed,
            )
        else:
            progress[key] = current.model_copy(
                update={
                    "outcome_count": current.outcome_count + 1,
                    "attempts_consumed": (
                        current.attempts_consumed + attempts_consumed
                    ),
                }
            )
    return tuple(progress.values())


def _financial_tool_manifest_envelope(
    *,
    dispatch_result: (
        FinancialStatementSelectionDispatchResult
        | FinancialIndicatorSelectionDispatchResult
    ),
) -> FinancialToolMessageManifestEnvelope:
    routing_result = dispatch_result.routing_result
    if isinstance(dispatch_result, FinancialStatementSelectionDispatchResult):
        manifests = (routing_result.manifest,)
        provider_calls = routing_result.provider_calls
        family_assessments = ()
        aggregate_family_assessment = None
    else:
        manifests = routing_result.family_manifests
        provider_calls = routing_result.provider_calls
        family_assessments = routing_result.family_assessments
        aggregate_family_assessment = routing_result.aggregate_family_assessment
    windowed_v5 = bool(manifests) and all(
        manifest.contract_version == "financial-manifest-v5"
        for manifest in manifests
    )
    temporal_v4 = bool(manifests) and all(
        manifest.contract_version == "financial-manifest-v4"
        for manifest in manifests
    )
    temporal = windowed_v5 or temporal_v4
    temporal_binding = (
        manifests[0].temporal_eligibility_binding if temporal else None
    )
    if temporal and (
        temporal_binding is None
        or any(
            manifest.temporal_eligibility_binding != temporal_binding
            for manifest in manifests
        )
    ):
        raise ValueError("financial ToolMessage temporal binding is inconsistent")
    indicator_v3 = isinstance(
        dispatch_result,
        FinancialIndicatorSelectionDispatchResult,
    ) and all(
        manifest.contract_version == "financial-manifest-v3"
        for manifest in manifests
    )
    indicator_metadata = indicator_v3 or (
        temporal
        and isinstance(
            dispatch_result,
            FinancialIndicatorSelectionDispatchResult,
        )
    )
    provider_attempt_counts = {
        call.provider_id: len(call.physical_attempt_events)
        for call in provider_calls
    }
    projected_manifests = tuple(
        _project_manifest_for_tool(manifest)
        for manifest in manifests
    )
    completeness_rows = tuple(
        row
        for manifest in manifests
        for row in _project_manifest_completeness_rows(
            manifest,
            provider_attempt_counts=provider_attempt_counts,
        )
    )
    return FinancialToolMessageManifestEnvelope(
        contract_version=(
            "financial-tool-message-envelope-v5"
            if windowed_v5
            else "financial-tool-message-envelope-v4"
            if temporal_v4
            else "financial-tool-message-envelope-v3"
            if indicator_v3
            else "financial-tool-message-envelope-v2"
        ),
        tool_call_id=dispatch_result.tool_call_id,
        tool_name=dispatch_result.request_key.tool_name,
        request_ref=dispatch_result.request_key.request_key,
        disposition=dispatch_result.disposition,
        capability_routing_plan_signature=(
            dispatch_result.request_key.capability_routing_plan_signature
        ),
        financial_manifest_version=(
            "financial-manifest-v5"
            if windowed_v5
            else "financial-manifest-v4"
            if temporal_v4
            else "financial-manifest-v3"
            if indicator_v3
            else "financial-manifest-v2"
        ),
        financial_eligibility_binding=(
            routing_result.financial_eligibility_binding
        ),
        logical_provider_count=len(provider_calls),
        logical_candidate_count=len(
            {
                item.candidate.candidate_identity
                for manifest in manifests
                for item in manifest.provider_candidates
            }
        ),
        physical_request_count=len(
            {
                event.attempt_event_id
                for manifest in manifests
                for event in manifest.physical_attempt_events
            }
        ),
        manifests=projected_manifests,
        completeness_rows=tuple(
            sorted(
                completeness_rows,
                key=lambda item: (
                    item.capability.value,
                    item.statement_or_ratio_family,
                    item.period,
                    item.provider,
                    item.artifact_identity or "",
                ),
            )
        ),
        family_assessments=family_assessments,
        aggregate_family_assessment=aggregate_family_assessment,
        indicator_field_semantics_version=(
            "financial-indicator-field-semantics-v1"
            if indicator_metadata
            else None
        ),
        indicator_metadata_compatibility_version=(
            "financial-indicator-metadata-compatibility-v1"
            if indicator_metadata
            else None
        ),
        metadata_assessment_identities=(
            tuple(
                item.assessment_identity
                for manifest in projected_manifests
                for item in manifest.metadata_compatibility_assessments
            )
            if indicator_metadata
            else ()
        ),
        incompatibility_summary=(
            tuple(
                item
                for manifest in projected_manifests
                for item in manifest.metadata_incompatible_rejections
            )
            if indicator_metadata
            else ()
        ),
        temporal_eligibility_binding=temporal_binding,
        selected_artifact=(
            FinancialRenderedArtifactIdentity.create(
                dispatch_result.rendered_value
            )
            if indicator_v3 or temporal
            else SourceArtifact(
                artifact_sha256=sha256(
                    dispatch_result.rendered_value.encode("utf-8")
                ).hexdigest(),
                source_ref=dispatch_result.request_key.request_key,
                tool_call_id=dispatch_result.tool_call_id,
                tool_name=dispatch_result.request_key.tool_name,
                raw_text=dispatch_result.rendered_value,
            )
        ),
    )


def _project_aggregate_completeness(
    completeness: FinancialAggregateCompleteness,
    *,
    manifest: FinancialAcquisitionManifest | None = None,
) -> FinancialAggregateCompletenessProjection:
    serialized = json.dumps(
        completeness.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    common: dict[str, Any] = {
        "assessment_ref": (
            "financial-completeness-assessment=sha256:"
            + sha256(serialized.encode("utf-8")).hexdigest()
        ),
        "company_type": completeness.company_type,
        "consolidation_scope": completeness.consolidation_scope,
        "currency": completeness.currency,
        "target_reporting_period_ends": (
            completeness.target_reporting_period_ends
        ),
        "covered_reporting_period_ends": (
            completeness.covered_reporting_period_ends
        ),
        "missing_reporting_period_ends": (
            completeness.missing_reporting_period_ends
        ),
        "rejection_reasons": completeness.rejection_reasons,
        "complete": completeness.complete,
    }
    if isinstance(completeness, FinancialHistoryCompletenessAssessment):
        return FinancialAggregateCompletenessProjection(
            contract_version=(
                "financial-aggregate-completeness-v5"
                if manifest is not None
                and manifest.contract_version == "financial-manifest-v5"
                else "financial-aggregate-completeness-v4"
                if manifest is not None
                and manifest.contract_version == "financial-manifest-v4"
                else "financial-aggregate-completeness-v2"
            ),
            temporal_eligibility_binding=(
                manifest.temporal_eligibility_binding
                if manifest is not None
                and manifest.contract_version
                in {"financial-manifest-v4", "financial-manifest-v5"}
                else None
            ),
            capability=FinancialCapability.STATEMENT,
            statement_type=completeness.statement_type,
            target_annual_period_ends=completeness.target_annual_period_ends,
            covered_annual_period_ends=completeness.covered_annual_period_ends,
            missing_annual_period_ends=completeness.missing_annual_period_ends,
            since_listing_exception=completeness.since_listing_exception,
            **common,
        )
    if isinstance(completeness, FinancialRatioHistoryCompletenessAssessment):
        indicator_forward = (
            manifest is not None
            and manifest.contract_version
            in {
                "financial-manifest-v3",
                "financial-manifest-v4",
                "financial-manifest-v5",
            }
        )
        windowed_v5 = (
            manifest is not None
            and manifest.contract_version == "financial-manifest-v5"
        )
        temporal_v4 = (
            manifest is not None
            and manifest.contract_version == "financial-manifest-v4"
        )
        temporal = windowed_v5 or temporal_v4
        return FinancialAggregateCompletenessProjection(
            contract_version=(
                "financial-aggregate-completeness-v5"
                if windowed_v5
                else "financial-aggregate-completeness-v4"
                if temporal_v4
                else "financial-aggregate-completeness-v3"
                if indicator_forward
                else "financial-aggregate-completeness-v2"
            ),
            source_contract_version=(
                "financial-period-selection-v3"
                if indicator_forward
                else "financial-period-selection-v1"
            ),
            capability=FinancialCapability.RATIO_FAMILY,
            ratio_family=completeness.ratio_family,
            indicator_field_semantics_version=(
                manifest.indicator_field_semantics_version
                if indicator_forward and manifest is not None
                else None
            ),
            indicator_metadata_compatibility_version=(
                manifest.indicator_metadata_compatibility_version
                if indicator_forward and manifest is not None
                else None
            ),
            metadata_assessment_identities=(
                tuple(
                    item.assessment_identity
                    for item in manifest.metadata_compatibility_assessments
                )
                if indicator_forward and manifest is not None
                else ()
            ),
            compatibility_states=(
                tuple(
                    item.compatibility_state
                    for item in manifest.metadata_compatibility_assessments
                )
                if indicator_forward and manifest is not None
                else ()
            ),
            family_disposition=(
                "selected" if completeness.complete else "unresolved"
            )
            if indicator_forward
            else None,
            temporal_eligibility_binding=(
                manifest.temporal_eligibility_binding if temporal else None
            ),
            **common,
        )
    raise TypeError("unsupported financial aggregate completeness assessment")


def _project_manifest_for_tool(
    manifest: FinancialAcquisitionManifest,
) -> FinancialToolManifestOperationalProjection:
    if (
        manifest.contract_version
        not in {
            "financial-manifest-v2",
            "financial-manifest-v3",
            "financial-manifest-v4",
            "financial-manifest-v5",
        }
        or manifest.disposition is None
        or manifest.final_rendered_artifact is None
    ):
        raise ValueError("financial ToolMessage requires forward manifest lineage")
    return FinancialToolManifestOperationalProjection(
        contract_version=(
            "financial-tool-manifest-v5"
            if manifest.contract_version == "financial-manifest-v5"
            else "financial-tool-manifest-v4"
            if manifest.contract_version == "financial-manifest-v4"
            else "financial-tool-manifest-v3"
            if manifest.contract_version == "financial-manifest-v3"
            else "financial-tool-manifest-v2"
        ),
        manifest_identity=manifest.manifest_identity,
        capability_routing_plan_signature=(
            manifest.capability_routing_plan_signature
        ),
        financial_eligibility_binding=(
            manifest.financial_eligibility_binding
        ),
        capability=manifest.requested_capability,
        statement_or_ratio_family=_manifest_family(manifest),
        disposition=manifest.disposition,
        requested_analysis_scope=manifest.requested_analysis_scope,
        availability_scope=manifest.availability_scope,
        availability_basis=manifest.availability_basis,
        eligibility_policy_version=manifest.eligibility_policy_version,
        availability_reason=manifest.availability_reason,
        final_rendered_artifact_identity=(
            manifest.final_rendered_artifact.artifact_identity
        ),
        artifact_identities=tuple(
            item.artifact.artifact_identity for item in manifest.artifacts
        ),
        acquisition_outcomes=manifest.acquisition_outcomes,
        physical_attempt_event_identities=tuple(
            event.attempt_event_id
            for event in manifest.physical_attempt_events
        ),
        candidate_identities=tuple(
            item.candidate.candidate_identity
            for item in manifest.provider_candidates
        ),
        selections=manifest.selections,
        rejected_periods=manifest.rejected_periods,
        overlaps=manifest.overlaps,
        conflicts=manifest.conflicts,
        revision_selections=manifest.revision_selections,
        aggregate_completeness=_project_aggregate_completeness(
            manifest.aggregate_completeness,
            manifest=manifest,
        ),
        publication_target_construction=(
            manifest.publication_target_construction
        ),
        indicator_field_semantics_version=(
            manifest.indicator_field_semantics_version
        ),
        indicator_metadata_compatibility_version=(
            manifest.indicator_metadata_compatibility_version
        ),
        metadata_compatibility_assessments=(
            manifest.metadata_compatibility_assessments
        ),
        metadata_incompatible_rejections=(
            manifest.metadata_incompatible_rejections
        ),
        selected_families=manifest.selected_families,
        unresolved_families=manifest.unresolved_families,
        temporal_eligibility_binding=manifest.temporal_eligibility_binding,
    )


def _manifest_family(manifest: FinancialAcquisitionManifest) -> str:
    family = (
        manifest.requested_statement_type
        if manifest.requested_capability is FinancialCapability.STATEMENT
        else manifest.requested_ratio_family
    )
    if family is None:
        raise ValueError("financial manifest family is absent")
    return family.value


def _project_manifest_for_audit(
    manifest: FinancialAcquisitionManifest,
) -> FinancialManifestAuditProjection:
    if (
        manifest.contract_version not in {
            "financial-manifest-v2",
            "financial-manifest-v3",
            "financial-manifest-v4",
            "financial-manifest-v5",
        }
        or manifest.disposition is None
        or manifest.final_rendered_artifact is None
    ):
        raise ValueError("financial manifest audit requires forward lineage")
    indicator_v3 = manifest.contract_version == "financial-manifest-v3"
    temporal_v4 = manifest.contract_version == "financial-manifest-v4"
    windowed_v5 = manifest.contract_version == "financial-manifest-v5"
    return FinancialManifestAuditProjection(
        contract_version=(
            "financial-manifest-audit-v5"
            if windowed_v5
            else "financial-manifest-audit-v4"
            if temporal_v4
            else "financial-manifest-audit-v3"
            if indicator_v3
            else "financial-manifest-audit-v2"
        ),
        manifest_identity=manifest.manifest_identity,
        capability_routing_plan_signature=(
            manifest.capability_routing_plan_signature
        ),
        financial_eligibility_binding=(
            manifest.financial_eligibility_binding
        ),
        capability=manifest.requested_capability,
        statement_or_ratio_family=_manifest_family(manifest),
        disposition=manifest.disposition,
        requested_analysis_scope=manifest.requested_analysis_scope,
        availability_scope=manifest.availability_scope,
        availability_basis=manifest.availability_basis,
        eligibility_policy_version=manifest.eligibility_policy_version,
        availability_reason=manifest.availability_reason,
        final_rendered_artifact_identity=(
            manifest.final_rendered_artifact.artifact_identity
        ),
        artifact_identities=tuple(
            item.artifact.artifact_identity for item in manifest.artifacts
        ),
        acquisition_outcomes=manifest.acquisition_outcomes,
        physical_attempt_events=manifest.physical_attempt_events,
        candidate_identities=tuple(
            item.candidate.candidate_identity
            for item in manifest.provider_candidates
        ),
        selections=manifest.selections,
        rejected_periods=manifest.rejected_periods,
        overlaps=manifest.overlaps,
        conflicts=manifest.conflicts,
        revision_selections=manifest.revision_selections,
        aggregate_completeness=_project_aggregate_completeness(
            manifest.aggregate_completeness,
            manifest=manifest,
        ),
        publication_target_construction=(
            manifest.publication_target_construction
        ),
        financial_manifest_version=manifest.contract_version,
        indicator_field_semantics_version=(
            manifest.indicator_field_semantics_version
        ),
        indicator_metadata_compatibility_version=(
            manifest.indicator_metadata_compatibility_version
        ),
        metadata_compatibility_assessments=(
            manifest.metadata_compatibility_assessments
        ),
        metadata_incompatible_rejections=(
            manifest.metadata_incompatible_rejections
        ),
        selected_families=manifest.selected_families,
        unselected_families=manifest.unselected_families,
        unresolved_families=manifest.unresolved_families,
        temporal_eligibility_binding=manifest.temporal_eligibility_binding,
    )


def _project_manifest_completeness_rows(
    manifest: FinancialAcquisitionManifest,
    *,
    provider_attempt_counts: Mapping[str, int],
) -> tuple[FinancialCompletenessAuditRow, ...]:
    indicator_v3 = manifest.contract_version == "financial-manifest-v3"
    temporal_v4 = manifest.contract_version == "financial-manifest-v4"
    windowed_v5 = manifest.contract_version == "financial-manifest-v5"
    temporal = temporal_v4 or windowed_v5
    indicator_metadata = indicator_v3 or (
        temporal
        and manifest.requested_capability is FinancialCapability.RATIO_FAMILY
    )
    selection_by_candidate = {
        item.candidate_identity: item for item in manifest.selections
    }
    rejection_by_candidate = {
        item.candidate_identity: item for item in manifest.rejected_periods
    }
    conflict_candidates = {
        candidate_identity
        for conflict in manifest.conflicts
        for candidate_identity in (
            conflict.selected_candidate_identity,
            conflict.conflicting_candidate_identity,
        )
    }
    overlap_reasons: dict[str, set[str]] = {}
    for overlap in manifest.overlaps:
        overlap_reasons.setdefault(
            overlap.overlapping_candidate_identity,
            set(),
        ).add(overlap.disposition)
    completeness = manifest.aggregate_completeness
    metadata_assessment_by_candidate = {
        item.candidate_identity: item
        for item in manifest.metadata_compatibility_assessments
    }
    since_listing = getattr(completeness, "since_listing_exception", None)
    listing_provenance = (
        since_listing.listing_provenance
        if since_listing is not None
        else None
    )
    rows: list[FinancialCompletenessAuditRow] = []
    for provider_candidate in manifest.provider_candidates:
        candidate = provider_candidate.candidate
        assessment = provider_candidate.assessment
        candidate_id = candidate.candidate_identity
        selection = selection_by_candidate.get(candidate_id)
        rejection = rejection_by_candidate.get(candidate_id)
        metadata_assessment = metadata_assessment_by_candidate.get(candidate_id)
        if candidate_id in conflict_candidates or (
            rejection is not None
            and FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES
            in rejection.reasons
        ):
            disposition = "conflicted"
        elif selection is not None:
            disposition = (
                "current_only"
                if selection.disposition == "current_only"
                else "selected"
            )
        elif rejection is not None:
            disposition = "rejected"
        else:
            disposition = "unselected"
        if indicator_v3:
            if metadata_assessment is None:
                raise ValueError(
                    "indicator completeness row assessment binding is absent"
                )
            if metadata_assessment.compatibility_state == "incompatible":
                disposition = "unselected"
            elif selection is None and manifest.unresolved_families:
                disposition = "unresolved"
        reasons = tuple(
            reason.value for reason in assessment.rejection_reasons
        )
        if not reasons and disposition == "unselected":
            reasons = tuple(
                sorted(overlap_reasons.get(candidate_id, {"not_selected"}))
            )
        resolution = candidate.company_type_resolution
        provider = candidate.artifact.dataset.provider_id
        filing_metadata = candidate.filing_metadata
        availability_scope = (
            selection.availability_scope
            if selection is not None
            else (
                FinancialAvailabilityScope.CURRENT_ONLY
                if manifest.requested_analysis_scope
                is FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE
                and (
                    FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                    in assessment.rejection_reasons
                    or FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
                    in assessment.rejection_reasons
                )
                else manifest.availability_scope
            )
        )
        availability_basis = (
            selection.availability_basis
            if selection is not None
            else (
                FinancialAvailabilityBasis.ANN_DATE_WITHOUT_FIRST_PUBLICATION
                if FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                in assessment.rejection_reasons
                else (
                    FinancialAvailabilityBasis.ANN_DATE_WITH_STRICT_PIT_CUTOFF_FAILURE
                    if FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
                    in assessment.rejection_reasons
                    else manifest.availability_basis
                )
            )
        )
        availability_reason = (
            selection.availability_reason.value
            if selection is not None
            and selection.availability_reason is not None
            else (
                FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT.value
                if FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT
                in assessment.rejection_reasons
                else (
                    FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED.value
                    if FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
                    in assessment.rejection_reasons
                    else (
                        manifest.availability_reason.value
                        if manifest.availability_reason is not None
                        else None
                    )
                )
            )
        )
        filing_metadata_values = (
            filing_metadata.ann_date,
            filing_metadata.f_ann_date,
            filing_metadata.report_type,
            filing_metadata.comp_type,
            filing_metadata.update_flag,
        )
        observed_normalized_units = tuple(sorted({
            field.normalized_unit
            for field in candidate.fields
            if field.normalized_unit is not None
        }))
        declared_normalized_unit = (
            "RATIO"
            if assessment.capability is FinancialCapability.RATIO_FAMILY
            else (
                observed_normalized_units[0]
                if len(observed_normalized_units) == 1
                else None
            )
        )
        rows.append(
            FinancialCompletenessAuditRow(
                contract_version=(
                    "financial-completeness-row-v6"
                    if windowed_v5
                    else "financial-completeness-row-v5"
                    if temporal_v4
                    else "financial-completeness-row-v4"
                    if indicator_metadata
                    else "financial-completeness-row-v3"
                ),
                manifest_identity=manifest.manifest_identity,
                capability=manifest.requested_capability,
                statement_or_ratio_family=_manifest_family(manifest),
                period=assessment.period_end,
                frequency=assessment.frequency,
                provider=provider,
                company_type=assessment.company_type,
                company_type_resolution_method=resolution.method,
                company_type_provenance=tuple(
                    f"{item.key}={item.value}"
                    for item in resolution.classifier_inputs
                ),
                provider_declared_type=resolution.provider_declared_type,
                provider_declaration_qualified=(
                    resolution.provider_declaration_qualified
                ),
                metadata_coverage=(
                    Decimal(
                        sum(
                            item is not None
                            for item in filing_metadata_values
                        )
                    )
                    / Decimal(len(filing_metadata_values))
                ),
                ann_date=filing_metadata.ann_date,
                f_ann_date=filing_metadata.f_ann_date,
                f_ann_date_state=(
                    "present" if filing_metadata.f_ann_date is not None else "absent"
                )
                if manifest.eligibility_policy_version is not None
                else None,
                requested_analysis_scope=manifest.requested_analysis_scope,
                availability_scope=availability_scope,
                availability_basis=availability_basis,
                eligibility_policy_version=manifest.eligibility_policy_version,
                availability_reason=availability_reason,
                report_type=filing_metadata.report_type,
                provider_comp_type=filing_metadata.comp_type,
                update_flag=filing_metadata.update_flag,
                revision_observed_at=filing_metadata.observed_at,
                retrieved_at=filing_metadata.retrieved_at,
                local_provider_revision_identity=(
                    filing_metadata.local_provider_revision_identity
                ),
                provider_filing_revision_id=(
                    filing_metadata.provider_filing_revision_id
                ),
                currency=assessment.currency,
                declared_normalized_unit=declared_normalized_unit,
                observed_normalized_units=observed_normalized_units,
                declared_critical_fields=assessment.declared_critical_fields,
                declared_core_fields=assessment.declared_core_fields,
                mapped_normalized_fields=tuple(sorted({
                    field.normalized_field for field in candidate.fields
                })),
                present_critical_fields=assessment.present_critical_fields,
                present_core_fields=assessment.present_core_fields,
                missing_critical_fields=assessment.missing_critical_fields,
                missing_core_fields=assessment.missing_core_fields,
                critical_coverage=assessment.critical_coverage,
                critical_threshold=Decimal("1"),
                core_coverage=assessment.core_coverage,
                core_threshold=Decimal("0.9"),
                disposition=disposition,
                typed_reason=reasons,
                pit_eligible=assessment.strict_pit_eligible,
                artifact_identity=candidate.artifact.artifact_identity,
                provider_attempt_count=provider_attempt_counts.get(provider, 0),
                since_listing_exception=since_listing is not None,
                listing_date=(
                    since_listing.listing_date
                    if since_listing is not None
                    else None
                ),
                listing_provider=(
                    listing_provenance.provider_id
                    if listing_provenance is not None
                    else None
                ),
                listing_source_ref=(
                    listing_provenance.source_ref
                    if listing_provenance is not None
                    else None
                ),
                compatibility_state=(
                    metadata_assessment.compatibility_state
                    if metadata_assessment is not None
                    else None
                ),
                indicator_field_semantics_version=(
                    manifest.indicator_field_semantics_version
                    if indicator_metadata
                    else None
                ),
                indicator_metadata_compatibility_version=(
                    manifest.indicator_metadata_compatibility_version
                    if indicator_metadata
                    else None
                ),
                metadata_compatibility_assessment_identity=(
                    metadata_assessment.assessment_identity
                    if metadata_assessment is not None
                    else None
                ),
                metadata_typed_reasons=(
                    tuple(
                        sorted(
                            {
                                item.reason
                                for item in metadata_assessment.dimensions
                                if item.state == "unproven"
                            }
                        )
                    )
                    if metadata_assessment is not None
                    else ()
                ),
                temporal_eligibility_binding=(
                    manifest.temporal_eligibility_binding if temporal else None
                ),
            )
        )
    if manifest.requested_capability is FinancialCapability.STATEMENT:
        missing_periods = (
            *(
                (
                    period,
                    FinancialReportingFrequency.ANNUAL,
                )
                for period in completeness.missing_annual_period_ends
            ),
            *(
                (
                    period,
                    FinancialReportingFrequency.QUARTERLY,
                )
                for period in completeness.missing_reporting_period_ends
            ),
        )
    else:
        missing_periods = tuple(
            (
                period,
                FinancialReportingFrequency.QUARTERLY,
            )
            for period in completeness.missing_reporting_period_ends
        )
    for period, frequency in missing_periods:
        rows.append(
            FinancialCompletenessAuditRow(
                contract_version=(
                    "financial-completeness-row-v6"
                    if windowed_v5
                    else "financial-completeness-row-v5"
                    if temporal_v4
                    else "financial-completeness-row-v4"
                    if indicator_metadata
                    else "financial-completeness-row-v3"
                ),
                manifest_identity=manifest.manifest_identity,
                capability=manifest.requested_capability,
                statement_or_ratio_family=_manifest_family(manifest),
                period=period,
                frequency=frequency,
                provider="none",
                company_type=completeness.company_type,
                company_type_resolution_method=None,
                company_type_provenance=(),
                provider_declared_type=None,
                provider_declaration_qualified=False,
                metadata_coverage=Decimal("0"),
                currency=completeness.currency,
                critical_coverage=Decimal("0"),
                critical_threshold=Decimal("1"),
                core_coverage=Decimal("0"),
                core_threshold=Decimal("0.9"),
                disposition="unresolved" if indicator_metadata else "rejected",
                typed_reason=tuple(
                    reason.value for reason in completeness.rejection_reasons
                ),
                pit_eligible=False,
                artifact_identity=None,
                provider_attempt_count=0,
                since_listing_exception=since_listing is not None,
                listing_date=(
                    since_listing.listing_date
                    if since_listing is not None
                    else None
                ),
                listing_provider=(
                    listing_provenance.provider_id
                    if listing_provenance is not None
                    else None
                ),
                listing_source_ref=(
                    listing_provenance.source_ref
                    if listing_provenance is not None
                    else None
                ),
                compatibility_state="unresolved" if indicator_metadata else None,
                indicator_field_semantics_version=(
                    manifest.indicator_field_semantics_version
                    if indicator_metadata
                    else None
                ),
                indicator_metadata_compatibility_version=(
                    manifest.indicator_metadata_compatibility_version
                    if indicator_metadata
                    else None
                ),
                temporal_eligibility_binding=(
                    manifest.temporal_eligibility_binding if temporal else None
                ),
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                item.capability.value,
                item.statement_or_ratio_family,
                item.period,
                item.provider,
                item.artifact_identity or "",
            ),
        )
    )


def _project_statement_manifest_audit_request(
    entry: FinancialStatementSelectionCheckpointEntry,
) -> tuple[FinancialManifestAuditRequest, tuple[FinancialCompletenessAuditRow, ...]]:
    result = entry.terminal.routing_result
    provider_attempt_counts = {
        call.provider_id: len(call.physical_attempt_events)
        for call in result.provider_calls
    }
    manifest = result.manifest
    rows = _project_manifest_completeness_rows(
        manifest,
        provider_attempt_counts=provider_attempt_counts,
    )
    return (
        FinancialManifestAuditRequest(
            contract_version=(
                "financial-manifest-request-audit-v5"
                if manifest.contract_version == "financial-manifest-v5"
                else "financial-manifest-request-audit-v4"
                if manifest.contract_version == "financial-manifest-v4"
                else "financial-manifest-request-audit-v2"
            ),
            request_ref=entry.canonical_request_key.request_key,
            tool_name=entry.canonical_request_key.tool_name,
            statement_type=entry.canonical_request_key.statement_type,
            frequency=entry.canonical_request_key.frequency,
            as_of_date=entry.canonical_request_key.as_of_date,
            logical_provider_count=len(result.provider_calls),
            logical_candidate_count=len(manifest.provider_candidates),
            physical_request_count=len(manifest.physical_attempt_events),
            duplicate_suppressed_count=entry.reuse_count,
            manifests=(_project_manifest_for_audit(manifest),),
            temporal_eligibility_binding=manifest.temporal_eligibility_binding,
        ),
        rows,
    )


def _project_indicator_manifest_audit_request(
    entry: FinancialIndicatorSelectionCheckpointEntry,
) -> tuple[FinancialManifestAuditRequest, tuple[FinancialCompletenessAuditRow, ...]]:
    result = entry.terminal.routing_result
    provider_attempt_counts = {
        call.provider_id: len(call.physical_attempt_events)
        for call in result.provider_calls
    }
    manifests = result.family_manifests
    rows = tuple(
        row
        for manifest in manifests
        for row in _project_manifest_completeness_rows(
            manifest,
            provider_attempt_counts=provider_attempt_counts,
        )
    )
    candidate_ids = {
        item.candidate.candidate_identity
        for manifest in manifests
        for item in manifest.provider_candidates
    }
    assessment_identities = tuple(
        item.assessment_identity
        for manifest in manifests
        for item in manifest.metadata_compatibility_assessments
    )
    indicator_v3 = all(
        manifest.contract_version == "financial-manifest-v3"
        for manifest in manifests
    )
    temporal_v4 = bool(manifests) and all(
        manifest.contract_version == "financial-manifest-v4"
        for manifest in manifests
    )
    windowed_v5 = bool(manifests) and all(
        manifest.contract_version == "financial-manifest-v5"
        for manifest in manifests
    )
    temporal = temporal_v4 or windowed_v5
    temporal_binding = (
        manifests[0].temporal_eligibility_binding if temporal else None
    )
    return (
        FinancialManifestAuditRequest(
            contract_version=(
                "financial-manifest-request-audit-v5"
                if windowed_v5
                else "financial-manifest-request-audit-v4"
                if temporal_v4
                else "financial-manifest-request-audit-v3"
                if indicator_v3
                else "financial-manifest-request-audit-v2"
            ),
            request_ref=entry.canonical_request_key.request_key,
            tool_name=entry.canonical_request_key.tool_name,
            statement_type=entry.canonical_request_key.statement_type,
            frequency=entry.canonical_request_key.frequency,
            as_of_date=entry.canonical_request_key.as_of_date,
            logical_provider_count=len(result.provider_calls),
            logical_candidate_count=len(candidate_ids),
            physical_request_count=len(result.physical_attempt_events),
            duplicate_suppressed_count=entry.reuse_count,
            manifests=tuple(
                _project_manifest_for_audit(manifest)
                for manifest in manifests
            ),
            family_assessments=result.family_assessments,
            aggregate_family_assessment=result.aggregate_family_assessment,
            indicator_field_semantics_version=(
                result.indicator_field_semantics_version
                if indicator_v3 or temporal
                else None
            ),
            indicator_metadata_compatibility_version=(
                result.indicator_metadata_compatibility_version
                if indicator_v3 or temporal
                else None
            ),
            metadata_assessment_identities=(
                assessment_identities if indicator_v3 or temporal else ()
            ),
            temporal_eligibility_binding=temporal_binding,
        ),
        tuple(
            sorted(
                rows,
                key=lambda item: (
                    item.capability.value,
                    item.statement_or_ratio_family,
                    item.period,
                    item.provider,
                    item.artifact_identity or "",
                ),
            )
        ),
    )


def _provider_attempt_evidence(
    events: tuple[ProviderSubrequestAttemptEvent, ...],
) -> tuple[ProviderPhysicalAttemptEvidence, ...]:
    counts_by_sequence: dict[str, int] = {}
    for event in events:
        counts_by_sequence[event.sequence_id] = (
            counts_by_sequence.get(event.sequence_id, 0) + 1
        )
    return tuple(
        ProviderPhysicalAttemptEvidence(
            attempt_event_id=event.attempt_event_id,
            sequence_id=event.sequence_id,
            request_key=event.request_key,
            upstream_service_id=event.upstream_service_id,
            upstream_service_name=event.upstream_service_id,
            capacity_scope=event.capacity_scope,
            attempt_index=event.attempt_index,
            attempted_at=event.attempted_at.isoformat(),
            pacing_event=event.pacing_event,
            pacing_wait_seconds=event.pacing_wait_seconds,
            outcome=event.outcome.value,
            retryable=event.retryable,
            status_code=event.status_code,
            retry_after_seconds=event.retry_after_seconds,
            cooldown_changed=event.cooldown_changed,
            cooldown_until=(
                event.cooldown_until.isoformat()
                if event.cooldown_until is not None
                else None
            ),
            final_physical_attempt_count=counts_by_sequence[
                event.sequence_id
            ],
        )
        for event in sorted(
            events,
            key=lambda item: (item.sequence_id, item.attempt_index),
        )
    )


def _selected_financial_evidence(
    *,
    dispatch_result: (
        FinancialStatementSelectionDispatchResult
        | FinancialIndicatorSelectionDispatchResult
    ),
    manifests: tuple[FinancialAcquisitionManifest, ...],
) -> tuple[tuple[SourceArtifact, ...], tuple[SourceFact, ...]]:
    artifacts: list[SourceArtifact] = []
    facts: list[SourceFact] = []
    for manifest in manifests:
        selected_by_id = {
            selection.candidate_identity: selection
            for selection in manifest.selections
            if selection.strict_pit_eligible
            or (
                selection.contract_version == "financial-period-selection-v2"
                and manifest.requested_analysis_scope
                is FinancialAvailabilityScope.CURRENT_ONLY
                and selection.availability_scope
                is FinancialAvailabilityScope.CURRENT_ONLY
            )
        }
        conflict_ids = {
            candidate_id
            for conflict in manifest.conflicts
            for candidate_id in (
                conflict.selected_candidate_identity,
                conflict.conflicting_candidate_identity,
            )
        }
        for provider_candidate in manifest.provider_candidates:
            candidate = provider_candidate.candidate
            assessment = provider_candidate.assessment
            if (
                candidate.candidate_identity not in selected_by_id
                or candidate.candidate_identity in conflict_ids
            ):
                continue
            selected_fields = tuple(
                field
                for field in candidate.fields
                if field.normalized_value is not None
                and field.normalized_unit is not None
            )
            if not selected_fields:
                continue
            selection = selected_by_id[candidate.candidate_identity]
            availability_lineage = None
            rendering_scope = None
            if selection.contract_version == "financial-period-selection-v2":
                if (
                    selection.requested_analysis_scope is None
                    or selection.availability_scope is None
                    or selection.availability_basis is None
                    or selection.analysis_as_of_date is None
                    or selection.ann_date is None
                    or selection.local_provider_revision_identity is None
                    or selection.observed_at is None
                    or selection.retrieved_at is None
                    or selection.ratio_family is None
                    or selection.period_end is None
                    or selection.eligibility_policy_version is None
                ):
                    raise ValueError("financial Source Fact availability is incomplete")
                rendering_scope = (
                    "current_analysis"
                    if selection.requested_analysis_scope
                    is FinancialAvailabilityScope.CURRENT_ONLY
                    else "strict_pit"
                )
                availability_lineage = SourceFactAvailabilityLineage(
                    requested_analysis_scope=(
                        selection.requested_analysis_scope.value
                    ),
                    availability_scope=selection.availability_scope.value,
                    availability_basis=selection.availability_basis.value,
                    analysis_as_of_date=selection.analysis_as_of_date,
                    ann_date=selection.ann_date,
                    f_ann_date=selection.f_ann_date,
                    provider_artifact_identity=selection.artifact_identity,
                    local_provider_revision_identity=(
                        selection.local_provider_revision_identity
                    ),
                    observed_at=selection.observed_at,
                    retrieved_at=selection.retrieved_at,
                    family=selection.ratio_family.value,
                    period=selection.period_end,
                    eligibility_policy_version=(
                        selection.eligibility_policy_version
                    ),
                    availability_reason=(
                        selection.availability_reason.value
                        if selection.availability_reason is not None
                        else None
                    ),
                )
            lines = tuple(
                _canonical_json(
                    {
                        "analysis_as_of_date": (
                            selection.analysis_as_of_date.isoformat()
                            if selection.analysis_as_of_date is not None
                            else None
                        ),
                        "ann_date": (
                            selection.ann_date.isoformat()
                            if selection.ann_date is not None
                            else None
                        ),
                        "artifact_identity": (
                            candidate.artifact.artifact_identity
                        ),
                        "availability_basis": (
                            selection.availability_basis.value
                            if selection.availability_basis is not None
                            else None
                        ),
                        "availability_reason": (
                            selection.availability_reason.value
                            if selection.availability_reason is not None
                            else None
                        ),
                        "availability_scope": (
                            selection.availability_scope.value
                            if selection.availability_scope is not None
                            else None
                        ),
                        "candidate_identity": candidate.candidate_identity,
                        "canonical_field": field.normalized_field,
                        "eligibility_policy_version": (
                            selection.eligibility_policy_version
                        ),
                        "f_ann_date": (
                            selection.f_ann_date.isoformat()
                            if selection.f_ann_date is not None
                            else None
                        ),
                        "local_provider_revision_identity": (
                            selection.local_provider_revision_identity
                        ),
                        "normalized_value": field.normalized_value,
                        "observed_at": (
                            selection.observed_at.isoformat()
                            if selection.observed_at is not None
                            else None
                        ),
                        "period": assessment.period_end.isoformat(),
                        "rendering_scope": rendering_scope,
                        "requested_analysis_scope": (
                            selection.requested_analysis_scope.value
                            if selection.requested_analysis_scope is not None
                            else None
                        ),
                        "retrieved_at": (
                            selection.retrieved_at.isoformat()
                            if selection.retrieved_at is not None
                            else None
                        ),
                        "unit": field.normalized_unit,
                    },
                    label="financial selected period evidence",
                )
                for field in selected_fields
            )
            raw_text = "\n".join(lines)
            source_ref = stable_acquisition_source_ref(
                "financial.period",
                manifest.manifest_identity,
                candidate.candidate_identity,
            )
            artifact = SourceArtifact(
                artifact_sha256=sha256(raw_text.encode("utf-8")).hexdigest(),
                source_ref=source_ref,
                tool_call_id=dispatch_result.tool_call_id,
                tool_name=dispatch_result.request_key.tool_name,
                raw_text=raw_text,
            )
            artifacts.append(artifact)
            offset = 0
            for field, line in zip(selected_fields, lines, strict=True):
                effective_date = assessment.period_end.isoformat()
                fact_id = stable_source_fact_id(
                    source_ref=source_ref,
                    artifact_sha256=artifact.artifact_sha256,
                    source_span_start=offset,
                    source_span_end=offset + len(line),
                    canonical_field=field.normalized_field,
                    instrument_symbol=(
                        candidate.period_identity.instrument_identity.symbol
                    ),
                    effective_date=effective_date,
                )
                facts.append(
                    SourceFact(
                        fact_kind="canonical",
                        fact_id=fact_id,
                        source_ref=source_ref,
                        tool_call_id=dispatch_result.tool_call_id,
                        tool_name=dispatch_result.request_key.tool_name,
                        artifact_sha256=artifact.artifact_sha256,
                        raw_text=line,
                        source_span_start=offset,
                        source_span_end=offset + len(line),
                        normalized_numeric_tokens=(
                            field.normalized_value,
                        ),
                        canonical_field=field.normalized_field,
                        normalized_value=field.normalized_value,
                        unit=field.normalized_unit,
                        instrument_symbol=(
                            candidate.period_identity.instrument_identity.symbol
                        ),
                        effective_date=effective_date,
                        availability_lineage=availability_lineage,
                    )
                )
                offset += len(line) + 1
    return tuple(artifacts), tuple(facts)


def project_financial_selection_evidence(
    evidence: EvidenceState | Mapping[str, Any] | None,
    dispatch_result: (
        FinancialStatementSelectionDispatchResult
        | FinancialIndicatorSelectionDispatchResult
    ),
) -> EvidenceState:
    """Project v2 operational attempts and strict selected periods into evidence."""

    current = (
        evidence
        if isinstance(evidence, EvidenceState)
        else EvidenceState.model_validate(evidence or {})
    )
    expected_identity = dispatch_result.selection_request.instrument_identity
    if (
        current.instrument_identity is not None
        and current.instrument_identity != expected_identity
    ):
        raise ValueError("financial selection contradicts Evidence State identity")
    manifests = (
        (dispatch_result.routing_result.manifest,)
        if isinstance(
            dispatch_result,
            FinancialStatementSelectionDispatchResult,
        )
        else dispatch_result.routing_result.family_manifests
    )
    if not manifests or any(
        manifest.contract_version != "financial-manifest-v2"
        for manifest in manifests
    ):
        return current
    bindings = tuple(
        manifest.financial_eligibility_binding for manifest in manifests
    )
    if not bindings or any(binding != bindings[0] for binding in bindings) or (
        current.financial_eligibility_binding is not None
        and bindings[0] != current.financial_eligibility_binding
    ):
        raise ValueError("financial selection eligibility binding is contradictory")
    attempt_events = {
        event.attempt_event_id: event
        for manifest in manifests
        for event in manifest.physical_attempt_events
    }
    projected_attempts = _provider_attempt_evidence(
        tuple(attempt_events.values())
    )
    merged_attempts = {
        (
            event.attempt_event_id
            or f"{event.sequence_id}:{event.attempt_index}"
        ): event
        for event in current.physical_attempt_events
    }
    for event in projected_attempts:
        key = event.attempt_event_id or (
            f"{event.sequence_id}:{event.attempt_index}"
        )
        prior = merged_attempts.get(key)
        if prior is not None and prior != event:
            raise ValueError("financial physical attempt evidence is contradictory")
        merged_attempts[key] = event
    new_artifacts, new_facts = _selected_financial_evidence(
        dispatch_result=dispatch_result,
        manifests=manifests,
    )
    artifact_by_sha = {
        artifact.artifact_sha256: artifact
        for artifact in current.source_artifacts
    }
    for artifact in new_artifacts:
        prior = artifact_by_sha.get(artifact.artifact_sha256)
        if prior is not None and prior.model_dump(
            mode="python",
            exclude={"tool_call_id"},
        ) != artifact.model_dump(
            mode="python",
            exclude={"tool_call_id"},
        ):
            raise ValueError("financial Source Artifact identity is contradictory")
        artifact_by_sha.setdefault(artifact.artifact_sha256, artifact)
    payload = current.model_dump(mode="python")
    payload["financial_eligibility_binding"] = bindings[0]
    payload["instrument_identity"] = expected_identity
    payload["source_artifacts"] = tuple(artifact_by_sha.values())
    current_analysis_facts = tuple(
        fact
        for fact in new_facts
        if fact.availability_lineage is not None
        and fact.availability_lineage.requested_analysis_scope == "current_only"
    )
    generally_admitted_facts = tuple(
        fact for fact in new_facts if fact not in current_analysis_facts
    )
    payload["source_facts"] = (
        *current.source_facts,
        *generally_admitted_facts,
    )
    payload["current_analysis_source_facts"] = (
        *current.current_analysis_source_facts,
        *current_analysis_facts,
    )
    payload["physical_attempt_events"] = tuple(
        sorted(
            merged_attempts.values(),
            key=lambda item: (item.sequence_id, item.attempt_index),
        )
    )
    payload["physical_attempt_count"] = len(
        payload["physical_attempt_events"]
    )
    projected_revision_selections = tuple(
        FinancialRevisionSelectionEvidence.model_validate(
            selection.model_dump(mode="python")
        )
        for manifest in manifests
        for selection in manifest.revision_selections
    )
    revision_selection_by_id = {
        item.selection_identity: item
        for item in current.financial_revision_selections
    }
    for selection in projected_revision_selections:
        prior = revision_selection_by_id.get(selection.selection_identity)
        if prior is not None and prior != selection:
            raise ValueError("financial revision selection evidence is contradictory")
        revision_selection_by_id[selection.selection_identity] = selection
    payload["financial_revision_selections"] = tuple(
        sorted(
            revision_selection_by_id.values(),
            key=lambda item: item.selection_identity,
        )
    )

    route = dispatch_result.routing_result.route
    route_rank_by_provider = {
        ("baostock" if provider_id == "baostock_qualified_families" else provider_id): rank
        for rank, provider_id in enumerate(route)
    }
    conflict_candidate_ids = {
        candidate_id
        for manifest in manifests
        for conflict in manifest.conflicts
        for candidate_id in (
            conflict.selected_candidate_identity,
            conflict.conflicting_candidate_identity,
        )
    }
    revision_binding_by_candidate = {
        disposition.candidate_identity: (selection, disposition)
        for manifest in manifests
        for selection in manifest.revision_selections
        for disposition in selection.candidate_dispositions
    }
    projected_observations: list[FinancialRevisionObservationEvidence] = []
    for manifest in manifests:
        if not manifest.revision_selections:
            continue
        manifest_policy = manifest.revision_selections[0].eligibility_policy_version
        for provider_candidate in manifest.provider_candidates:
            candidate = provider_candidate.candidate
            identity = candidate.period_identity
            provider_id = candidate.artifact.dataset.provider_id
            selection_binding = revision_binding_by_candidate.get(
                candidate.candidate_identity
            )
            if selection_binding is None:
                revision_disposition = "rejected"
                revision_selection_identity = None
                requested_analysis_scope = manifest.requested_analysis_scope
            else:
                revision_selection, candidate_disposition = selection_binding
                revision_disposition = candidate_disposition.disposition
                revision_selection_identity = revision_selection.selection_identity
                requested_analysis_scope = (
                    revision_selection.requested_analysis_scope
                )
            family = identity.statement_type or identity.ratio_family
            if family is None:
                raise ValueError("financial revision observation family is absent")
            projected_observations.append(
                FinancialRevisionObservationEvidence(
                    manifest_identity=manifest.manifest_identity,
                    candidate_identity=candidate.candidate_identity,
                    artifact_identity=candidate.artifact.artifact_identity,
                    local_provider_revision_identity=(
                        candidate.filing_metadata.local_provider_revision_identity
                    ),
                    payload_sha256=candidate.artifact.payload_sha256,
                    provider_id=provider_id,
                    provider_route_rank=route_rank_by_provider[provider_id],
                    family=family.value,
                    period=identity.period_end,
                    frequency=identity.frequency.value,
                    company_type=identity.company_type.value,
                    consolidation_scope=identity.consolidation_scope.value,
                    currency=identity.currency,
                    normalized_units=tuple(
                        sorted(
                            {
                                field.normalized_unit
                                for field in candidate.fields
                                if field.normalized_unit is not None
                            }
                        )
                    ),
                    assessment_disposition=(
                        provider_candidate.assessment.disposition.value
                    ),
                    revision_disposition=revision_disposition,
                    revision_selection_identity=revision_selection_identity,
                    rejection_reasons=tuple(
                        reason.value
                        for reason in provider_candidate.assessment.rejection_reasons
                    ),
                    conflicted=(
                        candidate.candidate_identity in conflict_candidate_ids
                    ),
                    requested_analysis_scope=(
                        requested_analysis_scope.value
                        if requested_analysis_scope is not None
                        else None
                    ),
                    eligibility_policy_version=manifest_policy,
                )
            )
    observation_by_id = {
        item.candidate_identity: item
        for item in current.financial_revision_observations
    }
    for observation in projected_observations:
        prior = observation_by_id.get(observation.candidate_identity)
        if prior is not None and prior != observation:
            raise ValueError("financial revision observation evidence is contradictory")
        observation_by_id[observation.candidate_identity] = observation
    payload["financial_revision_observations"] = tuple(
        sorted(
            observation_by_id.values(),
            key=lambda item: item.candidate_identity,
        )
    )
    if isinstance(dispatch_result, FinancialIndicatorSelectionDispatchResult):
        routed_family_assessments = tuple(
            FinancialFamilyEvidenceAssessment.model_validate(
                item.model_dump(mode="python")
            )
            for item in dispatch_result.routing_result.family_assessments
        )
        family_by_ref = {
            item.assessment_ref: item
            for item in current.financial_family_assessments
        }
        for assessment in routed_family_assessments:
            prior = family_by_ref.get(assessment.assessment_ref)
            if prior is not None and prior != assessment:
                raise ValueError("financial family evidence identity is contradictory")
            family_by_ref[assessment.assessment_ref] = assessment
        family_order = {
            "profit": 0,
            "operation": 1,
            "growth": 2,
            "balance": 3,
            "cash_flow": 4,
            "dupont": 5,
        }
        payload["financial_family_assessments"] = tuple(
            sorted(
                family_by_ref.values(),
                key=lambda item: (
                    item.eligibility_policy_version,
                    item.requested_analysis_scope,
                    item.company_type,
                    family_order[item.family],
                    item.assessment_ref,
                ),
            )
        )
        aggregate = dispatch_result.routing_result.aggregate_family_assessment
        if aggregate is not None:
            projected_aggregate = (
                FinancialFamilyAggregateEvidenceAssessment.model_validate(
                    aggregate.model_dump(mode="python")
                )
            )
            aggregate_by_ref = {
                item.assessment_ref: item
                for item in current.financial_family_aggregate_assessments
            }
            prior = aggregate_by_ref.get(projected_aggregate.assessment_ref)
            if prior is not None and prior != projected_aggregate:
                raise ValueError(
                    "financial family aggregate evidence identity is contradictory"
                )
            aggregate_by_ref[projected_aggregate.assessment_ref] = projected_aggregate
            payload["financial_family_aggregate_assessments"] = tuple(
                sorted(
                    aggregate_by_ref.values(),
                    key=lambda item: item.assessment_ref,
                )
            )
    return EvidenceState.model_validate(payload)


def _validate_financial_dispatch_audit_integrity(
    ledger: FinancialDispatchCheckpointLedger,
    *,
    run_asset_configuration: (
        RunAssetConfiguration | RunAssetConfigurationProjection | None
    ),
    config: Mapping[str, Any] | None,
    capability_routing_plan: MainlandCapabilityRoutingPlan | None,
    run_scope_id: str | None,
) -> None:
    """Fail closed before checkpoint state is authenticated in immutable audit."""

    if run_asset_configuration is None:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
        )
    if (
        ledger.run_asset_configuration_version
        != run_asset_configuration.asset_configuration_version
        or ledger.run_asset_configuration_signature
        != run_asset_configuration.asset_configuration_signature
    ):
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.ASSET_CONFIGURATION_MISMATCH
        )
    if ledger.contract_version in {
        FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION,
        FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
        FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
        FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
        FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
        FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
        FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION,
    }:
        if run_scope_id != ledger.run_scope_id:
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
            )
        if (
            capability_routing_plan is None
            or capability_routing_plan.plan_signature
            != ledger.capability_routing_plan_signature
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ROUTING_PLAN_MISMATCH
            )
        if ledger.contract_version in {
            FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
            FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
            FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION,
        } and (
                capability_routing_plan.financial_eligibility_binding is None
                or ledger.financial_eligibility_binding
                != capability_routing_plan.financial_eligibility_binding
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
            )
        if ledger.contract_version == (
            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION
        ) and (
            capability_routing_plan.plan_version != "1.2"
            or ledger.capability_routing_plan_version != "1.2"
            or ledger.indicator_field_semantics_version
            != capability_routing_plan.indicator_field_semantics_version
            or ledger.indicator_metadata_compatibility_version
            != capability_routing_plan.indicator_metadata_compatibility_version
            or ledger.financial_indicator_manifest_version
            != capability_routing_plan.indicator_manifest_contract_version
            or ledger.indicator_provider_topology
            != capability_routing_plan.route_for(
                MainlandCapability.FINANCIAL_INDICATORS
            )
        ):
            raise FinancialDispatchCheckpointError(
                FinancialDispatchCheckpointFailureReason.INDICATOR_METADATA_COMPATIBILITY_MISMATCH
            )
        if ledger.contract_version == FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION:
            temporal_bindings = capability_routing_plan.temporal_contract_bindings
            context = ledger.financial_eligibility_context
            if (
                capability_routing_plan.plan_version != "1.3"
                or temporal_bindings is None
                or ledger.capability_routing_plan_version != "1.3"
                or ledger.financial_manifest_version != "financial-manifest-v4"
                or ledger.financial_indicator_manifest_version
                != "financial-manifest-v4"
                or context is None
                or context.contract_version
                != "financial-eligibility-acquisition-context-v2"
                or context.temporal_eligibility_binding is None
                or ledger.temporal_eligibility_binding
                != FinancialTemporalEligibilityCheckpointProjection.model_validate(
                    context.temporal_eligibility_binding.model_dump(mode="python")
                )
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
                )
        if ledger.contract_version == FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION:
            temporal_bindings = capability_routing_plan.temporal_contract_bindings
            context = ledger.financial_eligibility_context
            if (
                capability_routing_plan.plan_version != "1.4"
                or temporal_bindings is None
                or ledger.capability_routing_plan_version != "1.4"
                or ledger.financial_manifest_version != "financial-manifest-v5"
                or ledger.financial_indicator_manifest_version
                != "financial-manifest-v5"
                or context is None
                or context.contract_version
                != FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V3
                or context.temporal_eligibility_binding is None
                or context.temporal_eligibility_binding.acquisition_window is None
                or ledger.temporal_eligibility_binding
                != FinancialTemporalEligibilityCheckpointProjection.model_validate(
                    context.temporal_eligibility_binding.model_dump(mode="python")
                )
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
                )
        if ledger.contract_version == FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION:
            context = ledger.financial_eligibility_context
            if (
                capability_routing_plan.plan_version != "1.5"
                or capability_routing_plan.temporal_contract_bindings is None
                or ledger.capability_routing_plan_version != "1.5"
                or context is None
                or context.contract_version
                != FINANCIAL_ELIGIBILITY_ACQUISITION_CONTEXT_VERSION_V4
                or context.temporal_eligibility_binding is None
                or context.temporal_eligibility_binding.contract_version
                != "financial-eligibility-temporal-binding-v3"
                or ledger.temporal_eligibility_binding
                != context.temporal_eligibility_binding
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
                )
        return
    effective_config = config
    if effective_config is None:
        from tradingagents.dataflows.config import get_config

        effective_config = get_config()
    retry_policy = None
    configured_retry_policy = effective_config.get(
        "financial_dispatch_retry_policy"
    )
    if configured_retry_policy is not None:
        retry_policy = RetryPolicy.model_validate(configured_retry_policy)
    dispatch_ledger = ledger.model_copy(
        update={"provider_subrequest_cache": None}
    )
    FinancialToolDispatcher.from_configured_vendors(
        instrument_identity=run_asset_configuration.instrument_identity,
        run_asset_configuration=run_asset_configuration,
        config=effective_config,
        retry_policy=retry_policy,
        checkpoint_ledger=dispatch_ledger,
    )


def project_financial_dispatch_ledger(
    checkpoint_ledger: Mapping[str, Any] | FinancialDispatchCheckpointLedger | None,
    *,
    run_asset_configuration: (
        RunAssetConfiguration | RunAssetConfigurationProjection | None
    ) = None,
    config: Mapping[str, Any] | None = None,
    capability_routing_plan: MainlandCapabilityRoutingPlan | None = None,
    run_scope_id: str | None = None,
) -> FinancialDispatchAuditProjection | None:
    """Project terminal dispatch state without provider payload or correlation text."""

    if checkpoint_ledger is None:
        return None
    try:
        ledger = FinancialDispatchCheckpointLedger.model_validate(checkpoint_ledger)
    except (TypeError, ValueError) as exc:
        raise FinancialDispatchCheckpointError(
            FinancialDispatchCheckpointFailureReason.MALFORMED
        ) from exc
    _validate_financial_dispatch_audit_integrity(
        ledger,
        run_asset_configuration=run_asset_configuration,
        config=config,
        capability_routing_plan=capability_routing_plan,
        run_scope_id=run_scope_id,
    )
    requests: list[FinancialDispatchAuditRequest] = []
    for entry in sorted(
        ledger.entries,
        key=lambda item: item.canonical_request_key.request_key,
    ):
        outcomes: list[FinancialDispatchAuditOutcome] = []
        for plan_outcome in entry.terminal.plan_outcomes:
            outcome = plan_outcome.outcome
            unavailable = (
                outcome if isinstance(outcome, SourceAcquisitionUnavailable) else None
            )
            outcomes.append(
                FinancialDispatchAuditOutcome(
                    provider=outcome.provider,
                    provider_order=outcome.provider_order,
                    outcome=outcome.outcome,
                    attempt=outcome.attempt,
                    retryable=outcome.retryable,
                    reason=(unavailable.reason if unavailable is not None else None),
                    error_code=(
                        unavailable.error_code if unavailable is not None else None
                    ),
                    http_status=(
                        unavailable.http_status if unavailable is not None else None
                    ),
                    retry_after_seconds=(
                        unavailable.retry_after_seconds
                        if unavailable is not None
                        else None
                    ),
                    artifact_sha256=(
                        outcome.artifact.artifact_sha256
                        if isinstance(outcome, SourceAcquisitionAvailable)
                        else None
                    ),
                )
            )
        key = entry.canonical_request_key
        requests.append(
            FinancialDispatchAuditRequest(
                request_ref=key.request_key,
                tool_name=key.tool_name,
                statement_type=key.statement_type,
                frequency=key.frequency,
                as_of_date=key.as_of_date,
                acquisition_attempt_count=entry.consumed_attempt_budget,
                duplicate_suppressed_count=entry.reuse_count,
                artifact_sha256=entry.artifact_sha256,
                outcomes=tuple(outcomes),
            )
        )
    if ledger.contract_version in {
        FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION,
        FINANCIAL_DISPATCH_ELIGIBILITY_LEDGER_VERSION,
        FINANCIAL_DISPATCH_CONTEXT_LEDGER_VERSION,
        FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION,
        FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
        FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
        FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION,
    }:
        manifest_requests: list[FinancialManifestAuditRequest] = []
        completeness_rows: list[FinancialCompletenessAuditRow] = []
        for entry in sorted(
            ledger.qualified_statement_selections,
            key=lambda item: item.canonical_request_key.request_key,
        ):
            request_projection, rows = (
                _project_statement_manifest_audit_request(entry)
            )
            manifest_requests.append(request_projection)
            completeness_rows.extend(rows)
        for entry in sorted(
            ledger.qualified_indicator_selections,
            key=lambda item: item.canonical_request_key.request_key,
        ):
            request_projection, rows = (
                _project_indicator_manifest_audit_request(entry)
            )
            manifest_requests.append(request_projection)
            completeness_rows.extend(rows)
        manifest_requests.sort(key=lambda item: item.request_ref)
        completeness_rows.sort(
            key=lambda item: (
                item.capability.value,
                item.statement_or_ratio_family,
                item.period,
                item.provider,
                item.artifact_identity or "",
            )
        )
        assert ledger.capability_routing_plan_signature is not None
        common_projection: dict[str, Any] = {
            "capability_routing_plan_signature": (
                ledger.capability_routing_plan_signature
            ),
            "financial_eligibility_binding": (
                ledger.financial_eligibility_binding
            ),
            "request_count": len(requests) + len(manifest_requests),
            "logical_provider_count": sum(
                item.logical_provider_count for item in manifest_requests
            ),
            "logical_candidate_count": sum(
                item.logical_candidate_count for item in manifest_requests
            ),
            "acquisition_attempt_count": (
                sum(item.acquisition_attempt_count for item in requests)
                + sum(
                    item.physical_request_count for item in manifest_requests
                )
            ),
            "duplicate_suppressed_count": (
                sum(item.duplicate_suppressed_count for item in requests)
                + sum(
                    item.duplicate_suppressed_count
                    for item in manifest_requests
                )
            ),
            "requests": tuple(requests),
            "manifest_requests": tuple(manifest_requests),
            "completeness_rows": tuple(completeness_rows),
        }
        if ledger.contract_version == FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION:
            temporal_binding = ledger.temporal_eligibility_binding
            if not isinstance(
                temporal_binding,
                FinancialEligibilityTemporalContextBinding,
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
                )
            listing_decision = temporal_binding.listing_temporal_decision
            proof = temporal_binding.composite_listing_temporal_proof
            if (
                not isinstance(
                    listing_decision,
                    CompositeListingTemporalEligibilityDecision,
                )
                or proof is None
            ):
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
                )
            return FinancialDispatchAuditProjectionV6(
                metadata_assessment_identities=tuple(
                    item.assessment_identity
                    for item in ledger.indicator_metadata_assessments
                ),
                metadata_compatibility_states=tuple(
                    item.compatibility_state
                    for item in ledger.indicator_metadata_assessments
                ),
                metadata_incompatibility_reasons=tuple(
                    sorted(
                        {
                            reason.reason
                            for rejection in ledger.indicator_metadata_rejections
                            for reason in rejection.typed_reasons
                        }
                    )
                ),
                temporal_eligibility_binding=temporal_binding,
                listing_temporal_proof_identity=proof.proof_identity,
                effective_artifact_identity=(
                    listing_decision.effective_artifact_identity
                ),
                effective_row_identity=listing_decision.effective_row_identity,
                security_lookup_artifact_identity=(
                    listing_decision.security_lookup_artifact_identity
                ),
                publication_artifact_identity=(
                    listing_decision.publication_artifact_identity
                ),
                publication_announcement_identity=(
                    listing_decision.publication_announcement_identity
                ),
                document_artifact_identity=(
                    listing_decision.document_artifact_identity
                ),
                document_request_identity=(
                    listing_decision.document_request_identity
                ),
                **common_projection,
            )
        if ledger.contract_version in {
            FINANCIAL_DISPATCH_TEMPORAL_LEDGER_VERSION,
            FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION,
        }:
            temporal_binding = ledger.temporal_eligibility_binding
            if temporal_binding is None:
                raise FinancialDispatchCheckpointError(
                    FinancialDispatchCheckpointFailureReason.TEMPORAL_COMPATIBILITY_MISMATCH
                )
            projection_type = (
                FinancialDispatchAuditProjectionV5
                if ledger.contract_version == FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION
                else FinancialDispatchAuditProjectionV4
            )
            return projection_type(
                metadata_assessment_identities=tuple(
                    item.assessment_identity
                    for item in ledger.indicator_metadata_assessments
                ),
                metadata_compatibility_states=tuple(
                    item.compatibility_state
                    for item in ledger.indicator_metadata_assessments
                ),
                metadata_incompatibility_reasons=tuple(
                    sorted(
                        {
                            reason.reason
                            for rejection in ledger.indicator_metadata_rejections
                            for reason in rejection.typed_reasons
                        }
                    )
                ),
                temporal_eligibility_binding=temporal_binding,
                **common_projection,
            )
        if ledger.contract_version == (
            FINANCIAL_DISPATCH_INDICATOR_COMPATIBILITY_LEDGER_VERSION
        ) and any(
            request.contract_version == "financial-manifest-request-audit-v3"
            for request in manifest_requests
        ):
            return FinancialDispatchAuditProjectionV3(
                metadata_assessment_identities=tuple(
                    item.assessment_identity
                    for item in ledger.indicator_metadata_assessments
                ),
                metadata_compatibility_states=tuple(
                    item.compatibility_state
                    for item in ledger.indicator_metadata_assessments
                ),
                metadata_incompatibility_reasons=tuple(
                    sorted(
                        {
                            reason.reason
                            for rejection in ledger.indicator_metadata_rejections
                            for reason in rejection.typed_reasons
                        }
                    )
                ),
                **common_projection,
            )
        return FinancialDispatchAuditProjectionV2(
            **common_projection,
        )
    return FinancialDispatchAuditProjectionV1(
        request_count=len(requests),
        acquisition_attempt_count=sum(
            item.acquisition_attempt_count for item in requests
        ),
        duplicate_suppressed_count=sum(
            item.duplicate_suppressed_count for item in requests
        ),
        requests=tuple(requests),
    )


def _market_from_authoritative_identity(
    instrument_identity: InstrumentIdentityEvidence,
) -> str | None:
    if (
        instrument_identity.venue.strip().upper() in {"XSHG", "XSHE"}
        and instrument_identity.instrument_kind is InstrumentKind.EQUITY
        and instrument_identity.currency.strip().upper() == "CNY"
    ):
        return "cn_a"
    return None


def _configured_vendor_value(
    config: Mapping[str, Any],
    *,
    tool_name: str,
    market: str | None,
) -> str:
    tool_vendors = config.get("tool_vendors", {})
    if isinstance(tool_vendors, Mapping) and tool_name in tool_vendors:
        return str(tool_vendors[tool_name])
    market_vendors = config.get("market_data_vendors", {})
    if market is not None and isinstance(market_vendors, Mapping):
        selected_market = market_vendors.get(market, {})
        if (
            isinstance(selected_market, Mapping)
            and "fundamental_data" in selected_market
        ):
            return str(selected_market["fundamental_data"])
    data_vendors = config.get("data_vendors", {})
    if isinstance(data_vendors, Mapping):
        return str(data_vendors.get("fundamental_data", "default"))
    return "default"


def _financial_provider_from_implementation(
    provider_name: str,
    *,
    tool_name: str,
    implementation: object,
    canonical_symbol: str,
) -> FinancialProvider:
    implementations = implementation if isinstance(implementation, list) else [implementation]
    if not implementations or any(not callable(item) for item in implementations):
        raise TypeError(
            f"financial provider {provider_name!r} has an invalid implementation"
        )
    multiple_variants = isinstance(implementation, list)
    return FinancialProvider(
        name=provider_name,
        variants=tuple(
            FinancialProviderVariant(
                variant_id=f"variant-{index}" if multiple_variants else "default",
                invoke=_configured_financial_invoker(
                    item,
                    tool_name=tool_name,
                    canonical_symbol=canonical_symbol,
                ),
            )
            for index, item in enumerate(implementations, 1)
        ),
    )


@dataclass(frozen=True)
class _ConfiguredFinancialInvoker:
    implementation: Callable[..., object]
    tool_name: str
    canonical_symbol: str

    def __call__(self, request: FinancialToolRequest) -> object:
        return self._invoke(request, canonical_request_key=None)

    def invoke_with_request_key(
        self,
        request: FinancialToolRequest,
        canonical_request_key: str,
    ) -> object:
        return self._invoke(request, canonical_request_key=canonical_request_key)

    def _invoke(
        self,
        request: FinancialToolRequest,
        *,
        canonical_request_key: str | None,
    ) -> object:
        material_arguments = dict(request.material_arguments)
        current_date = request.as_of_date.isoformat()
        internal_arguments: dict[str, object] = {}
        implementation_module = getattr(self.implementation, "__module__", "")
        implementation_name = getattr(self.implementation, "__name__", "")
        if (
            implementation_module == "tradingagents.dataflows.y_finance"
            and implementation_name == self.tool_name
            and canonical_request_key is not None
        ):
            internal_arguments = {
                "_acquired": True,
                "_request_key": canonical_request_key,
            }
        elif (
            implementation_module == "tradingagents.dataflows.akshare_data"
            and implementation_name == "get_fundamentals"
            and canonical_request_key is not None
        ):
            return _invoke_dispatcher_owned_akshare_fundamentals(
                self.implementation,
                canonical_symbol=self.canonical_symbol,
                current_date=current_date,
                material_arguments=material_arguments,
                canonical_request_key=canonical_request_key,
            )
        elif (
            implementation_module
            in {
                "tradingagents.dataflows.akshare_data",
                "tradingagents.dataflows.baostock_data",
            }
            and implementation_name == "get_fundamentals"
        ):
            internal_arguments = {"_acquired": True}
        if self.tool_name == "get_fundamentals":
            return self.implementation(
                self.canonical_symbol,
                current_date,
                **material_arguments,
                **internal_arguments,
            )
        return self.implementation(
            self.canonical_symbol,
            request.frequency.value,
            current_date,
            **material_arguments,
            **internal_arguments,
        )


def _invoke_dispatcher_owned_akshare_fundamentals(
    implementation: Callable[..., object],
    *,
    canonical_symbol: str,
    current_date: str,
    material_arguments: Mapping[str, object],
    canonical_request_key: str,
) -> object:
    from tradingagents.dataflows.config import get_config
    from tradingagents.dataflows.market_snapshot import (
        _coordinator_now,
        _coordinator_sleep,
        _typed_mainland_physical_request,
        record_active_physical_attempt_events,
    )
    from tradingagents.market_history import (
        MarketHistoryConfig,
        MarketHistoryStore,
        PhysicalAttemptBudgetExhausted,
        ProviderRequestCoordinator,
        RequestPriority,
        upstream_service_identity_for_provider,
    )
    from tradingagents.market_history.config import DataUsageMode

    try:
        history_config = MarketHistoryConfig.from_mapping(get_config())
    except Exception as exc:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.NOT_CONFIGURED
        ) from exc
    if history_config.data_usage_mode is DataUsageMode.PRODUCTION:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.USAGE_NOT_ENTITLED
        )
    try:
        store = MarketHistoryStore.open_provider_request_authority(history_config)
    except Exception as exc:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.UPSTREAM_BUSY
        ) from exc

    with store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_service_id, service_name = upstream_service_identity_for_provider(
            "akshare"
        )
        coordinator.register_upstream_service(upstream_service_id, service_name)
        physical_request_index = 0
        owner_id = f"financial-akshare:{threading.get_ident()}:{uuid4().hex}"

        def execute_physical_request(
            operation: str,
            physical_request: Callable[[], object],
        ) -> object:
            nonlocal physical_request_index
            physical_request_index += 1
            subrequest_key = stable_acquisition_source_ref(
                "financial-provider-physical-request",
                canonical_request_key,
                str(physical_request_index),
                operation,
            )

            def typed_physical_request() -> object:
                value = _typed_mainland_physical_request(physical_request)
                if value is None or getattr(value, "empty", False) is True:
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
                        retryable=False,
                    )
                return value

            try:
                result = coordinator.execute_direct_physical_request(
                    request_key=subrequest_key,
                    upstream_service_id=upstream_service_id,
                    owner_id=f"{owner_id}:{physical_request_index}",
                    priority=RequestPriority.INTERACTIVE_MAINLAND,
                    now=_coordinator_now,
                    sleep=_coordinator_sleep,
                    lease_duration=timedelta(minutes=2),
                    operation=f"financial-fundamentals:{operation}",
                    physical_request=typed_physical_request,
                    cooldown_scope="market-snapshot",
                )
            except PhysicalAttemptBudgetExhausted as exc:
                record_active_physical_attempt_events(exc.attempt_events)
                raise exc.failure from exc
            record_active_physical_attempt_events(result.attempt_events)
            return result.value

        value = implementation(
            canonical_symbol,
            current_date,
            **dict(material_arguments),
            _acquired=True,
            _physical_request=execute_physical_request,
        )
        if physical_request_index == 0:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.PROVIDER_ERROR
            )
        return value


def _configured_financial_invoker(
    implementation: Callable[..., object],
    *,
    tool_name: str,
    canonical_symbol: str,
) -> Callable[[FinancialToolRequest], object]:
    return _ConfiguredFinancialInvoker(
        implementation=implementation,
        tool_name=tool_name,
        canonical_symbol=canonical_symbol,
    )


def _canonical_json(value: object, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON data") from exc


def _validate_financial_payload(
    value: object,
    *,
    request: FinancialToolRequest,
    canonical_symbol: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        )
    normalized = value.lstrip().casefold()
    if normalized.startswith("no_data_available:"):
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.NO_DATA)
    if normalized.startswith(("error retrieving ", "data_unavailable:")):
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.PROVIDER_ERROR)
    stripped = value.strip()
    if stripped.startswith("{"):
        _validate_financial_json(
            stripped,
            request=request,
            canonical_symbol=canonical_symbol,
        )
    else:
        _validate_financial_report(
            stripped,
            request=request,
            canonical_symbol=canonical_symbol,
        )
    return value


def _validate_financial_json(
    value: str,
    *,
    request: FinancialToolRequest,
    canonical_symbol: str,
) -> None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        ) from exc
    if not isinstance(payload, dict):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        )
    if any(key in payload for key in ("Error Message", "Information", "Note")):
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.PROVIDER_ERROR)
    if request.statement_type is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS:
        symbol = payload.get("Symbol")
        meaningful_values = tuple(
            item
            for key, item in payload.items()
            if key != "Symbol" and item not in (None, "", [], {})
        )
        if (
            not isinstance(symbol, str)
            or symbol.strip().upper() != canonical_symbol
            or not meaningful_values
        ):
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            )
        return

    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or symbol.strip().upper() != canonical_symbol:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        )
    report_key = (
        "annualReports"
        if request.frequency is FinancialReportingFrequency.ANNUAL
        else "quarterlyReports"
    )
    reports = payload.get(report_key)
    if reports == []:
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.NO_DATA)
    if not isinstance(reports, list) or not reports:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        )
    for report in reports:
        if not isinstance(report, Mapping):
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            )
        raw_date = report.get("fiscalDateEnding")
        try:
            report_date = date.fromisoformat(str(raw_date))
        except ValueError as exc:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            ) from exc
        if report_date > request.as_of_date:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            )
        if not any(
            key != "fiscalDateEnding" and item not in (None, "", [], {})
            for key, item in report.items()
        ):
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            )


def _validate_financial_report(
    value: str,
    *,
    request: FinancialToolRequest,
    canonical_symbol: str,
) -> None:
    lines = value.splitlines()
    if request.statement_type is FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS:
        expected_header = f"# Company Fundamentals for {canonical_symbol}"
        if not lines or lines[0].strip() != expected_header:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            )
        has_provenance_header = any(
            line.startswith(("# Data retrieved on:", "# Primary source:"))
            for line in lines[1:]
        )
        body = tuple(
            line.strip()
            for line in lines[1:]
            if line.strip() and not line.lstrip().startswith("#")
        )
        if not has_provenance_header or not body:
            raise AcquisitionFailure(
                reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
            )
        return

    headings = {
        FinancialStatementType.BALANCE_SHEET: "Balance Sheet",
        FinancialStatementType.CASH_FLOW: "Cash Flow",
        FinancialStatementType.INCOME_STATEMENT: "Income Statement",
    }
    expected_header = (
        f"# {headings[request.statement_type]} data for {canonical_symbol} "
        f"({request.frequency.value})"
    )
    if not lines or lines[0].strip() != expected_header:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        )
    if not any(line.startswith("# Data retrieved on:") for line in lines[1:]):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        )
    data_lines = tuple(
        line.strip()
        for line in lines[1:]
        if line.strip() and not line.lstrip().startswith("#")
    )
    if len(data_lines) < 2 or any("," not in line for line in data_lines[:2]):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        )


def _invoke_financial_variant(
    variant: FinancialProviderVariant,
    request: FinancialToolRequest,
    *,
    canonical_request_key: str,
    provider_subrequest_cache: ProviderSubrequestCache | None,
) -> object:
    """Translate raw adapter failures before deterministic policy observes them."""

    try:
        cached_invoke = getattr(
            variant.invoke,
            "invoke_with_provider_subrequest_cache",
            None,
        )
        if callable(cached_invoke) and provider_subrequest_cache is not None:
            return cached_invoke(
                request,
                canonical_request_key,
                provider_subrequest_cache,
            )
        keyed_invoke = getattr(variant.invoke, "invoke_with_request_key", None)
        if callable(keyed_invoke):
            return keyed_invoke(request, canonical_request_key)
        return variant.invoke(request)
    except AcquisitionFailure:
        raise
    except PhysicalAttemptFailure as exc:
        reason = {
            PhysicalAttemptOutcome.PERMISSION_DENIED: (
                AcquisitionUnavailableReason.PERMISSION_DENIED
            ),
            PhysicalAttemptOutcome.RATE_LIMITED: (
                AcquisitionUnavailableReason.RATE_LIMITED
            ),
            PhysicalAttemptOutcome.TIMEOUT: AcquisitionUnavailableReason.TIMEOUT,
            PhysicalAttemptOutcome.DISCONNECT: (
                AcquisitionUnavailableReason.DISCONNECT
            ),
            PhysicalAttemptOutcome.EMPTY_FRAME: AcquisitionUnavailableReason.NO_DATA,
            PhysicalAttemptOutcome.AUTHENTICATION: (
                AcquisitionUnavailableReason.AUTHENTICATION
            ),
            PhysicalAttemptOutcome.MALFORMED_RESPONSE: (
                AcquisitionUnavailableReason.MALFORMED_RESPONSE
            ),
            PhysicalAttemptOutcome.UPSTREAM_BUSY: (
                AcquisitionUnavailableReason.UPSTREAM_BUSY
            ),
        }.get(exc.outcome, AcquisitionUnavailableReason.PROVIDER_ERROR)
        raise AcquisitionFailure(
            reason=reason,
            status_code=_valid_http_status(exc.status_code),
            error_code=_valid_error_code(exc.error_code),
            retry_after_seconds=_valid_retry_after(exc.retry_after_seconds),
            retryable=exc.retryable,
        ) from None
    except VendorRateLimitError as exc:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.RATE_LIMITED,
            status_code=_valid_http_status(exc.status_code),
            error_code=_valid_error_code(exc.error_code),
            retry_after_seconds=_valid_retry_after(exc.retry_after_seconds),
        ) from None
    except VendorNotConfiguredError:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.NOT_CONFIGURED
        ) from None
    except NoMarketDataError:
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.NO_DATA) from None
    except (RequestsTimeout, TimeoutError):
        raise AcquisitionFailure(reason=AcquisitionUnavailableReason.TIMEOUT) from None
    except PermissionError:
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.AUTHENTICATION
        ) from None
    except (RequestsConnectionError, ConnectionError):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.DISCONNECT
        ) from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise AcquisitionFailure(
            reason=AcquisitionUnavailableReason.MALFORMED_RESPONSE
        ) from None


def _valid_http_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def _valid_error_code(value: object) -> str | None:
    if not isinstance(value, str) or _ACQUISITION_TOKEN.fullmatch(value) is None:
        return None
    return value


def _valid_retry_after(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= 0 else None


__all__ = [
    "CanonicalFinancialRequestKey",
    "DEFAULT_FINANCIAL_ACQUISITION_POLICY_VERSION",
    "FINANCIAL_DISPATCH_AUDIT_VERSION",
    "FINANCIAL_DISPATCH_COMPOSITE_AUDIT_VERSION",
    "FINANCIAL_DISPATCH_COMPOSITE_LEDGER_VERSION",
    "FINANCIAL_DISPATCH_LEDGER_VERSION",
    "FINANCIAL_DISPATCH_MANIFEST_AUDIT_VERSION",
    "FINANCIAL_DISPATCH_MANIFEST_LEDGER_VERSION",
    "FINANCIAL_DISPATCH_WINDOWED_AUDIT_VERSION",
    "FINANCIAL_DISPATCH_WINDOWED_LEDGER_VERSION",
    "FinancialCompletenessAuditRow",
    "FinancialCircuitCheckpointState",
    "FinancialDispatchAuditOutcome",
    "FinancialDispatchAuditProjection",
    "FinancialDispatchAuditProjectionV1",
    "FinancialDispatchAuditProjectionV2",
    "FinancialDispatchAuditProjectionV5",
    "FinancialDispatchAuditProjectionV6",
    "FinancialDispatchAuditRequest",
    "FinancialDispatchCheckpointEntry",
    "FinancialDispatchCheckpointError",
    "FinancialDispatchCheckpointFailureReason",
    "FinancialDispatchCheckpointLedger",
    "FinancialDispatchResult",
    "FinancialPlanOutcome",
    "FinancialManifestAuditProjection",
    "FinancialManifestAuditRequest",
    "FinancialProvider",
    "FinancialProviderChainCheckpointBinding",
    "FinancialProviderVariant",
    "FinancialProviderVariantProgress",
    "FinancialReportingFrequency",
    "FinancialStatementType",
    "FinancialTerminalUnavailableEnvelope",
    "FinancialToolDispatcher",
    "FinancialToolMessageAuditEnvelope",
    "FinancialToolMessageManifestEnvelope",
    "FinancialToolManifestOperationalProjection",
    "FinancialToolRequest",
    "project_financial_dispatch_ledger",
    "project_financial_selection_evidence",
]

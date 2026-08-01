"""Qualified Tushare single-stock financial-indicator adapter.

This adapter is deliberately not registered in production routing. Ticket 10
owns route activation and normalized period selection.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.capability_routing import (
    MainlandCapability,
    MainlandCapabilityRoutingMode,
    MainlandCapabilityRoutingPlan,
    TushareCapability,
)
from tradingagents.dataflows._tushare_indicator_normalization import (
    TUSHARE_FINANCIAL_INDICATOR_ADAPTER_VERSION,
    TushareIndicatorRowRejection,
    project_indicator_candidates,
    qualified_indicator_families,
)
from tradingagents.dataflows._tushare_statement_artifacts import (
    decode_provider_artifact,
    encode_provider_artifact,
    financial_artifact_identity,
    financial_row_artifact_identities,
    require_utc,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCapability,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialConsolidationScope,
    FinancialPeriodCandidate,
    FinancialPeriodCompletenessAssessment,
    FinancialProviderArtifactIdentity,
    FinancialRatioHistoryCompletenessAssessment,
    assess_financial_ratio_history_coverage,
)
from tradingagents.dataflows.provider_subrequests import (
    ProviderSubrequestArtifactRef,
    ProviderSubrequestAttemptEvent,
    ProviderSubrequestCache,
    ProviderSubrequestKey,
    ProviderSubrequestOutcome,
    ProviderSubrequestOutcomeKind,
)
from tradingagents.evidence import InstrumentIdentityEvidence, InstrumentKind
from tradingagents.market_history.config import DataUsageMode
from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    RateLimitScope,
    RequestPriority,
    upstream_service_identity_for_provider,
)

_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_ENDPOINT_ID = "fina_indicator"
_ARTIFACT_METADATA = {
    "capability": "financial_ratio_family",
    "qualified_metadata_absences": [
        "comp_type",
        "f_ann_date",
        "report_scope",
        "stable_restatement_identity",
    ],
}


class TushareFinancialIndicatorSdkClient(Protocol):
    def fina_indicator(self, **kwargs: object) -> pd.DataFrame: ...


class TushareFinancialIndicatorAdapterFailureReason(str, Enum):
    NOT_ENABLED = "tushare_financial_indicators_not_enabled"
    INCOHERENT_PLAN = "tushare_financial_indicators_plan_incoherent"


class TushareFinancialIndicatorAdapterConfigurationError(ValueError):
    """Typed secret-free construction failure raised before provider work."""

    def __init__(
        self,
        reason: TushareFinancialIndicatorAdapterFailureReason,
    ) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class TushareFinancialIndicatorMetadataAvailability(BaseModel):
    """Qualified metadata properties of the endpoint, not response guesses."""

    model_config = _CLOSED_MODEL_CONFIG

    ann_date: Literal[True] = True
    reporting_period: Literal[True] = True
    f_ann_date: Literal[False] = False
    report_scope: Literal[False] = False
    comp_type: Literal[False] = False
    stable_restatement_identity: Literal[False] = False


class TushareFinancialIndicatorAdapterResult(BaseModel):
    """Payload-free Ticket 02 projections and immutable artifact references."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["tushare-financial-indicator-adapter-v1"] = (
        TUSHARE_FINANCIAL_INDICATOR_ADAPTER_VERSION
    )
    endpoint_id: Literal["fina_indicator"] = _ENDPOINT_ID
    subrequest_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")
    provider_artifact: ProviderSubrequestArtifactRef | None = None
    artifact: FinancialProviderArtifactIdentity | None = None
    row_artifacts: tuple[FinancialProviderArtifactIdentity, ...] = ()
    outcome: ProviderSubrequestOutcome
    sequence_id: str
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]
    candidates: tuple[FinancialPeriodCandidate, ...] = ()
    period_completeness: tuple[FinancialPeriodCompletenessAssessment, ...] = ()
    family_completeness: tuple[FinancialRatioHistoryCompletenessAssessment, ...] = ()
    row_rejections: tuple[TushareIndicatorRowRejection, ...] = ()
    metadata_availability: TushareFinancialIndicatorMetadataAvailability = (
        TushareFinancialIndicatorMetadataAvailability()
    )
    current_analysis_capable: bool = False
    strict_no_lookahead_eligible: Literal[False] = False
    strict_replay_eligible: Literal[False] = False
    restatement_lineage_established: Literal[False] = False

    @model_validator(mode="after")
    def _validate_result(self) -> TushareFinancialIndicatorAdapterResult:
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.provider_artifact is not None and self.artifact is not None):
            raise ValueError("Tushare indicator artifact contradicts acquisition outcome")
        if not available and (
            self.row_artifacts
            or self.candidates
            or self.period_completeness
            or self.family_completeness
            or self.row_rejections
            or self.current_analysis_capable
        ):
            raise ValueError("unavailable Tushare indicator cannot emit projections")
        if available and not self.row_artifacts:
            raise ValueError("available Tushare indicator requires row artifacts")
        row_artifact_by_id = {
            item.artifact_identity: item for item in self.row_artifacts
        }
        if len(row_artifact_by_id) != len(self.row_artifacts):
            raise ValueError("Tushare indicator row artifacts must be distinct")
        if self.artifact is not None and any(
            item.dataset != self.artifact.dataset
            or item.canonical_request_sha256
            != self.artifact.canonical_request_sha256
            or item.retrieved_at != self.artifact.retrieved_at
            or item.observed_at != self.artifact.observed_at
            for item in self.row_artifacts
        ):
            raise ValueError("Tushare indicator row artifact contradicts bulk artifact")
        used_row_artifact_ids: set[str] = set()
        for candidate in self.candidates:
            used_row_artifact_ids.add(candidate.artifact.artifact_identity)
            if (
                candidate.artifact.artifact_identity not in row_artifact_by_id
                or candidate.period_identity.capability
                is not FinancialCapability.RATIO_FAMILY
                or candidate.period_identity.statement_type is not None
                or candidate.period_identity.ratio_family is None
            ):
                raise ValueError(
                    "Tushare indicator candidate contradicts its artifact"
                )
        if tuple(item.candidate_identity for item in self.period_completeness) != tuple(
            item.candidate_identity for item in self.candidates
        ):
            raise ValueError("Tushare indicator completeness is not candidate-aligned")
        for rejection in self.row_rejections:
            used_row_artifact_ids.add(rejection.artifact_identity)
            if rejection.artifact_identity not in row_artifact_by_id:
                raise ValueError(
                    "Tushare indicator rejection contradicts its artifact"
                )
        if used_row_artifact_ids != set(row_artifact_by_id):
            raise ValueError("Tushare indicator row artifact has no disposition")
        expected_current = any(item.complete for item in self.family_completeness)
        if self.current_analysis_capable != expected_current:
            raise ValueError("Tushare indicator current capability is inconsistent")
        return self


class TushareFinancialIndicatorAdapter:
    """Acquire and normalize one qualified Tushare indicator history."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        subrequest_cache: ProviderSubrequestCache,
        sdk_client: TushareFinancialIndicatorSdkClient,
        owner_id: str,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        lease_duration: timedelta,
    ) -> None:
        _validate_enabled_plan(routing_plan)
        self._routing_plan = routing_plan
        self._cache = subrequest_cache
        self._sdk_client = sdk_client
        self._owner_id = owner_id
        self._now = now
        self._sleep = sleep
        self._lease_duration = lease_duration

    def acquire_financial_indicators(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        company_type_resolution: FinancialCompanyTypeResolution,
        range_start: date,
        as_of_date: date,
        eligible_reporting_period_ends: Sequence[date],
    ) -> TushareFinancialIndicatorAdapterResult:
        _validate_instrument_identity(instrument_identity)
        if range_start > as_of_date:
            raise ValueError("Tushare indicator range must not end before it starts")
        upstream_service_id, _ = upstream_service_identity_for_provider(
            "tushare",
            account_scope=self._routing_plan.account_scope_label,
        )
        key = ProviderSubrequestKey.create(
            provider_id="tushare",
            upstream_service_id=upstream_service_id,
            account_scope=self._routing_plan.account_scope_label,
            capacity_scope=_ENDPOINT_ID,
            instrument_identity=instrument_identity,
            requested_range_start=range_start.isoformat(),
            requested_range_end=as_of_date.isoformat(),
            requested_fields=("all_fields",),
            as_of_date=as_of_date,
            qualification_profile=self._routing_plan.qualification_profile,
            normalizer_version=self._routing_plan.normalizer_version,
            data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
        )

        def physical_request() -> bytes:
            try:
                frame = self._sdk_client.fina_indicator(
                    ts_code=_tushare_symbol(instrument_identity),
                    start_date=range_start.strftime("%Y%m%d"),
                    end_date=as_of_date.strftime("%Y%m%d"),
                )
            except Exception as exc:
                raise _sanitized_sdk_failure(exc) from None
            if not isinstance(frame, pd.DataFrame):
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                    retryable=False,
                    error_code="tushare_response_not_frame",
                )
            if frame.empty:
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
                    retryable=False,
                    error_code="tushare_empty_frame",
                )
            observed_at = require_utc(self._now())
            try:
                return encode_provider_artifact(
                    endpoint_id=_ENDPOINT_ID,
                    frame=frame,
                    retrieved_at=observed_at,
                    observed_at=observed_at,
                    artifact_contract_version=(
                        TUSHARE_FINANCIAL_INDICATOR_ADAPTER_VERSION
                    ),
                    adapter_metadata=_ARTIFACT_METADATA,
                )
            except (TypeError, ValueError, OverflowError):
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                    retryable=False,
                    error_code="tushare_response_not_serializable",
                ) from None

        subrequest = self._cache.execute(
            key,
            owner_id=self._owner_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=self._now,
            sleep=self._sleep,
            lease_duration=self._lease_duration,
            operation="tushare_financial_indicator",
            media_type="application/json",
            physical_request=physical_request,
        )
        if subrequest.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return TushareFinancialIndicatorAdapterResult(
                subrequest_key=key.subrequest_key,
                outcome=subrequest.outcome,
                sequence_id=subrequest.sequence_id,
                attempt_events=subrequest.attempt_events,
            )
        assert subrequest.value is not None
        assert subrequest.artifact is not None
        payload = decode_provider_artifact(
            subrequest.value,
            endpoint_id=_ENDPOINT_ID,
            artifact_contract_version=TUSHARE_FINANCIAL_INDICATOR_ADAPTER_VERSION,
            adapter_metadata=_ARTIFACT_METADATA,
        )
        artifact = financial_artifact_identity(key=key, payload=payload)
        row_artifacts = financial_row_artifact_identities(
            key=key,
            response_artifact=artifact,
            payload=payload,
        )
        candidates, period_completeness, row_rejections = (
            project_indicator_candidates(
                payload=payload,
                row_artifacts=row_artifacts,
                instrument_identity=instrument_identity,
                company_type_resolution=company_type_resolution,
                expected_provider_symbol=_tushare_symbol(instrument_identity),
                pit_as_of_date=as_of_date,
            )
        )
        families = qualified_indicator_families(
            company_type_resolution.company_type
        )
        family_completeness = tuple(
            assess_financial_ratio_history_coverage(
                instrument_identity=instrument_identity,
                ratio_family=family,
                company_type=company_type_resolution.company_type,
                consolidation_scope=(FinancialConsolidationScope.UNKNOWN),
                currency="XXX",
                assessments=period_completeness,
                eligible_reporting_period_ends=eligible_reporting_period_ends,
            )
            for family in families
            if company_type_resolution.company_type is not FinancialCompanyType.UNKNOWN
        )
        return TushareFinancialIndicatorAdapterResult(
            subrequest_key=key.subrequest_key,
            provider_artifact=subrequest.artifact,
            artifact=artifact,
            row_artifacts=row_artifacts,
            outcome=subrequest.outcome,
            sequence_id=subrequest.sequence_id,
            attempt_events=subrequest.attempt_events,
            candidates=candidates,
            period_completeness=period_completeness,
            family_completeness=family_completeness,
            row_rejections=row_rejections,
            current_analysis_capable=any(item.complete for item in family_completeness),
        )


def _validate_enabled_plan(plan: MainlandCapabilityRoutingPlan) -> None:
    if (
        plan.mode
        not in {
            MainlandCapabilityRoutingMode.QUALIFIED_V1,
            MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW,
        }
        or TushareCapability.FINANCIAL_INDICATORS
        not in plan.enabled_tushare_capabilities
    ):
        raise TushareFinancialIndicatorAdapterConfigurationError(
            TushareFinancialIndicatorAdapterFailureReason.NOT_ENABLED
        )
    endpoint_ids = {item.endpoint_id for item in plan.endpoint_pacing_identities}
    coherent = (
        plan.route_for(MainlandCapability.FINANCIAL_INDICATORS)[0] == "tushare"
        and _ENDPOINT_ID in endpoint_ids
    )
    if not coherent:
        raise TushareFinancialIndicatorAdapterConfigurationError(
            TushareFinancialIndicatorAdapterFailureReason.INCOHERENT_PLAN
        )


def _validate_instrument_identity(identity: InstrumentIdentityEvidence) -> None:
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.currency != "CNY"
        or identity.venue not in {"XSHG", "XSHE"}
    ):
        raise ValueError("Tushare indicators require a mainland Equity identity")


def _tushare_symbol(identity: InstrumentIdentityEvidence) -> str:
    symbol = identity.symbol.strip().upper()
    if symbol.endswith(".SS"):
        return symbol[:-3] + ".SH"
    if symbol.endswith(".SZ"):
        return symbol
    raise ValueError("Tushare indicator symbol is not a canonical mainland symbol")


def _sanitized_sdk_failure(exc: Exception) -> PhysicalAttemptFailure:
    type_name = type(exc).__name__.casefold()
    message = str(exc)[:512].casefold()
    retry_after = _retry_after_seconds(exc)
    if isinstance(exc, TimeoutError) or "timeout" in type_name:
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.TIMEOUT,
            retryable=True,
            error_code="tushare_timeout",
        )
    if any(marker in message for marker in ("频率", "每分钟", "rate limit", "too many")):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            retry_after_seconds=retry_after,
            error_code="tushare_rate_limited",
            rate_limit_scope=RateLimitScope.CAPACITY,
        )
    if any(
        marker in message
        for marker in ("没有接口", "权限", "permission", "access denied")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            error_code="tushare_permission_denied",
        )
    if any(marker in message for marker in ("认证", "auth", "无效token", "invalid token")):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code="tushare_authentication",
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=False,
        error_code="tushare_provider_error",
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    value = getattr(exc, "retry_after_seconds", None)
    if value is None:
        value = getattr(exc, "retry_after", None)
    try:
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


__all__ = [
    "TUSHARE_FINANCIAL_INDICATOR_ADAPTER_VERSION",
    "TushareFinancialIndicatorAdapter",
    "TushareFinancialIndicatorAdapterConfigurationError",
    "TushareFinancialIndicatorAdapterFailureReason",
    "TushareFinancialIndicatorAdapterResult",
    "TushareFinancialIndicatorMetadataAvailability",
    "TushareFinancialIndicatorSdkClient",
    "TushareIndicatorRowRejection",
]

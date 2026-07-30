"""Qualified Tushare single-stock financial statement adapters.

This module is deliberately not registered in the production financial route.
It exposes the Ticket 04 acquisition seam for Ticket 10 to compose later.
"""

from __future__ import annotations

import math
from collections.abc import Callable
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
from tradingagents.dataflows._tushare_statement_artifacts import (
    TUSHARE_STATEMENT_ADAPTER_VERSION,
    decode_provider_artifact,
    encode_provider_artifact,
    financial_artifact_identity,
    financial_row_artifact_identities,
    require_utc,
)
from tradingagents.dataflows._tushare_statement_normalization import (
    TushareStatementRowRejection,
    project_candidates,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialPeriodCandidate,
    FinancialProviderArtifactIdentity,
    FinancialStatementType,
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
_STATEMENT_ENDPOINTS: dict[FinancialStatementType, str] = {
    FinancialStatementType.BALANCE_SHEET: "balancesheet",
    FinancialStatementType.INCOME_STATEMENT: "income",
    FinancialStatementType.CASH_FLOW: "cashflow",
}
_ROUTING_CAPABILITIES: dict[FinancialStatementType, MainlandCapability] = {
    FinancialStatementType.BALANCE_SHEET: MainlandCapability.BALANCE_SHEET,
    FinancialStatementType.INCOME_STATEMENT: MainlandCapability.INCOME_STATEMENT,
    FinancialStatementType.CASH_FLOW: MainlandCapability.CASH_FLOW,
}


class TushareStatementSdkClient(Protocol):
    """The three qualified single-stock Tushare Pro methods."""

    def balancesheet(self, **kwargs: object) -> pd.DataFrame: ...

    def income(self, **kwargs: object) -> pd.DataFrame: ...

    def cashflow(self, **kwargs: object) -> pd.DataFrame: ...


class TushareStatementAdapterFailureReason(str, Enum):
    NOT_ENABLED = "tushare_statements_not_enabled"
    INCOHERENT_PLAN = "tushare_statements_plan_incoherent"


class TushareStatementAdapterConfigurationError(ValueError):
    """Typed secret-free construction failure raised before provider work."""

    def __init__(self, reason: TushareStatementAdapterFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class TushareStatementAdapterResult(BaseModel):
    """Payload-free adapter result; raw rows remain in the immutable artifact."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["tushare-statement-adapter-v1"] = (
        TUSHARE_STATEMENT_ADAPTER_VERSION
    )
    statement_type: FinancialStatementType
    endpoint_id: Literal["balancesheet", "income", "cashflow"]
    subrequest_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")
    provider_artifact: ProviderSubrequestArtifactRef | None = None
    artifact: FinancialProviderArtifactIdentity | None = None
    row_artifacts: tuple[FinancialProviderArtifactIdentity, ...] = ()
    outcome: ProviderSubrequestOutcome
    sequence_id: str
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]
    annual_candidates: tuple[FinancialPeriodCandidate, ...] = ()
    reporting_period_candidates: tuple[FinancialPeriodCandidate, ...] = ()
    row_rejections: tuple[TushareStatementRowRejection, ...] = ()

    @model_validator(mode="after")
    def _validate_result(self) -> TushareStatementAdapterResult:
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.provider_artifact is not None and self.artifact is not None):
            raise ValueError("Tushare statement artifact contradicts acquisition outcome")
        if not available and (
            self.row_artifacts
            or self.annual_candidates
            or self.reporting_period_candidates
            or self.row_rejections
        ):
            raise ValueError("unavailable Tushare statement cannot emit candidates")
        if available and not self.row_artifacts:
            raise ValueError("available Tushare statement requires row artifacts")
        row_artifact_by_id = {
            item.artifact_identity: item for item in self.row_artifacts
        }
        if len(row_artifact_by_id) != len(self.row_artifacts):
            raise ValueError("Tushare statement row artifacts must be distinct")
        if self.artifact is not None and any(
            item.dataset != self.artifact.dataset
            or item.canonical_request_sha256
            != self.artifact.canonical_request_sha256
            or item.retrieved_at != self.artifact.retrieved_at
            or item.observed_at != self.artifact.observed_at
            for item in self.row_artifacts
        ):
            raise ValueError("Tushare statement row artifact contradicts bulk artifact")
        used_row_artifact_ids: set[str] = set()
        for candidate in (*self.annual_candidates, *self.reporting_period_candidates):
            used_row_artifact_ids.add(candidate.artifact.artifact_identity)
            if (
                candidate.artifact.artifact_identity not in row_artifact_by_id
                or candidate.period_identity.statement_type is not self.statement_type
            ):
                raise ValueError("Tushare statement candidate contradicts its artifact")
        for rejection in self.row_rejections:
            used_row_artifact_ids.add(rejection.artifact_identity)
            if rejection.artifact_identity not in row_artifact_by_id:
                raise ValueError("Tushare statement rejection contradicts its artifact")
        if used_row_artifact_ids != set(row_artifact_by_id):
            raise ValueError("Tushare statement row artifact has no disposition")
        return self


class TushareStatementAdapter:
    """Acquire and normalize one qualified Tushare statement endpoint."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        subrequest_cache: ProviderSubrequestCache,
        sdk_client: TushareStatementSdkClient,
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

    def acquire_balance_sheet(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> TushareStatementAdapterResult:
        return self._acquire(
            statement_type=FinancialStatementType.BALANCE_SHEET,
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )

    def acquire_income_statement(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> TushareStatementAdapterResult:
        return self._acquire(
            statement_type=FinancialStatementType.INCOME_STATEMENT,
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )

    def acquire_cash_flow(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> TushareStatementAdapterResult:
        return self._acquire(
            statement_type=FinancialStatementType.CASH_FLOW,
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )

    def _acquire(
        self,
        *,
        statement_type: FinancialStatementType,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> TushareStatementAdapterResult:
        _validate_instrument_identity(instrument_identity)
        if range_start > as_of_date:
            raise ValueError("Tushare statement range must not end before it starts")
        endpoint_id = _STATEMENT_ENDPOINTS[statement_type]
        upstream_service_id, _ = upstream_service_identity_for_provider(
            "tushare",
            account_scope=self._routing_plan.account_scope_label,
        )
        key = ProviderSubrequestKey.create(
            provider_id="tushare",
            upstream_service_id=upstream_service_id,
            account_scope=self._routing_plan.account_scope_label,
            capacity_scope=endpoint_id,
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
                method = getattr(self._sdk_client, endpoint_id)
                frame = method(
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
                    endpoint_id=endpoint_id,
                    frame=frame,
                    retrieved_at=observed_at,
                    observed_at=observed_at,
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
            operation=f"tushare_statement_{endpoint_id}",
            media_type="application/json",
            physical_request=physical_request,
        )
        if subrequest.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return TushareStatementAdapterResult(
                statement_type=statement_type,
                endpoint_id=endpoint_id,
                subrequest_key=key.subrequest_key,
                outcome=subrequest.outcome,
                sequence_id=subrequest.sequence_id,
                attempt_events=subrequest.attempt_events,
            )
        assert subrequest.value is not None
        assert subrequest.artifact is not None
        payload = decode_provider_artifact(subrequest.value, endpoint_id=endpoint_id)
        artifact = financial_artifact_identity(
            key=key,
            payload=payload,
        )
        row_artifacts = financial_row_artifact_identities(
            key=key,
            response_artifact=artifact,
            payload=payload,
        )
        annual, reporting, rejections = project_candidates(
            payload=payload,
            row_artifacts=row_artifacts,
            statement_type=statement_type,
            instrument_identity=instrument_identity,
            expected_provider_symbol=_tushare_symbol(instrument_identity),
        )
        return TushareStatementAdapterResult(
            statement_type=statement_type,
            endpoint_id=endpoint_id,
            subrequest_key=key.subrequest_key,
            provider_artifact=subrequest.artifact,
            artifact=artifact,
            row_artifacts=row_artifacts,
            outcome=subrequest.outcome,
            sequence_id=subrequest.sequence_id,
            attempt_events=subrequest.attempt_events,
            annual_candidates=annual,
            reporting_period_candidates=reporting,
            row_rejections=rejections,
        )


def _validate_enabled_plan(plan: MainlandCapabilityRoutingPlan) -> None:
    if (
        plan.mode is not MainlandCapabilityRoutingMode.QUALIFIED_V1
        or TushareCapability.STATEMENTS not in plan.enabled_tushare_capabilities
    ):
        raise TushareStatementAdapterConfigurationError(
            TushareStatementAdapterFailureReason.NOT_ENABLED
        )
    endpoint_ids = {item.endpoint_id for item in plan.endpoint_pacing_identities}
    coherent = all(
        plan.route_for(capability)[0] == "tushare"
        for capability in _ROUTING_CAPABILITIES.values()
    ) and endpoint_ids.issuperset(_STATEMENT_ENDPOINTS.values())
    if not coherent:
        raise TushareStatementAdapterConfigurationError(
            TushareStatementAdapterFailureReason.INCOHERENT_PLAN
        )


def _validate_instrument_identity(identity: InstrumentIdentityEvidence) -> None:
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.currency != "CNY"
        or identity.venue not in {"XSHG", "XSHE"}
    ):
        raise ValueError("Tushare statements require a mainland Equity identity")


def _tushare_symbol(identity: InstrumentIdentityEvidence) -> str:
    symbol = identity.symbol.strip().upper()
    if symbol.endswith(".SS"):
        return symbol[:-3] + ".SH"
    if symbol.endswith(".SZ"):
        return symbol
    raise ValueError("Tushare statement symbol is not a canonical mainland symbol")


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
    "TUSHARE_STATEMENT_ADAPTER_VERSION",
    "TushareStatementAdapter",
    "TushareStatementAdapterConfigurationError",
    "TushareStatementAdapterFailureReason",
    "TushareStatementAdapterResult",
    "TushareStatementRowRejection",
    "TushareStatementSdkClient",
]

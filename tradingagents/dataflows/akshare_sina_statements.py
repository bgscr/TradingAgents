"""Qualified AKShare-Sina financial-statement adapters.

Ticket 07 intentionally leaves this adapter unregistered. Ticket 10 owns
dispatcher-level routing and Yahoo fallback; legacy/default routes remain intact.
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
)
from tradingagents.dataflows._akshare_sina_statement_artifacts import (
    AKSHARE_SINA_STATEMENT_ADAPTER_VERSION,
    decode_provider_artifact,
    encode_provider_artifact,
    financial_artifact_identity,
    financial_row_artifact_identities,
    require_utc,
)
from tradingagents.dataflows._akshare_sina_statement_normalization import (
    AkshareSinaStatementRowRejection,
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
_STATEMENT_IDS: dict[FinancialStatementType, str] = {
    FinancialStatementType.BALANCE_SHEET: "balance_sheet",
    FinancialStatementType.INCOME_STATEMENT: "income_statement",
    FinancialStatementType.CASH_FLOW: "cash_flow",
}
_SINA_STATEMENT_SYMBOLS: dict[FinancialStatementType, str] = {
    FinancialStatementType.BALANCE_SHEET: "资产负债表",
    FinancialStatementType.INCOME_STATEMENT: "利润表",
    FinancialStatementType.CASH_FLOW: "现金流量表",
}
_ROUTING_CAPABILITIES: dict[FinancialStatementType, MainlandCapability] = {
    FinancialStatementType.BALANCE_SHEET: MainlandCapability.BALANCE_SHEET,
    FinancialStatementType.INCOME_STATEMENT: MainlandCapability.INCOME_STATEMENT,
    FinancialStatementType.CASH_FLOW: MainlandCapability.CASH_FLOW,
}


class AkshareSinaStatementSdkClient(Protocol):
    """The qualified existing AKShare Sina financial-report endpoint seam."""

    def stock_financial_report_sina(
        self,
        *,
        stock: str,
        symbol: str,
    ) -> pd.DataFrame: ...


class AkshareSinaStatementAdapterFailureReason(str, Enum):
    NOT_ENABLED = "akshare_sina_statements_not_enabled"
    INCOHERENT_PLAN = "akshare_sina_statements_plan_incoherent"


class AkshareSinaStatementAdapterConfigurationError(ValueError):
    """Typed secret-free construction failure raised before provider work."""

    def __init__(self, reason: AkshareSinaStatementAdapterFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class AkshareSinaStatementAdapterResult(BaseModel):
    """Payload-free result; exact original rows remain in immutable storage."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["akshare-sina-statement-adapter-v1"] = (
        AKSHARE_SINA_STATEMENT_ADAPTER_VERSION
    )
    statement_type: FinancialStatementType
    endpoint_id: Literal["stock_financial_report_sina"] = (
        "stock_financial_report_sina"
    )
    statement_id: Literal["balance_sheet", "income_statement", "cash_flow"]
    provider_statement_symbol: Literal["资产负债表", "利润表", "现金流量表"]
    subrequest_key: str = Field(pattern=r"^provider-subrequest:v1:[0-9a-f]{64}$")
    provider_artifact: ProviderSubrequestArtifactRef | None = None
    artifact: FinancialProviderArtifactIdentity | None = None
    row_artifacts: tuple[FinancialProviderArtifactIdentity, ...] = ()
    outcome: ProviderSubrequestOutcome
    sequence_id: str
    attempt_events: tuple[ProviderSubrequestAttemptEvent, ...]
    annual_candidates: tuple[FinancialPeriodCandidate, ...] = ()
    reporting_period_candidates: tuple[FinancialPeriodCandidate, ...] = ()
    row_rejections: tuple[AkshareSinaStatementRowRejection, ...] = ()

    @model_validator(mode="after")
    def _validate_result(self) -> AkshareSinaStatementAdapterResult:
        if self.statement_id != _STATEMENT_IDS[self.statement_type]:
            raise ValueError("AKShare-Sina statement ID contradicts statement type")
        if self.provider_statement_symbol != _SINA_STATEMENT_SYMBOLS[self.statement_type]:
            raise ValueError("AKShare-Sina provider symbol contradicts statement type")
        available = self.outcome.kind is ProviderSubrequestOutcomeKind.AVAILABLE
        if available != (self.provider_artifact is not None and self.artifact is not None):
            raise ValueError("AKShare-Sina artifact contradicts acquisition outcome")
        if not available and (
            self.row_artifacts
            or self.annual_candidates
            or self.reporting_period_candidates
            or self.row_rejections
        ):
            raise ValueError("unavailable AKShare-Sina statement cannot emit periods")
        if available and not self.row_artifacts:
            raise ValueError("available AKShare-Sina statement requires row artifacts")
        row_artifact_by_id = {
            item.artifact_identity: item for item in self.row_artifacts
        }
        if len(row_artifact_by_id) != len(self.row_artifacts):
            raise ValueError("AKShare-Sina row artifacts must be distinct")
        if self.artifact is not None and any(
            item.dataset != self.artifact.dataset
            or item.canonical_request_sha256 != self.artifact.canonical_request_sha256
            or item.retrieved_at != self.artifact.retrieved_at
            or item.observed_at != self.artifact.observed_at
            for item in self.row_artifacts
        ):
            raise ValueError("AKShare-Sina row artifact contradicts response artifact")
        disposed: set[str] = set()
        for candidate in (*self.annual_candidates, *self.reporting_period_candidates):
            disposed.add(candidate.artifact.artifact_identity)
            if (
                candidate.artifact.artifact_identity not in row_artifact_by_id
                or candidate.period_identity.statement_type is not self.statement_type
            ):
                raise ValueError("AKShare-Sina candidate contradicts retained artifact")
        for rejection in self.row_rejections:
            disposed.add(rejection.artifact_identity)
            if rejection.artifact_identity not in row_artifact_by_id:
                raise ValueError("AKShare-Sina rejection contradicts retained artifact")
        if disposed != set(row_artifact_by_id):
            raise ValueError("AKShare-Sina row artifact has no disposition")
        return self


class AkshareSinaStatementBatchResult(BaseModel):
    """Three independent statement outcomes with no provider fallback policy."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["akshare-sina-statement-batch-v1"] = (
        "akshare-sina-statement-batch-v1"
    )
    results: tuple[AkshareSinaStatementAdapterResult, ...]

    @model_validator(mode="after")
    def _validate_results(self) -> AkshareSinaStatementBatchResult:
        expected = tuple(_STATEMENT_IDS)
        actual = tuple(result.statement_type for result in self.results)
        if actual != expected:
            raise ValueError("AKShare-Sina batch requires each statement exactly once")
        if len({result.subrequest_key for result in self.results}) != len(self.results):
            raise ValueError("AKShare-Sina statements require independent subrequests")
        return self

    def result_for(
        self,
        statement_type: FinancialStatementType,
    ) -> AkshareSinaStatementAdapterResult:
        for result in self.results:
            if result.statement_type is statement_type:
                return result
        raise ValueError("AKShare-Sina batch does not contain the requested statement")


class AkshareSinaStatementAdapter:
    """Acquire one statement per coordinated AKShare-Sina physical request."""

    def __init__(
        self,
        *,
        routing_plan: MainlandCapabilityRoutingPlan,
        subrequest_cache: ProviderSubrequestCache,
        sdk_client: AkshareSinaStatementSdkClient,
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
    ) -> AkshareSinaStatementAdapterResult:
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
    ) -> AkshareSinaStatementAdapterResult:
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
    ) -> AkshareSinaStatementAdapterResult:
        return self._acquire(
            statement_type=FinancialStatementType.CASH_FLOW,
            instrument_identity=instrument_identity,
            range_start=range_start,
            as_of_date=as_of_date,
        )

    def acquire_statements(
        self,
        *,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> AkshareSinaStatementBatchResult:
        """Acquire all three statements sequentially and salvage every sibling."""

        return AkshareSinaStatementBatchResult(
            results=tuple(
                self._acquire(
                    statement_type=statement_type,
                    instrument_identity=instrument_identity,
                    range_start=range_start,
                    as_of_date=as_of_date,
                )
                for statement_type in _STATEMENT_IDS
            )
        )

    def _acquire(
        self,
        *,
        statement_type: FinancialStatementType,
        instrument_identity: InstrumentIdentityEvidence,
        range_start: date,
        as_of_date: date,
    ) -> AkshareSinaStatementAdapterResult:
        _validate_instrument_identity(instrument_identity)
        if range_start > as_of_date:
            raise ValueError("AKShare-Sina statement range must not end before it starts")
        statement_id = _STATEMENT_IDS[statement_type]
        provider_statement_symbol = _SINA_STATEMENT_SYMBOLS[statement_type]
        upstream_service_id, _ = upstream_service_identity_for_provider(
            "akshare_sina"
        )
        capacity_scope = f"stock_financial_report_sina.{statement_id}"
        key = ProviderSubrequestKey.create(
            provider_id="akshare_sina",
            upstream_service_id=upstream_service_id,
            account_scope="public",
            capacity_scope=capacity_scope,
            instrument_identity=instrument_identity,
            requested_range_start=range_start.isoformat(),
            requested_range_end=as_of_date.isoformat(),
            requested_fields=(statement_id,),
            as_of_date=as_of_date,
            qualification_profile=self._routing_plan.qualification_profile,
            normalizer_version=self._routing_plan.normalizer_version,
            data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
        )

        def physical_request() -> bytes:
            try:
                frame = self._sdk_client.stock_financial_report_sina(
                    stock=_sina_symbol(instrument_identity),
                    symbol=provider_statement_symbol,
                )
            except PhysicalAttemptFailure:
                raise
            except Exception as exc:
                raise _sanitized_sdk_failure(exc) from None
            if not isinstance(frame, pd.DataFrame):
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                    retryable=False,
                    error_code="akshare_sina_response_not_frame",
                )
            if frame.empty:
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.EMPTY_FRAME,
                    retryable=False,
                    error_code="akshare_sina_empty_frame",
                )
            observed_at = require_utc(self._now())
            try:
                return encode_provider_artifact(
                    statement_id=statement_id,
                    frame=frame,
                    retrieved_at=observed_at,
                    observed_at=observed_at,
                )
            except (TypeError, ValueError, OverflowError):
                raise PhysicalAttemptFailure(
                    outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
                    retryable=False,
                    error_code="akshare_sina_response_not_serializable",
                ) from None

        subrequest = self._cache.execute(
            key,
            owner_id=self._owner_id,
            priority=RequestPriority.INTERACTIVE_MAINLAND,
            now=self._now,
            sleep=self._sleep,
            lease_duration=self._lease_duration,
            operation=f"akshare_sina_statement_{statement_id}",
            media_type="application/json",
            physical_request=physical_request,
        )
        common = {
            "statement_type": statement_type,
            "statement_id": statement_id,
            "provider_statement_symbol": provider_statement_symbol,
            "subrequest_key": key.subrequest_key,
            "outcome": subrequest.outcome,
            "sequence_id": subrequest.sequence_id,
            "attempt_events": subrequest.attempt_events,
        }
        if subrequest.outcome.kind is not ProviderSubrequestOutcomeKind.AVAILABLE:
            return AkshareSinaStatementAdapterResult(**common)
        assert subrequest.value is not None
        assert subrequest.artifact is not None
        payload = decode_provider_artifact(
            subrequest.value,
            statement_id=statement_id,
        )
        artifact = financial_artifact_identity(key=key, payload=payload)
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
            range_start=range_start,
            as_of_date=as_of_date,
        )
        return AkshareSinaStatementAdapterResult(
            **common,
            provider_artifact=subrequest.artifact,
            artifact=artifact,
            row_artifacts=row_artifacts,
            annual_candidates=annual,
            reporting_period_candidates=reporting,
            row_rejections=rejections,
        )


def _validate_enabled_plan(plan: MainlandCapabilityRoutingPlan) -> None:
    if plan.mode not in {
        MainlandCapabilityRoutingMode.QUALIFIED_V1,
        MainlandCapabilityRoutingMode.QUALIFIED_V1_SHADOW,
    }:
        raise AkshareSinaStatementAdapterConfigurationError(
            AkshareSinaStatementAdapterFailureReason.NOT_ENABLED
        )
    coherent = all(
        "akshare_sina" in plan.route_for(capability)
        for capability in _ROUTING_CAPABILITIES.values()
    )
    if not coherent:
        raise AkshareSinaStatementAdapterConfigurationError(
            AkshareSinaStatementAdapterFailureReason.INCOHERENT_PLAN
        )


def _validate_instrument_identity(identity: InstrumentIdentityEvidence) -> None:
    if (
        not identity.is_authoritative
        or identity.instrument_kind is not InstrumentKind.EQUITY
        or identity.currency != "CNY"
        or identity.venue not in {"XSHG", "XSHE"}
    ):
        raise ValueError("AKShare-Sina statements require a mainland Equity identity")


def _sina_symbol(identity: InstrumentIdentityEvidence) -> str:
    symbol = identity.symbol.strip().upper()
    if symbol.endswith(".SS"):
        return "sh" + symbol[:-3]
    if symbol.endswith(".SZ"):
        return "sz" + symbol[:-3]
    raise ValueError("AKShare-Sina symbol is not a canonical mainland symbol")


def _sanitized_sdk_failure(exc: Exception) -> PhysicalAttemptFailure:
    type_name = type(exc).__name__.casefold()
    message = str(exc)[:512].casefold()
    retry_after = _retry_after_seconds(exc)
    if isinstance(exc, TimeoutError) or "timeout" in type_name:
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.TIMEOUT,
            retryable=True,
            error_code="akshare_sina_timeout",
        )
    if isinstance(exc, ConnectionError) or any(
        marker in type_name or marker in message
        for marker in ("connection", "disconnect", "proxyerror", "remote disconnected")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.DISCONNECT,
            retryable=True,
            error_code="akshare_sina_disconnect",
        )
    if any(
        marker in message
        for marker in ("rate limit", "too many requests", "频率", "限流")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            retry_after_seconds=retry_after,
            error_code="akshare_sina_rate_limited",
            rate_limit_scope=RateLimitScope.CAPACITY,
        )
    if any(
        marker in message
        for marker in ("permission", "access denied", "forbidden", "权限", "拒绝访问")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PERMISSION_DENIED,
            retryable=False,
            error_code="akshare_sina_permission_denied",
        )
    if any(
        marker in message
        for marker in ("authentication", "unauthorized", "invalid credential", "认证")
    ):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code="akshare_sina_authentication",
        )
    if isinstance(exc, KeyError | TypeError | ValueError):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="akshare_sina_malformed_response",
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=False,
        error_code="akshare_sina_provider_error",
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
    "AKSHARE_SINA_STATEMENT_ADAPTER_VERSION",
    "AkshareSinaStatementAdapter",
    "AkshareSinaStatementAdapterConfigurationError",
    "AkshareSinaStatementAdapterFailureReason",
    "AkshareSinaStatementBatchResult",
    "AkshareSinaStatementAdapterResult",
    "AkshareSinaStatementRowRejection",
    "AkshareSinaStatementSdkClient",
]

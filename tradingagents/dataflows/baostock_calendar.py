from __future__ import annotations

import contextlib
import io
import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.market_history.config import DataUsageMode
from tradingagents.market_history.coordinator import (
    PhysicalAttemptBudgetExhausted,
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
    ProviderPhysicalAttemptEvent,
    ProviderRequestCoordinator,
    RateLimitScope,
    RequestPriority,
    record_active_physical_attempt_io,
    upstream_service_identity_for_provider,
)

BAOSTOCK_CALENDAR_REQUEST_VERSION = "market-session-calendar-acquisition-request-v1"
BAOSTOCK_CALENDAR_DATASET_NAME = "mainland-session-calendar-v1"
BAOSTOCK_CALENDAR_NORMALIZER_VERSION = "baostock-session-calendar-normalizer-v1"
BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT = "mainland-session-calendar-payload-v1"
MAINLAND_REGULAR_SESSION_TIME_CONTRACT = "mainland-cn-regular-session-times-v1"
BAOSTOCK_CALENDAR_CAPACITY_SCOPE = "session-calendar"
BAOSTOCK_CALENDAR_PROVENANCE = "observed_point_in_time"
_REFERENCE_MARKET = "mainland-cn"
_TIMEZONE_NAME = "Asia/Shanghai"
_MAX_SAFE_INCLUSIVE_RANGE_DAYS = 732
_BAOSTOCK_LOGIN_CAPACITY_COOLDOWN_SECONDS = 300.0
_SAFE_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)


class BaoStockSessionCalendarTransport(Protocol):
    def login(self) -> object: ...

    def query_trade_dates(self, *, start_date: str, end_date: str) -> object: ...

    def logout(self) -> object: ...


class BaoStockSessionCalendarFailureReason(str, Enum):
    OPERATOR_AUTHORIZATION_REQUIRED = "operator_authorization_required"
    UNBOUNDED_RANGE = "calendar_range_unbounded"
    REVERSED_RANGE = "calendar_range_reversed"
    RANGE_OUT_OF_POLICY = "calendar_range_out_of_policy"
    UNSUPPORTED_PROVIDER = "calendar_provider_unsupported"
    UNSUPPORTED_REFERENCE_MARKET = "calendar_reference_market_unsupported"
    UNSUPPORTED_TIMEZONE = "calendar_timezone_unsupported"
    UNSUPPORTED_DATA_USAGE_MODE = "calendar_data_usage_mode_unsupported"
    UNSAFE_ACCOUNT_SCOPE = "calendar_account_scope_unsafe"
    UNSUPPORTED_CAPACITY_SCOPE = "calendar_capacity_scope_unsupported"
    UNSUPPORTED_DATASET = "calendar_dataset_unsupported"
    UNSUPPORTED_NORMALIZER = "calendar_normalizer_unsupported"
    UNSUPPORTED_PAYLOAD_CONTRACT = "calendar_payload_contract_unsupported"
    UNSUPPORTED_SESSION_TIME_CONTRACT = "calendar_session_time_contract_unsupported"
    EMPTY_RESPONSE = "calendar_provider_response_empty"
    MALFORMED_ROW = "calendar_provider_row_malformed"
    INVALID_TRADING_DAY = "calendar_provider_membership_invalid"
    DUPLICATE_DATE = "calendar_provider_date_duplicate"
    CONTRADICTORY_MEMBERSHIP = "calendar_provider_membership_contradictory"
    OUT_OF_RANGE_DATE = "calendar_provider_date_out_of_range"
    INCOMPLETE_RANGE = "calendar_provider_range_incomplete"
    COOLDOWN_ACTIVE = "provider_cooldown_active"
    RATE_LIMITED = "provider_rate_limited"
    UPSTREAM_BUSY = "provider_upstream_busy"
    LOGIN_FAILED = "baostock_login_failed"
    QUERY_FAILED = "baostock_calendar_query_failed"
    LOGOUT_FAILED = "baostock_logout_failed"
    INTERRUPTED = "calendar_acquisition_interrupted"
    UNSAFE_OWNER_SCOPE = "calendar_owner_scope_unsafe"
    PROVIDER_ERROR = "calendar_provider_error"


class BaoStockSessionCalendarTerminalOutcome(str, Enum):
    AVAILABLE = "available"


class BaoStockSessionCalendarRequest(BaseModel):
    """Closed Instrument-neutral request for one bounded calendar acquisition."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["market-session-calendar-acquisition-request-v1"] = (
        BAOSTOCK_CALENDAR_REQUEST_VERSION
    )
    provider: str = "baostock"
    endpoint: str = "query_trade_dates"
    reference_market: str = _REFERENCE_MARKET
    timezone_name: str = _TIMEZONE_NAME
    start_date: date | None = None
    end_date: date | None = None
    maximum_inclusive_range_days: int = Field(
        default=_MAX_SAFE_INCLUSIVE_RANGE_DAYS,
        ge=1,
        le=_MAX_SAFE_INCLUSIVE_RANGE_DAYS,
        strict=True,
    )
    data_usage_mode: DataUsageMode
    account_scope: str = "not-applicable"
    capacity_scope: str = BAOSTOCK_CALENDAR_CAPACITY_SCOPE
    dataset_name: str = BAOSTOCK_CALENDAR_DATASET_NAME
    normalizer_version: str = BAOSTOCK_CALENDAR_NORMALIZER_VERSION
    payload_contract: str = BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT
    session_time_contract: str = MAINLAND_REGULAR_SESSION_TIME_CONTRACT
    operator_provider_io_authorized: bool = Field(
        default=False,
        exclude=True,
        repr=False,
        strict=True,
    )


class MainlandSessionCalendarMembership(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    calendar_date: date
    is_trading_day: bool


class BaoStockSessionCalendarAttempt(BaseModel):
    """Safe immutable projection of one existing-schema attempt event."""

    model_config = _CLOSED_MODEL_CONFIG

    attempt_event_id: str
    sequence_id: str
    request_key: str
    upstream_service_id: str
    upstream_service_name: str
    capacity_scope: str
    operation: str
    attempt_index: int
    attempted_at: datetime
    pacing_event: str
    pacing_wait_seconds: float
    outcome: PhysicalAttemptOutcome
    retryable: bool
    status_code: int | None
    error_code: str | None
    retry_after_seconds: float | None
    cooldown_changed: bool
    cooldown_until: datetime | None
    final_physical_attempt_count: int | None

    @classmethod
    def from_coordinator(
        cls,
        event: ProviderPhysicalAttemptEvent,
    ) -> BaoStockSessionCalendarAttempt:
        return cls(**vars(event))


class BaoStockSessionCalendarAcquisition(BaseModel):
    """Closed provider-neutral acquisition candidate for CAL-02 publication."""

    model_config = _CLOSED_MODEL_CONFIG

    provider: str
    endpoint: str
    upstream_service_id: str
    upstream_service_name: str
    provider_dataset_id: str
    dataset_name: str
    reference_market: str
    timezone_name: str
    requested_start_date: date
    requested_end_date: date
    data_usage_mode: DataUsageMode
    normalizer_version: str
    payload_contract: str
    session_time_contract: str
    provenance: str
    requested_at: datetime
    retrieved_at: datetime
    observed_at: datetime
    membership: tuple[MainlandSessionCalendarMembership, ...]
    content_identity: str
    logical_request_id: str
    workflow_sequence_id: str
    physical_attempt_count: int
    attempt_events: tuple[BaoStockSessionCalendarAttempt, ...]
    terminal_outcome: BaoStockSessionCalendarTerminalOutcome

    @model_validator(mode="after")
    def _validate_available_result(self) -> BaoStockSessionCalendarAcquisition:
        if (
            self.provider != "baostock"
            or self.endpoint != "query_trade_dates"
            or self.dataset_name != BAOSTOCK_CALENDAR_DATASET_NAME
            or self.reference_market != _REFERENCE_MARKET
            or self.timezone_name != _TIMEZONE_NAME
            or self.normalizer_version != BAOSTOCK_CALENDAR_NORMALIZER_VERSION
            or self.payload_contract != BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT
            or self.session_time_contract != MAINLAND_REGULAR_SESSION_TIME_CONTRACT
            or self.provenance != BAOSTOCK_CALENDAR_PROVENANCE
        ):
            raise ValueError("calendar acquisition authority is invalid")
        if self.requested_start_date > self.requested_end_date:
            raise ValueError("calendar acquisition range is invalid")
        expected_count = (self.requested_end_date - self.requested_start_date).days + 1
        membership_dates = tuple(item.calendar_date for item in self.membership)
        if (
            len(self.membership) != expected_count
            or membership_dates != tuple(sorted(set(membership_dates)))
            or (
                membership_dates
                and (
                    membership_dates[0] != self.requested_start_date
                    or membership_dates[-1] != self.requested_end_date
                )
            )
        ):
            raise ValueError("calendar acquisition membership is incomplete")
        timestamps = (self.requested_at, self.retrieved_at, self.observed_at)
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("calendar acquisition timestamps must be timezone-aware")
        if self.physical_attempt_count != 3 or len(self.attempt_events) != 3:
            raise ValueError("calendar acquisition requires exactly three attempts")
        if tuple(item.operation for item in self.attempt_events) != (
            "baostock_login",
            "query_trade_dates",
            "baostock_logout",
        ):
            raise ValueError("calendar acquisition operation order is invalid")
        if tuple(item.attempt_index for item in self.attempt_events) != (1, 2, 3):
            raise ValueError("calendar acquisition attempt indices are invalid")
        if any(
            item.sequence_id != self.workflow_sequence_id
            or item.request_key != self.logical_request_id
            or item.final_physical_attempt_count != 3
            or item.outcome is not PhysicalAttemptOutcome.AVAILABLE
            for item in self.attempt_events
        ):
            raise ValueError("calendar acquisition attempt binding is invalid")
        return self


class BaoStockSessionCalendarAcquisitionError(RuntimeError):
    """Closed payload-free terminal acquisition failure."""

    def __init__(
        self,
        reason: BaoStockSessionCalendarFailureReason,
        *,
        physical_outcome: PhysicalAttemptOutcome | None = None,
        sequence_id: str = "",
        attempt_events: tuple[ProviderPhysicalAttemptEvent, ...] = (),
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        self.physical_outcome = physical_outcome
        self.sequence_id = sequence_id
        self.attempt_events = tuple(
            BaoStockSessionCalendarAttempt.from_coordinator(event)
            for event in attempt_events
        )
        self.physical_attempt_count = len(attempt_events)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(reason.value)


class _BaoStockSdkTransport:
    """Lazy production SDK boundary; construction performs no provider work."""

    @staticmethod
    def _sdk():
        import baostock

        return baostock

    def login(self) -> object:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            return self._sdk().login()

    def query_trade_dates(self, *, start_date: str, end_date: str) -> object:
        return self._sdk().query_trade_dates(
            start_date=start_date,
            end_date=end_date,
        )

    def logout(self) -> object:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            return self._sdk().logout()


def acquire_mainland_session_calendar(
    request: BaoStockSessionCalendarRequest,
    *,
    coordinator: ProviderRequestCoordinator,
    transport_factory: Callable[[], BaoStockSessionCalendarTransport] = (
        _BaoStockSdkTransport
    ),
    owner_id: str,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    lease_duration: timedelta,
) -> BaoStockSessionCalendarAcquisition:
    """Acquire one complete BaoStock calendar candidate without persistence."""

    start_date, end_date = _validate_request(request)
    if (
        not isinstance(owner_id, str)
        or _SAFE_SCOPE.fullmatch(owner_id) is None
        or any(
            marker in owner_id.casefold()
            for marker in ("credential", "password", "secret", "token", "api_key")
        )
    ):
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.UNSAFE_OWNER_SCOPE
        )
    requested_at = _utc(now())
    upstream_id, upstream_name = upstream_service_identity_for_provider("baostock")
    logical_request_id = _logical_request_id(
        request,
        start_date=start_date,
        end_date=end_date,
        upstream_service_id=upstream_id,
    )

    def workflow(execute_operation):
        transport = transport_factory()
        execute_operation(
            "baostock_login",
            lambda: _login(transport),
        )
        primary_failure: PhysicalAttemptFailure | None = None
        payload: dict[str, object] | None = None
        try:
            payload = execute_operation(
                "query_trade_dates",
                lambda: _query_and_normalize(
                    transport,
                    start_date=start_date,
                    end_date=end_date,
                    now=now,
                ),
            )
        except PhysicalAttemptFailure as exc:
            primary_failure = exc
        try:
            execute_operation("baostock_logout", lambda: _logout(transport))
        except PhysicalAttemptFailure as exc:
            if primary_failure is None:
                primary_failure = exc
        if primary_failure is not None:
            raise primary_failure
        assert payload is not None
        payload["requested_at"] = requested_at
        return payload

    try:
        coordinated = coordinator.execute_physical_workflow(
            request_key=logical_request_id,
            upstream_service_id=upstream_id,
            owner_id=owner_id,
            priority=RequestPriority.INCREMENTAL_REFRESH,
            now=now,
            sleep=sleep,
            lease_duration=lease_duration,
            max_physical_operations=3,
            workflow=workflow,
            cooldown_scope=BAOSTOCK_CALENDAR_CAPACITY_SCOPE,
        )
    except PhysicalAttemptBudgetExhausted as exc:
        raise _public_failure(exc) from None

    payload = coordinated.value
    if not isinstance(payload, Mapping):  # pragma: no cover - coordinator invariant
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.PROVIDER_ERROR
        )
    membership = tuple(
        MainlandSessionCalendarMembership(
            calendar_date=item[0],
            is_trading_day=item[1],
        )
        for item in payload["membership"]
    )
    provider_dataset_id = _provider_dataset_id(upstream_id)
    return BaoStockSessionCalendarAcquisition(
        provider="baostock",
        endpoint="query_trade_dates",
        upstream_service_id=upstream_id,
        upstream_service_name=upstream_name,
        provider_dataset_id=provider_dataset_id,
        dataset_name=BAOSTOCK_CALENDAR_DATASET_NAME,
        reference_market=_REFERENCE_MARKET,
        timezone_name=_TIMEZONE_NAME,
        requested_start_date=start_date,
        requested_end_date=end_date,
        data_usage_mode=request.data_usage_mode,
        normalizer_version=BAOSTOCK_CALENDAR_NORMALIZER_VERSION,
        payload_contract=BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT,
        session_time_contract=MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
        provenance=BAOSTOCK_CALENDAR_PROVENANCE,
        requested_at=payload["requested_at"],
        retrieved_at=payload["retrieved_at"],
        observed_at=payload["observed_at"],
        membership=membership,
        content_identity=payload["content_identity"],
        logical_request_id=logical_request_id,
        workflow_sequence_id=coordinated.sequence_id,
        physical_attempt_count=coordinated.physical_attempt_count,
        attempt_events=tuple(
            BaoStockSessionCalendarAttempt.from_coordinator(event)
            for event in coordinated.attempt_events
        ),
        terminal_outcome=BaoStockSessionCalendarTerminalOutcome.AVAILABLE,
    )


def _validate_request(
    request: BaoStockSessionCalendarRequest,
) -> tuple[date, date]:
    if request.provider != "baostock" or request.endpoint != "query_trade_dates":
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_PROVIDER
        )
    if request.reference_market != _REFERENCE_MARKET:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_REFERENCE_MARKET
        )
    if request.timezone_name != _TIMEZONE_NAME:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_TIMEZONE
        )
    if request.data_usage_mode is not DataUsageMode.PERSONAL_RESEARCH:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_DATA_USAGE_MODE
        )
    if (
        request.account_scope != "not-applicable"
        or _SAFE_SCOPE.fullmatch(request.account_scope) is None
    ):
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.UNSAFE_ACCOUNT_SCOPE
        )
    if request.capacity_scope != BAOSTOCK_CALENDAR_CAPACITY_SCOPE:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_CAPACITY_SCOPE
        )
    fixed_contracts = (
        (
            request.dataset_name,
            BAOSTOCK_CALENDAR_DATASET_NAME,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_DATASET,
        ),
        (
            request.normalizer_version,
            BAOSTOCK_CALENDAR_NORMALIZER_VERSION,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_NORMALIZER,
        ),
        (
            request.payload_contract,
            BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_PAYLOAD_CONTRACT,
        ),
        (
            request.session_time_contract,
            MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_SESSION_TIME_CONTRACT,
        ),
    )
    for value, expected, reason in fixed_contracts:
        if value != expected:
            raise BaoStockSessionCalendarAcquisitionError(reason)
    if request.start_date is None or request.end_date is None:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.UNBOUNDED_RANGE
        )
    if request.start_date > request.end_date:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.REVERSED_RANGE
        )
    inclusive_days = (request.end_date - request.start_date).days + 1
    if inclusive_days > request.maximum_inclusive_range_days:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.RANGE_OUT_OF_POLICY
        )
    if not request.operator_provider_io_authorized:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.OPERATOR_AUTHORIZATION_REQUIRED
        )
    return request.start_date, request.end_date


def _login(transport: BaoStockSessionCalendarTransport) -> bool:
    record_active_physical_attempt_io()
    try:
        result = transport.login()
    except PhysicalAttemptFailure:
        raise
    except Exception as exc:
        raise _transport_failure(exc, operation="login") from None
    error_code = _provider_error_code(result)
    if error_code == "10001005":
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            error_code="10001005",
            retry_after_seconds=_BAOSTOCK_LOGIN_CAPACITY_COOLDOWN_SECONDS,
            rate_limit_scope=RateLimitScope.UPSTREAM,
        )
    if error_code != "0":
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code="baostock_login_rejected",
        )
    return True


def _query_and_normalize(
    transport: BaoStockSessionCalendarTransport,
    *,
    start_date: date,
    end_date: date,
    now: Callable[[], datetime],
) -> dict[str, object]:
    record_active_physical_attempt_io()
    try:
        result = transport.query_trade_dates(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        membership = _normalize_provider_rows(
            result,
            start_date=start_date,
            end_date=end_date,
        )
        observed_at = _utc(now())
    except PhysicalAttemptFailure:
        raise
    except BaoStockSessionCalendarAcquisitionError as exc:
        raise PhysicalAttemptFailure(
            outcome=(
                PhysicalAttemptOutcome.EMPTY_FRAME
                if exc.reason is BaoStockSessionCalendarFailureReason.EMPTY_RESPONSE
                else PhysicalAttemptOutcome.MALFORMED_RESPONSE
            ),
            retryable=False,
            error_code=exc.reason.value,
        ) from None
    except Exception as exc:
        raise _transport_failure(exc, operation="query") from None
    retrieved_at = observed_at
    membership_payload = tuple(
        (item.calendar_date, item.is_trading_day) for item in membership
    )
    content_identity = _content_identity(membership)
    return {
        "retrieved_at": retrieved_at,
        "observed_at": observed_at,
        "membership": membership_payload,
        "content_identity": content_identity,
    }


def _logout(transport: BaoStockSessionCalendarTransport) -> bool:
    record_active_physical_attempt_io()
    try:
        result = transport.logout()
    except PhysicalAttemptFailure:
        raise
    except Exception as exc:
        raise _transport_failure(exc, operation="logout") from None
    if _provider_error_code(result) != "0":
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
            retryable=False,
            error_code="baostock_logout_rejected",
        )
    return True


def _normalize_provider_rows(
    result: object,
    *,
    start_date: date,
    end_date: date,
) -> tuple[MainlandSessionCalendarMembership, ...]:
    if _provider_error_code(result) != "0":
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
            retryable=False,
            error_code="baostock_calendar_query_rejected",
        )
    raw_fields = getattr(result, "fields", None)
    if not isinstance(raw_fields, (list, tuple)):
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.MALFORMED_ROW
        )
    fields = tuple(str(item) for item in raw_fields)
    if (
        fields.count("calendar_date") != 1
        or fields.count("is_trading_day") != 1
    ):
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.MALFORMED_ROW
        )
    rows: dict[date, bool] = {}
    max_rows = (end_date - start_date).days + 2
    row_count = 0
    try:
        while result.next():
            row_count += 1
            if row_count > max_rows:
                raise BaoStockSessionCalendarAcquisitionError(
                    BaoStockSessionCalendarFailureReason.OUT_OF_RANGE_DATE
                )
            values = result.get_row_data()
            if not isinstance(values, (list, tuple)) or len(values) != len(fields):
                raise BaoStockSessionCalendarAcquisitionError(
                    BaoStockSessionCalendarFailureReason.MALFORMED_ROW
                )
            row = dict(zip(fields, values, strict=True))
            raw_date = row["calendar_date"]
            if not isinstance(raw_date, str) or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", raw_date
            ) is None:
                raise BaoStockSessionCalendarAcquisitionError(
                    BaoStockSessionCalendarFailureReason.MALFORMED_ROW
                )
            try:
                calendar_date = date.fromisoformat(raw_date)
            except (TypeError, ValueError):
                raise BaoStockSessionCalendarAcquisitionError(
                    BaoStockSessionCalendarFailureReason.MALFORMED_ROW
                ) from None
            raw_membership = row["is_trading_day"]
            if not isinstance(raw_membership, str) or raw_membership not in {"0", "1"}:
                raise BaoStockSessionCalendarAcquisitionError(
                    BaoStockSessionCalendarFailureReason.INVALID_TRADING_DAY
                )
            membership = raw_membership == "1"
            if not start_date <= calendar_date <= end_date:
                raise BaoStockSessionCalendarAcquisitionError(
                    BaoStockSessionCalendarFailureReason.OUT_OF_RANGE_DATE
                )
            if calendar_date in rows:
                reason = (
                    BaoStockSessionCalendarFailureReason.CONTRADICTORY_MEMBERSHIP
                    if rows[calendar_date] is not membership
                    else BaoStockSessionCalendarFailureReason.DUPLICATE_DATE
                )
                raise BaoStockSessionCalendarAcquisitionError(reason)
            rows[calendar_date] = membership
    except BaoStockSessionCalendarAcquisitionError:
        raise
    except Exception:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.MALFORMED_ROW
        ) from None
    if not rows:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.EMPTY_RESPONSE
        )
    expected_count = (end_date - start_date).days + 1
    if len(rows) != expected_count:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.INCOMPLETE_RANGE
        )
    return tuple(
        MainlandSessionCalendarMembership(
            calendar_date=day,
            is_trading_day=rows[day],
        )
        for day in sorted(rows)
    )


def _provider_error_code(result: object) -> str:
    value = getattr(result, "error_code", None)
    if value is None:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_response_missing_error_code",
        )
    code = str(value).strip()
    if not code or len(code) > 32 or re.fullmatch(r"[A-Za-z0-9_.:-]+", code) is None:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code="baostock_response_error_code_invalid",
        )
    return code


def _transport_failure(exc: Exception, *, operation: str) -> PhysicalAttemptFailure:
    error_code = str(getattr(exc, "error_code", "") or "").strip()
    retry_after = _retry_after_seconds(exc)
    if error_code == "10001005":
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED,
            retryable=True,
            error_code="10001005",
            retry_after_seconds=(
                retry_after
                if retry_after is not None
                else _BAOSTOCK_LOGIN_CAPACITY_COOLDOWN_SECONDS
            ),
            rate_limit_scope=RateLimitScope.UPSTREAM,
        )
    if isinstance(exc, TimeoutError):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.TIMEOUT,
            retryable=True,
            error_code=f"baostock_{operation}_timeout",
        )
    if isinstance(exc, PermissionError):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code=f"baostock_{operation}_authentication",
        )
    if isinstance(exc, ConnectionError):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.DISCONNECT,
            retryable=True,
            error_code=f"baostock_{operation}_disconnect",
        )
    if isinstance(exc, (AttributeError, KeyError, TypeError, ValueError)):
        return PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.MALFORMED_RESPONSE,
            retryable=False,
            error_code=f"baostock_{operation}_malformed",
        )
    return PhysicalAttemptFailure(
        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
        retryable=False,
        error_code=f"baostock_{operation}_provider_error",
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    value = getattr(exc, "retry_after_seconds", None)
    try:
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


def _public_failure(
    exc: PhysicalAttemptBudgetExhausted,
) -> BaoStockSessionCalendarAcquisitionError:
    failure = exc.failure
    calendar_reason_by_code = {
        reason.value: reason
        for reason in (
            BaoStockSessionCalendarFailureReason.EMPTY_RESPONSE,
            BaoStockSessionCalendarFailureReason.MALFORMED_ROW,
            BaoStockSessionCalendarFailureReason.INVALID_TRADING_DAY,
            BaoStockSessionCalendarFailureReason.DUPLICATE_DATE,
            BaoStockSessionCalendarFailureReason.CONTRADICTORY_MEMBERSHIP,
            BaoStockSessionCalendarFailureReason.OUT_OF_RANGE_DATE,
            BaoStockSessionCalendarFailureReason.INCOMPLETE_RANGE,
        )
    }
    if failure.error_code in calendar_reason_by_code:
        reason = calendar_reason_by_code[failure.error_code]
    elif not exc.attempt_events and failure.outcome is PhysicalAttemptOutcome.RATE_LIMITED:
        reason = BaoStockSessionCalendarFailureReason.COOLDOWN_ACTIVE
    elif failure.outcome is PhysicalAttemptOutcome.UPSTREAM_BUSY:
        reason = BaoStockSessionCalendarFailureReason.UPSTREAM_BUSY
    elif failure.outcome is PhysicalAttemptOutcome.RATE_LIMITED:
        reason = BaoStockSessionCalendarFailureReason.RATE_LIMITED
    elif failure.outcome is PhysicalAttemptOutcome.ABANDONED:
        reason = BaoStockSessionCalendarFailureReason.INTERRUPTED
    elif exc.attempt_events:
        failed_event = next(
            (
                event
                for event in exc.attempt_events
                if event.outcome is not PhysicalAttemptOutcome.AVAILABLE
            ),
            exc.attempt_events[-1],
        )
        operation = failed_event.operation
        reason = {
            "baostock_login": BaoStockSessionCalendarFailureReason.LOGIN_FAILED,
            "query_trade_dates": BaoStockSessionCalendarFailureReason.QUERY_FAILED,
            "baostock_logout": BaoStockSessionCalendarFailureReason.LOGOUT_FAILED,
        }.get(operation, BaoStockSessionCalendarFailureReason.PROVIDER_ERROR)
    else:
        reason = BaoStockSessionCalendarFailureReason.PROVIDER_ERROR
    return BaoStockSessionCalendarAcquisitionError(
        reason,
        physical_outcome=failure.outcome,
        sequence_id=exc.sequence_id,
        attempt_events=exc.attempt_events,
        retryable=failure.retryable,
        retry_after_seconds=failure.retry_after_seconds,
    )


def _logical_request_id(
    request: BaoStockSessionCalendarRequest,
    *,
    start_date: date,
    end_date: date,
    upstream_service_id: str,
) -> str:
    payload = {
        "contract_version": BAOSTOCK_CALENDAR_REQUEST_VERSION,
        "provider": request.provider,
        "upstream_service_id": upstream_service_id,
        "account_scope": request.account_scope,
        "capacity_scope": request.capacity_scope,
        "endpoint": request.endpoint,
        "dataset_name": request.dataset_name,
        "reference_market": request.reference_market,
        "timezone_name": request.timezone_name,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "maximum_inclusive_range_days": request.maximum_inclusive_range_days,
        "data_usage_mode": request.data_usage_mode.value,
        "normalizer_version": request.normalizer_version,
        "payload_contract": request.payload_contract,
        "session_time_contract": request.session_time_contract,
    }
    return f"{BAOSTOCK_CALENDAR_REQUEST_VERSION}=sha256:{_digest(payload)}"


def _provider_dataset_id(upstream_service_id: str) -> str:
    digest = sha256()
    for component in (
        "provider-dataset",
        upstream_service_id,
        BAOSTOCK_CALENDAR_DATASET_NAME,
    ):
        encoded = component.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"provider-dataset=sha256:{digest.hexdigest()}"


def _content_identity(
    membership: tuple[MainlandSessionCalendarMembership, ...],
) -> str:
    payload = {
        "provider": "baostock",
        "dataset_name": BAOSTOCK_CALENDAR_DATASET_NAME,
        "reference_market": _REFERENCE_MARKET,
        "timezone_name": _TIMEZONE_NAME,
        "normalizer_version": BAOSTOCK_CALENDAR_NORMALIZER_VERSION,
        "payload_contract": BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT,
        "membership": [
            {
                "calendar_date": item.calendar_date.isoformat(),
                "is_trading_day": item.is_trading_day,
            }
            for item in membership
        ],
    }
    return f"calendar-acquisition-content=sha256:{_digest(payload)}"


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.PROVIDER_ERROR
        )
    return value.astimezone(timezone.utc)


__all__ = [
    "BAOSTOCK_CALENDAR_DATASET_NAME",
    "BAOSTOCK_CALENDAR_NORMALIZER_VERSION",
    "BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT",
    "MAINLAND_REGULAR_SESSION_TIME_CONTRACT",
    "BaoStockSessionCalendarAcquisition",
    "BaoStockSessionCalendarAcquisitionError",
    "BaoStockSessionCalendarAttempt",
    "BaoStockSessionCalendarFailureReason",
    "BaoStockSessionCalendarRequest",
    "BaoStockSessionCalendarTerminalOutcome",
    "BaoStockSessionCalendarTransport",
    "MainlandSessionCalendarMembership",
    "acquire_mainland_session_calendar",
]

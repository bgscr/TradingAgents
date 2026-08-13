from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import Future
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from threading import Lock
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.dataflows.baostock_calendar import (
    BAOSTOCK_CALENDAR_CAPACITY_SCOPE,
    BAOSTOCK_CALENDAR_DATASET_NAME,
    BAOSTOCK_CALENDAR_NORMALIZER_VERSION,
    BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT,
    MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
    BaoStockSessionCalendarAttempt,
    BaoStockSessionCalendarRequest,
    acquire_mainland_session_calendar,
)
from tradingagents.market_history.calendar import (
    MAINLAND_SESSION_CALENDAR_PROVENANCE,
    MAINLAND_SESSION_CALENDAR_READINESS_CONTRACT,
    MainlandSessionCalendarReadiness,
    MainlandSessionCalendarReadinessError,
    MarketSessionCalendarIntegrityError,
    MarketSessionCalendarReadFailure,
    MarketSessionStatus,
    StoredSessionCalendar,
    publication_from_baostock_session_calendar_acquisition,
)
from tradingagents.market_history.config import (
    DataUsageMode,
    MarketHistoryConfig,
    payload_mutation_sidecar_path,
)
from tradingagents.market_history.coordinator import (
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.result_codec import decode_coordinated_result
from tradingagents.market_history.store import (
    MarketHistoryStore,
    PayloadMutationTimeoutError,
)

MARKET_SESSION_CALENDAR_BOOTSTRAP_REQUEST_CONTRACT = (
    "market-session-calendar-bootstrap-request-v1"
)
MARKET_SESSION_CALENDAR_BOOTSTRAP_RECEIPT_CONTRACT = (
    "market-session-calendar-bootstrap-receipt-v1"
)
_REFERENCE_MARKET = "mainland-cn"
_TIMEZONE_NAME = "Asia/Shanghai"
_PROVIDER = "baostock"
_ACCOUNT_SCOPE = "not-applicable"
_MAX_SAFE_INCLUSIVE_RANGE_DAYS = 732
_SAFE_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SAFE_PUBLIC_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:=+/-]{0,127}")
_SAFE_DIGEST = re.compile(r"[0-9a-f]{64}")
_SECRET_MARKERS = ("credential", "password", "secret", "token", "api_key")
_UPSTREAM_SERVICE_ID, _UPSTREAM_SERVICE_NAME = (
    upstream_service_identity_for_provider(_PROVIDER)
)
_CLOSED_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)
_STORE_OPEN_LOCK = Lock()
_BOOTSTRAP_SINGLE_FLIGHT_LOCK = Lock()
_BOOTSTRAP_SINGLE_FLIGHTS: dict[
    tuple[object, ...], Future[MarketSessionCalendarBootstrapReceipt]
] = {}


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Production wall clock kept injectable at the application-service seam."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class MarketSessionCalendarBootstrapDisposition(str, Enum):
    CREATED = "created"
    REUSED = "reused"
    REFRESHED_SAME_REVISION = "refreshed_same_revision"


class MarketSessionCalendarBootstrapFailureReason(str, Enum):
    INVALID_EVALUATED_AT = "calendar_evaluated_at_invalid"
    UNSUPPORTED_READINESS_CONTRACT = "calendar_readiness_contract_unsupported"


class MarketSessionCalendarBootstrapError(RuntimeError):
    """Closed safe failure owned by the CAL-03 application boundary."""

    def __init__(self, reason: MarketSessionCalendarBootstrapFailureReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


def _safe_public_text(value: str) -> str:
    folded = value.casefold()
    if (
        _SAFE_PUBLIC_TEXT.fullmatch(value) is None
        or "://" in value
        or value.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[/\\]", value) is not None
        or any(marker in folded for marker in _SECRET_MARKERS)
    ):
        raise ValueError("unsafe calendar bootstrap text")
    return value


def _calendar_provider_dataset_id(upstream_service_id: str) -> str:
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


class MarketSessionCalendarBootstrapRequest(BaseModel):
    """Closed material and operational inputs for explicit calendar bootstrap."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["market-session-calendar-bootstrap-request-v1"] = (
        MARKET_SESSION_CALENDAR_BOOTSTRAP_REQUEST_CONTRACT
    )
    provider: str = _PROVIDER
    reference_market: str = _REFERENCE_MARKET
    timezone_name: str = _TIMEZONE_NAME
    start_date: date | None = None
    end_date: date | None = None
    evaluated_at: datetime
    data_usage_mode: DataUsageMode
    account_scope: str = _ACCOUNT_SCOPE
    capacity_scope: str = BAOSTOCK_CALENDAR_CAPACITY_SCOPE
    authorize_provider_io: bool = Field(
        default=False,
        exclude=True,
        repr=False,
        strict=True,
    )
    force_refresh: bool = Field(default=False, strict=True)
    dataset_name: str = BAOSTOCK_CALENDAR_DATASET_NAME
    normalizer_version: str = BAOSTOCK_CALENDAR_NORMALIZER_VERSION
    payload_contract: str = BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT
    readiness_contract: str = MAINLAND_SESSION_CALENDAR_READINESS_CONTRACT
    session_time_contract: str = MAINLAND_REGULAR_SESSION_TIME_CONTRACT
    maximum_inclusive_range_days: int = Field(
        default=_MAX_SAFE_INCLUSIVE_RANGE_DAYS,
        ge=1,
        le=_MAX_SAFE_INCLUSIVE_RANGE_DAYS,
        strict=True,
    )

    @field_validator(
        "provider",
        "reference_market",
        "timezone_name",
        "capacity_scope",
        "dataset_name",
        "normalizer_version",
        "payload_contract",
        "readiness_contract",
        "session_time_contract",
    )
    @classmethod
    def _validate_safe_material_text(cls, value: str) -> str:
        return _safe_public_text(value)

    @field_validator("account_scope")
    @classmethod
    def _validate_safe_account_scope(cls, value: str) -> str:
        try:
            return _safe_public_text(value)
        except ValueError:
            from tradingagents.dataflows.baostock_calendar import (
                BaoStockSessionCalendarAcquisitionError,
                BaoStockSessionCalendarFailureReason,
            )

            raise BaoStockSessionCalendarAcquisitionError(
                BaoStockSessionCalendarFailureReason.UNSAFE_ACCOUNT_SCOPE
            ) from None


class MarketSessionCalendarBootstrapReceipt(BaseModel):
    """Closed deterministic safe projection of one completed bootstrap."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["market-session-calendar-bootstrap-receipt-v1"] = (
        MARKET_SESSION_CALENDAR_BOOTSTRAP_RECEIPT_CONTRACT
    )
    provider: Literal["baostock"]
    endpoint: Literal["query_trade_dates"]
    upstream_service_id: str = Field(
        pattern=r"^upstream-service=sha256:[0-9a-f]{64}$",
    )
    upstream_service_name: Literal["BaoStock TCP service"]
    provider_dataset_id: str = Field(
        pattern=r"^provider-dataset=sha256:[0-9a-f]{64}$",
    )
    dataset_name: Literal["mainland-session-calendar-v1"]
    reference_market: Literal["mainland-cn"]
    timezone_name: Literal["Asia/Shanghai"]
    provenance: Literal["observed_point_in_time"]
    requested_start_date: date
    requested_end_date: date
    evaluated_at: datetime
    calendar_revision_id: str = Field(
        pattern=r"^market-session-calendar=sha256:[0-9a-f]{64}$",
    )
    calendar_refresh_ingestion_id: str = Field(
        pattern=r"^ingestion-run=sha256:[0-9a-f]{64}$",
    )
    retrieved_at: datetime | None
    observed_at: datetime
    published_at: datetime
    canonical_content_digest: str
    normalizer_version: Literal["baostock-session-calendar-normalizer-v1"]
    payload_contract: Literal["mainland-session-calendar-payload-v1"]
    readiness_contract: Literal["mainland-session-calendar-readiness-v1"]
    session_time_contract: Literal["mainland-cn-regular-session-times-v1"]
    total_session_count: int = Field(ge=1, le=1_000_000, strict=True)
    open_session_count: int = Field(ge=0, le=1_000_000, strict=True)
    closed_session_count: int = Field(ge=0, le=1_000_000, strict=True)
    latest_completed_session: date
    latest_completed_session_close: datetime
    next_open_session: date
    next_open_session_close: datetime
    logical_request_id: str | None = Field(
        pattern=r"^market-session-calendar-acquisition-request-v1=sha256:[0-9a-f]{64}$",
    )
    workflow_sequence_id: str | None = Field(
        pattern=r"^provider-request-sequence=sha256:[0-9a-f]{64}$",
    )
    physical_attempts: tuple[BaoStockSessionCalendarAttempt, ...]
    logical_request_count: Literal[0, 1]
    physical_attempt_count: Literal[0, 3]
    provider_io_performed: bool
    disposition: MarketSessionCalendarBootstrapDisposition

    @field_validator(
        "provider",
        "endpoint",
        "upstream_service_id",
        "upstream_service_name",
        "provider_dataset_id",
        "dataset_name",
        "reference_market",
        "timezone_name",
        "provenance",
        "calendar_revision_id",
        "calendar_refresh_ingestion_id",
        "normalizer_version",
        "payload_contract",
        "readiness_contract",
        "session_time_contract",
    )
    @classmethod
    def _validate_safe_receipt_text(cls, value: str) -> str:
        return _safe_public_text(value)

    @field_validator("logical_request_id", "workflow_sequence_id")
    @classmethod
    def _validate_optional_safe_receipt_text(cls, value: str | None) -> str | None:
        return None if value is None else _safe_public_text(value)

    @field_validator("canonical_content_digest")
    @classmethod
    def _validate_content_digest(cls, value: str) -> str:
        if _SAFE_DIGEST.fullmatch(value) is None:
            raise ValueError("calendar content digest is invalid")
        return value

    @model_validator(mode="after")
    def _validate_safe_projection(self) -> MarketSessionCalendarBootstrapReceipt:
        timestamps = (
            self.evaluated_at,
            self.retrieved_at,
            self.observed_at,
            self.published_at,
            self.latest_completed_session_close,
            self.next_open_session_close,
        )
        if any(value is not None and value.tzinfo is None for value in timestamps):
            raise ValueError("calendar receipt timestamps must be timezone-aware")
        if (
            self.upstream_service_id != _UPSTREAM_SERVICE_ID
            or self.upstream_service_name != _UPSTREAM_SERVICE_NAME
            or self.provider_dataset_id
            != _calendar_provider_dataset_id(_UPSTREAM_SERVICE_ID)
            or self.requested_start_date > self.requested_end_date
            or self.latest_completed_session >= self.next_open_session
            or self.total_session_count
            != self.open_session_count + self.closed_session_count
            or self.published_at < self.observed_at
            or self.physical_attempt_count != len(self.physical_attempts)
        ):
            raise ValueError("calendar receipt projection is inconsistent")
        if self.provider_io_performed:
            if (
                self.disposition is MarketSessionCalendarBootstrapDisposition.REUSED
                or self.retrieved_at is None
                or self.logical_request_id is None
                or self.workflow_sequence_id is None
                or self.logical_request_count != 1
                or self.physical_attempt_count != 3
            ):
                raise ValueError("provider-work receipt accounting is inconsistent")
        elif (
            self.disposition is not MarketSessionCalendarBootstrapDisposition.REUSED
            or self.retrieved_at is not None
            or self.logical_request_id is not None
            or self.workflow_sequence_id is not None
            or self.logical_request_count != 0
            or self.physical_attempt_count != 0
            or self.physical_attempts
        ):
            raise ValueError("persisted-reuse receipt accounting is inconsistent")
        for attempt in self.physical_attempts:
            for value in (
                attempt.attempt_event_id,
                attempt.sequence_id,
                attempt.request_key,
                attempt.upstream_service_id,
                attempt.upstream_service_name,
                attempt.capacity_scope,
                attempt.operation,
                attempt.pacing_event,
            ):
                _safe_public_text(value)
            if attempt.error_code is not None:
                _safe_public_text(attempt.error_code)
        return self


class _PersistedAcquisitionAudit(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    retrieved_at: datetime
    observed_at: datetime
    logical_request_id: str
    workflow_sequence_id: str
    physical_attempts: tuple[BaoStockSessionCalendarAttempt, ...]


@dataclass
class _BootstrapMutationLease:
    concurrent_refresh_ingestion_id: str | None
    completed_refresh_ingestion_id: str | None = None

    def complete(self, ingestion_run_id: str) -> None:
        self.completed_refresh_ingestion_id = ingestion_run_id


def bootstrap_mainland_session_calendar(
    request: MarketSessionCalendarBootstrapRequest,
    *,
    history_config: MarketHistoryConfig,
    clock: Clock,
) -> MarketSessionCalendarBootstrapReceipt:
    """Reuse or explicitly acquire and publish one ready mainland calendar."""

    _validate_request(request, history_config=history_config)
    flight_key = _bootstrap_flight_key(request, history_config=history_config)
    with _BOOTSTRAP_SINGLE_FLIGHT_LOCK:
        shared = _BOOTSTRAP_SINGLE_FLIGHTS.get(flight_key)
        if shared is None:
            shared = Future()
            _BOOTSTRAP_SINGLE_FLIGHTS[flight_key] = shared
            leader = True
        else:
            leader = False
    if not leader:
        return shared.result()

    try:
        receipt = _bootstrap_mainland_session_calendar(
            request,
            history_config=history_config,
            clock=clock,
        )
    except BaseException as exc:
        shared.set_exception(exc)
        raise
    else:
        shared.set_result(receipt)
        return receipt
    finally:
        with _BOOTSTRAP_SINGLE_FLIGHT_LOCK:
            if _BOOTSTRAP_SINGLE_FLIGHTS.get(flight_key) is shared:
                del _BOOTSTRAP_SINGLE_FLIGHTS[flight_key]


def _bootstrap_mainland_session_calendar(
    request: MarketSessionCalendarBootstrapRequest,
    *,
    history_config: MarketHistoryConfig,
    clock: Clock,
) -> MarketSessionCalendarBootstrapReceipt:

    assert request.start_date is not None
    assert request.end_date is not None
    evaluated_at = request.evaluated_at.astimezone(timezone.utc)

    with _open_history_store(history_config) as store:
        initial_revision_ids, initial_refresh_ids = _initial_bootstrap_state(
            store,
            reference_market=request.reference_market,
        )
        if not request.force_refresh:
            ready = _ready_current_revision(
                store,
                request=request,
                evaluated_at=evaluated_at,
            )
            if ready is not None:
                return _reused_receipt(
                    store,
                    request=request,
                    calendar=ready[0],
                    readiness=ready[1],
                )

    if not request.authorize_provider_io:
        from tradingagents.dataflows.baostock_calendar import (
            BaoStockSessionCalendarAcquisitionError,
            BaoStockSessionCalendarFailureReason,
        )

        raise BaoStockSessionCalendarAcquisitionError(
            BaoStockSessionCalendarFailureReason.OPERATOR_AUTHORIZATION_REQUIRED
        )

    with (
        _open_history_store(history_config) as store,
        # This existing cross-process mutex closes the CAL-01-complete/CAL-02-
        # publish gap. A follower rechecks durable readiness after waiting and
        # therefore cannot start a second ordinary provider workflow.
        _calendar_bootstrap_mutation_scope(
            store,
            "calendar_bootstrap",
            _calendar_bootstrap_operation_subject(request),
        ) as mutation_lease,
    ):
        ready = _ready_current_revision(
            store,
            request=request,
            evaluated_at=evaluated_at,
        )
        if ready is not None:
            calendar, readiness = ready
            handoff_refresh_id = (
                _matching_refresh_since(
                    store,
                    request=request,
                    calendar=calendar,
                    initial_refresh_ids=initial_refresh_ids,
                    concurrent_refresh_ingestion_id=(
                        mutation_lease.concurrent_refresh_ingestion_id
                    ),
                )
                if request.force_refresh
                else calendar.ingestion_run_id
            )
            if not request.force_refresh or handoff_refresh_id is not None:
                assert handoff_refresh_id is not None
                published_at = _calendar_publication_timestamp(
                    store,
                    handoff_refresh_id,
                )
                audit = _persisted_acquisition_handoff(
                    request,
                    history_config=history_config,
                    calendar=calendar,
                    expected_observed_at=published_at,
                )
                return _reused_receipt(
                    store,
                    request=request,
                    calendar=calendar,
                    readiness=readiness,
                    acquisition_audit=audit,
                    calendar_refresh_ingestion_id=handoff_refresh_id,
                    disposition=(
                        MarketSessionCalendarBootstrapDisposition.REFRESHED_SAME_REVISION
                        if request.force_refresh
                        and calendar.calendar_revision_id in initial_revision_ids
                        else MarketSessionCalendarBootstrapDisposition.CREATED
                    ),
                )

        acquisition = _acquire_calendar(
            request,
            history_config=history_config,
            clock=clock,
        )
        published = store.publish_session_calendar(
            publication_from_baostock_session_calendar_acquisition(acquisition)
        )
        mutation_lease.complete(published.ingestion_run_id)
        published_at = _calendar_publication_timestamp(
            store,
            published.ingestion_run_id,
        )

    # Verification deliberately happens after the mutation mutex is released:
    # it remains exact-ID safe even if a concurrent publisher changes current.
    with _open_history_store(history_config) as store:
        calendar = store.read_session_calendar_revision(
            published.calendar_revision_id
        )
        readiness = store.assess_mainland_session_calendar_readiness(
            published.calendar_revision_id,
            requested_start_date=request.start_date,
            requested_end_date=request.end_date,
            evaluated_at=evaluated_at,
        )

    disposition = (
        MarketSessionCalendarBootstrapDisposition.REFRESHED_SAME_REVISION
        if request.force_refresh
        and published.calendar_revision_id in initial_revision_ids
        else MarketSessionCalendarBootstrapDisposition.CREATED
    )
    return _receipt(
        request=request,
        calendar=calendar,
        readiness=readiness,
        retrieved_at=acquisition.retrieved_at,
        observed_at=acquisition.observed_at,
        published_at=published_at,
        logical_request_id=acquisition.logical_request_id,
        workflow_sequence_id=acquisition.workflow_sequence_id,
        physical_attempts=acquisition.attempt_events,
        provider_io_performed=True,
        disposition=disposition,
        calendar_refresh_ingestion_id=published.ingestion_run_id,
    )


def _validate_request(
    request: MarketSessionCalendarBootstrapRequest,
    *,
    history_config: MarketHistoryConfig,
) -> None:
    from tradingagents.dataflows.baostock_calendar import (
        BaoStockSessionCalendarAcquisitionError,
        BaoStockSessionCalendarFailureReason,
    )

    checks = (
        (
            request.provider == _PROVIDER,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_PROVIDER,
        ),
        (
            request.reference_market == _REFERENCE_MARKET,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_REFERENCE_MARKET,
        ),
        (
            request.timezone_name == _TIMEZONE_NAME,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_TIMEZONE,
        ),
        (
            request.data_usage_mode is DataUsageMode.PERSONAL_RESEARCH
            and request.data_usage_mode is history_config.data_usage_mode,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_DATA_USAGE_MODE,
        ),
        (
            request.account_scope == _ACCOUNT_SCOPE
            and _SAFE_SCOPE.fullmatch(request.account_scope) is not None,
            BaoStockSessionCalendarFailureReason.UNSAFE_ACCOUNT_SCOPE,
        ),
        (
            request.capacity_scope == BAOSTOCK_CALENDAR_CAPACITY_SCOPE,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_CAPACITY_SCOPE,
        ),
        (
            request.dataset_name == BAOSTOCK_CALENDAR_DATASET_NAME,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_DATASET,
        ),
        (
            request.normalizer_version == BAOSTOCK_CALENDAR_NORMALIZER_VERSION,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_NORMALIZER,
        ),
        (
            request.payload_contract == BAOSTOCK_CALENDAR_PAYLOAD_CONTRACT,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_PAYLOAD_CONTRACT,
        ),
        (
            request.session_time_contract == MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
            BaoStockSessionCalendarFailureReason.UNSUPPORTED_SESSION_TIME_CONTRACT,
        ),
    )
    for valid, reason in checks:
        if not valid:
            raise BaoStockSessionCalendarAcquisitionError(reason)
    if request.readiness_contract != MAINLAND_SESSION_CALENDAR_READINESS_CONTRACT:
        raise MarketSessionCalendarBootstrapError(
            MarketSessionCalendarBootstrapFailureReason.UNSUPPORTED_READINESS_CONTRACT
        )
    if (
        request.evaluated_at.tzinfo is None
        or request.evaluated_at.utcoffset() is None
    ):
        raise MarketSessionCalendarBootstrapError(
            MarketSessionCalendarBootstrapFailureReason.INVALID_EVALUATED_AT
        )
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


def _open_history_store(history_config: MarketHistoryConfig) -> MarketHistoryStore:
    with _STORE_OPEN_LOCK:
        return MarketHistoryStore.open(history_config)


def _bootstrap_flight_key(
    request: MarketSessionCalendarBootstrapRequest,
    *,
    history_config: MarketHistoryConfig,
) -> tuple[object, ...]:
    return (
        str(history_config.database_path),
        request.contract_version,
        request.provider,
        request.reference_market,
        request.timezone_name,
        request.start_date,
        request.end_date,
        request.evaluated_at.isoformat(),
        request.data_usage_mode.value,
        request.account_scope,
        request.capacity_scope,
        request.authorize_provider_io,
        request.force_refresh,
        request.dataset_name,
        request.normalizer_version,
        request.payload_contract,
        request.readiness_contract,
        request.session_time_contract,
        request.maximum_inclusive_range_days,
    )


def _open_provider_request_authority(
    history_config: MarketHistoryConfig,
) -> MarketHistoryStore:
    with _STORE_OPEN_LOCK:
        return MarketHistoryStore.open_provider_request_authority(history_config)


def _acquire_calendar(
    request: MarketSessionCalendarBootstrapRequest,
    *,
    history_config: MarketHistoryConfig,
    clock: Clock,
):
    with _open_provider_request_authority(history_config) as authority:
        coordinator = ProviderRequestCoordinator(authority)
        upstream_id, upstream_name = upstream_service_identity_for_provider(_PROVIDER)
        coordinator.register_upstream_service(upstream_id, upstream_name)
        owner_id = _calendar_acquisition_owner_id(authority)
        try:
            acquisition = acquire_mainland_session_calendar(
                _acquisition_request(request),
                coordinator=coordinator,
                owner_id=owner_id,
                now=clock.now,
                sleep=clock.sleep,
                lease_duration=timedelta(seconds=30),
            )
        except BaseException:
            with suppress(BaseException):
                coordinator.close()
            raise
        coordinator.close()
        return acquisition


def _calendar_bootstrap_operation_subject(
    request: MarketSessionCalendarBootstrapRequest,
) -> str:
    material = "|".join(
        (
            request.provider,
            request.reference_market,
            request.start_date.isoformat() if request.start_date is not None else "",
            request.end_date.isoformat() if request.end_date is not None else "",
            request.evaluated_at.astimezone(timezone.utc).isoformat(),
            request.normalizer_version,
            request.payload_contract,
            request.readiness_contract,
            request.session_time_contract,
        )
    )
    return sha256(material.encode("utf-8")).hexdigest()


@contextmanager
def _calendar_bootstrap_mutation_scope(
    store: MarketHistoryStore,
    operation: str,
    operation_subject: str,
) -> Iterator[_BootstrapMutationLease]:
    active_refresh_id = _bootstrap_mutex_completed_refresh_id(
        store,
        operation_subject,
    )
    while True:
        scope = store._payload_mutation_scope(operation, operation_subject)
        try:
            scope.__enter__()
        except PayloadMutationTimeoutError:
            # Bootstrap is an explicit maintenance operation. Keep waiting for
            # the durable owner instead of timing out into a duplicate workflow.
            continue
        try:
            acquired_refresh_id = _bootstrap_mutex_completed_refresh_id(
                store,
                operation_subject,
            )
            lease = _BootstrapMutationLease(
                acquired_refresh_id
                if acquired_refresh_id is not None
                and acquired_refresh_id != active_refresh_id
                else None
            )
            try:
                yield lease
            finally:
                if lease.completed_refresh_ingestion_id is not None:
                    _write_bootstrap_mutex_completed_refresh(
                        store,
                        operation_subject=operation_subject,
                        ingestion_run_id=lease.completed_refresh_ingestion_id,
                    )
        finally:
            scope.__exit__(*sys.exc_info())
        return


def _bootstrap_mutex_completed_refresh_id(
    store: MarketHistoryStore,
    operation_subject: str,
) -> str | None:
    connection = sqlite3.connect(
        payload_mutation_sidecar_path(store.config.database_path),
        isolation_level=None,
        timeout=0.0,
    )
    try:
        row = connection.execute(
            "SELECT ingestion_run_id FROM calendar_bootstrap_handoffs "
            "WHERE operation_subject = ?",
            (operation_subject,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return str(row[0]) if row is not None else None


def _write_bootstrap_mutex_completed_refresh(
    store: MarketHistoryStore,
    *,
    operation_subject: str,
    ingestion_run_id: str,
) -> None:
    connection = store._payload_mutation_connection
    assert connection is not None
    connection.execute(
        "CREATE TABLE IF NOT EXISTS calendar_bootstrap_handoffs ("
        "operation_subject TEXT PRIMARY KEY, ingestion_run_id TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO calendar_bootstrap_handoffs "
        "(operation_subject, ingestion_run_id) VALUES (?, ?) "
        "ON CONFLICT(operation_subject) DO UPDATE SET "
        "ingestion_run_id = excluded.ingestion_run_id",
        (operation_subject, ingestion_run_id),
    )
    # Publishing is complete. Commit the safe handoff and release the existing
    # sidecar mutex atomically; the enclosing store scope's rollback is then a no-op.
    connection.commit()


def _persisted_acquisition_handoff(
    request: MarketSessionCalendarBootstrapRequest,
    *,
    history_config: MarketHistoryConfig,
    calendar: StoredSessionCalendar,
    expected_observed_at: datetime | None = None,
) -> _PersistedAcquisitionAudit | None:
    upstream_id, _ = upstream_service_identity_for_provider(_PROVIDER)
    expected_logical_request_id = _calendar_acquisition_logical_request_id(request)
    with _open_provider_request_authority(history_config) as authority:
        rows = tuple(
            authority._connection.execute(
            "SELECT request_key, sequence_id, result_payload, result_sha256 "
            "FROM provider_request_sequences WHERE request_key LIKE "
            "'market-session-calendar-acquisition-request-v1=%' "
            "AND upstream_service_id = ? AND status = 'succeeded' "
            "ORDER BY completed_at DESC, sequence_id DESC",
            (upstream_id,),
            )
        )
        coordinator = ProviderRequestCoordinator(authority)
        expected_membership = tuple(
            (
                session.session_date,
                session.status is MarketSessionStatus.OPEN,
            )
            for session in calendar.sessions
        )
        for logical_request_id, sequence_id, raw_payload, result_digest in rows:
            if str(logical_request_id) != expected_logical_request_id:
                continue
            payload_bytes = bytes(raw_payload)
            if sha256(payload_bytes).hexdigest() != str(result_digest):
                continue
            payload = decode_coordinated_result(payload_bytes)
            if not isinstance(payload, Mapping):
                continue
            try:
                observed_at = payload["observed_at"]
                retrieved_at = payload["retrieved_at"]
                membership = tuple(payload["membership"])
                if (
                    not isinstance(observed_at, datetime)
                    or not isinstance(retrieved_at, datetime)
                    or observed_at.astimezone(timezone.utc)
                    != (expected_observed_at or calendar.observed_at)
                    or membership != expected_membership
                    or membership[0][0] != request.start_date
                    or membership[-1][0] != request.end_date
                ):
                    continue
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            attempts = tuple(
                BaoStockSessionCalendarAttempt.from_coordinator(event)
                for event in coordinator.physical_attempt_events(str(sequence_id))
            )
            if len(attempts) == 3:
                return _PersistedAcquisitionAudit(
                    retrieved_at=retrieved_at,
                    observed_at=observed_at,
                    logical_request_id=str(logical_request_id),
                    workflow_sequence_id=str(sequence_id),
                    physical_attempts=attempts,
                )
        return None


def _calendar_acquisition_logical_request_id(
    request: MarketSessionCalendarBootstrapRequest,
) -> str:
    acquisition_request = _acquisition_request(request)
    assert acquisition_request.start_date is not None
    assert acquisition_request.end_date is not None
    payload = {
        "contract_version": acquisition_request.contract_version,
        "provider": acquisition_request.provider,
        "upstream_service_id": _UPSTREAM_SERVICE_ID,
        "account_scope": acquisition_request.account_scope,
        "capacity_scope": acquisition_request.capacity_scope,
        "endpoint": acquisition_request.endpoint,
        "dataset_name": acquisition_request.dataset_name,
        "reference_market": acquisition_request.reference_market,
        "timezone_name": acquisition_request.timezone_name,
        "start_date": acquisition_request.start_date.isoformat(),
        "end_date": acquisition_request.end_date.isoformat(),
        "maximum_inclusive_range_days": (
            acquisition_request.maximum_inclusive_range_days
        ),
        "data_usage_mode": acquisition_request.data_usage_mode.value,
        "normalizer_version": acquisition_request.normalizer_version,
        "payload_contract": acquisition_request.payload_contract,
        "session_time_contract": acquisition_request.session_time_contract,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{acquisition_request.contract_version}=sha256:{sha256(encoded).hexdigest()}"


def _initial_bootstrap_state(
    store: MarketHistoryStore,
    *,
    reference_market: str,
) -> tuple[frozenset[str], frozenset[str]]:
    revision_ids = frozenset(
        str(row[0])
        for row in store._connection.execute(
            "SELECT calendar_revision_id FROM market_session_calendars "
            "WHERE reference_market = ?",
            (reference_market,),
        )
    )
    refresh_ids = frozenset(
        str(row[0])
        for row in store._connection.execute(
            "SELECT run_id FROM ingestion_runs WHERE operation = 'calendar_refresh' "
            "AND status = 'published'"
        )
    )
    return revision_ids, refresh_ids


def _matching_refresh_since(
    store: MarketHistoryStore,
    *,
    request: MarketSessionCalendarBootstrapRequest,
    calendar: StoredSessionCalendar,
    initial_refresh_ids: frozenset[str],
    concurrent_refresh_ingestion_id: str | None,
) -> str | None:
    rows = store._connection.execute(
        "SELECT run_id FROM ingestion_runs WHERE operation = 'calendar_refresh' "
        "AND status = 'published' AND provider_dataset_id = ? "
        "AND requested_start = ? AND requested_end = ? AND payload_digest = ? "
        "ORDER BY completed_at DESC, run_id DESC",
        (
            calendar.provider_dataset_id,
            request.start_date.isoformat() if request.start_date is not None else "",
            request.end_date.isoformat() if request.end_date is not None else "",
            calendar.payload_digest,
        ),
    )
    matching_ids = tuple(str(row[0]) for row in rows)
    if concurrent_refresh_ingestion_id in matching_ids:
        return concurrent_refresh_ingestion_id
    return next(
        (
            ingestion_run_id
            for ingestion_run_id in matching_ids
            if ingestion_run_id not in initial_refresh_ids
        ),
        None,
    )


def _calendar_publication_timestamp(
    store: MarketHistoryStore,
    ingestion_run_id: str,
) -> datetime:
    row = store._connection.execute(
        "SELECT completed_at FROM ingestion_runs WHERE run_id = ? "
        "AND operation = 'calendar_refresh' AND status = 'published'",
        (ingestion_run_id,),
    ).fetchone()
    if row is None:
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
        )
    try:
        published_at = datetime.fromisoformat(str(row[0]))
        if published_at.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
        ) from None
    return published_at.astimezone(timezone.utc)


def _reused_receipt(
    store: MarketHistoryStore,
    *,
    request: MarketSessionCalendarBootstrapRequest,
    calendar: StoredSessionCalendar,
    readiness: MainlandSessionCalendarReadiness,
    acquisition_audit: _PersistedAcquisitionAudit | None = None,
    calendar_refresh_ingestion_id: str | None = None,
    disposition: MarketSessionCalendarBootstrapDisposition | None = None,
) -> MarketSessionCalendarBootstrapReceipt:
    handed_off = acquisition_audit is not None
    return _receipt(
        request=request,
        calendar=calendar,
        readiness=readiness,
        retrieved_at=(acquisition_audit.retrieved_at if handed_off else None),
        observed_at=(
            acquisition_audit.observed_at if handed_off else calendar.observed_at
        ),
        published_at=_calendar_publication_timestamp(
            store,
            calendar_refresh_ingestion_id or calendar.ingestion_run_id,
        ),
        logical_request_id=(
            acquisition_audit.logical_request_id if handed_off else None
        ),
        workflow_sequence_id=(
            acquisition_audit.workflow_sequence_id if handed_off else None
        ),
        physical_attempts=(
            acquisition_audit.physical_attempts if handed_off else ()
        ),
        provider_io_performed=handed_off,
        disposition=disposition
        or (
            MarketSessionCalendarBootstrapDisposition.CREATED
            if handed_off
            else MarketSessionCalendarBootstrapDisposition.REUSED
        ),
        calendar_refresh_ingestion_id=calendar_refresh_ingestion_id,
    )


def _calendar_acquisition_owner_id(authority: MarketHistoryStore) -> str:
    row = authority._connection.execute(
        "SELECT COUNT(*) FROM provider_request_sequences "
        "WHERE request_key LIKE 'market-session-calendar-acquisition-request-v1=%'"
    ).fetchone()
    sequence_ordinal = int(row[0]) + 1 if row is not None else 1
    return f"market-session-calendar-bootstrap-{sequence_ordinal}"


def _acquisition_request(
    request: MarketSessionCalendarBootstrapRequest,
) -> BaoStockSessionCalendarRequest:
    return BaoStockSessionCalendarRequest(
        provider=request.provider,
        reference_market=request.reference_market,
        timezone_name=request.timezone_name,
        start_date=request.start_date,
        end_date=request.end_date,
        maximum_inclusive_range_days=request.maximum_inclusive_range_days,
        data_usage_mode=request.data_usage_mode,
        account_scope=request.account_scope,
        capacity_scope=request.capacity_scope,
        dataset_name=request.dataset_name,
        normalizer_version=request.normalizer_version,
        payload_contract=request.payload_contract,
        session_time_contract=request.session_time_contract,
        operator_provider_io_authorized=request.authorize_provider_io,
    )


def _ready_current_revision(
    store: MarketHistoryStore,
    *,
    request: MarketSessionCalendarBootstrapRequest,
    evaluated_at: datetime,
) -> tuple[StoredSessionCalendar, MainlandSessionCalendarReadiness] | None:
    revision_ids = tuple(
        str(row[0])
        for row in store._connection.execute(
            "SELECT calendar_revision_id FROM market_session_calendars "
            "WHERE reference_market = ? "
            "ORDER BY observed_at DESC, calendar_revision_id DESC",
            (request.reference_market,),
        )
    )
    for revision_id in revision_ids:
        try:
            calendar = store.read_session_calendar_revision(revision_id)
            readiness = store.assess_mainland_session_calendar_readiness(
                revision_id,
                requested_start_date=request.start_date,
                requested_end_date=request.end_date,
                evaluated_at=evaluated_at,
            )
        except (
            MainlandSessionCalendarReadinessError,
            MarketSessionCalendarIntegrityError,
        ):
            continue
        return calendar, readiness
    return None


def _receipt(
    *,
    request: MarketSessionCalendarBootstrapRequest,
    calendar: StoredSessionCalendar,
    readiness: MainlandSessionCalendarReadiness,
    retrieved_at: datetime | None,
    observed_at: datetime,
    published_at: datetime,
    logical_request_id: str | None,
    workflow_sequence_id: str | None,
    physical_attempts: tuple[BaoStockSessionCalendarAttempt, ...],
    provider_io_performed: bool,
    disposition: MarketSessionCalendarBootstrapDisposition,
    calendar_refresh_ingestion_id: str | None = None,
) -> MarketSessionCalendarBootstrapReceipt:
    open_count = sum(
        session.status is MarketSessionStatus.OPEN for session in calendar.sessions
    )
    return MarketSessionCalendarBootstrapReceipt(
        provider=calendar.provider_name,
        endpoint=calendar.endpoint,
        upstream_service_id=calendar.upstream_service_id,
        upstream_service_name=calendar.upstream_service_name,
        provider_dataset_id=calendar.provider_dataset_id,
        dataset_name=calendar.dataset_name,
        reference_market=calendar.reference_market,
        timezone_name=calendar.timezone_name,
        provenance=MAINLAND_SESSION_CALENDAR_PROVENANCE,
        requested_start_date=request.start_date,
        requested_end_date=request.end_date,
        evaluated_at=readiness.evaluated_at,
        calendar_revision_id=calendar.calendar_revision_id,
        calendar_refresh_ingestion_id=(
            calendar_refresh_ingestion_id or calendar.ingestion_run_id
        ),
        retrieved_at=retrieved_at,
        observed_at=observed_at,
        published_at=published_at,
        canonical_content_digest=calendar.payload_digest,
        normalizer_version=calendar.normalizer_version,
        payload_contract=calendar.payload_contract,
        readiness_contract=readiness.contract_version,
        session_time_contract=calendar.session_time_contract,
        total_session_count=calendar.session_count,
        open_session_count=open_count,
        closed_session_count=calendar.session_count - open_count,
        latest_completed_session=readiness.latest_completed_session,
        latest_completed_session_close=readiness.latest_completed_session_close,
        next_open_session=readiness.next_open_session,
        next_open_session_close=readiness.next_open_session_close,
        logical_request_id=logical_request_id,
        workflow_sequence_id=workflow_sequence_id,
        physical_attempts=physical_attempts,
        logical_request_count=int(provider_io_performed),
        physical_attempt_count=len(physical_attempts),
        provider_io_performed=provider_io_performed,
        disposition=disposition,
    )


__all__ = [
    "Clock",
    "MARKET_SESSION_CALENDAR_BOOTSTRAP_RECEIPT_CONTRACT",
    "MARKET_SESSION_CALENDAR_BOOTSTRAP_REQUEST_CONTRACT",
    "MarketSessionCalendarBootstrapDisposition",
    "MarketSessionCalendarBootstrapError",
    "MarketSessionCalendarBootstrapFailureReason",
    "MarketSessionCalendarBootstrapReceipt",
    "MarketSessionCalendarBootstrapRequest",
    "SystemClock",
    "bootstrap_mainland_session_calendar",
]

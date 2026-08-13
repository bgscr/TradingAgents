from __future__ import annotations

import json
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from hashlib import sha256
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from tradingagents.market_history.models import ProvenanceClass, ProviderDatasetSpec
from tradingagents.market_history.revisions import RevisionKind, revision_identity

if TYPE_CHECKING:
    from tradingagents.dataflows.baostock_calendar import (
        BaoStockSessionCalendarAcquisition,
    )
    from tradingagents.market_history.store import MarketHistoryStore


MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT = "mainland-session-calendar-payload-v1"
MAINLAND_SESSION_CALENDAR_READINESS_CONTRACT = (
    "mainland-session-calendar-readiness-v1"
)
MAINLAND_REGULAR_SESSION_TIME_CONTRACT = "mainland-cn-regular-session-times-v1"
MAINLAND_SESSION_CALENDAR_NORMALIZER = "baostock-session-calendar-normalizer-v1"
MAINLAND_SESSION_CALENDAR_DATASET = "mainland-session-calendar-v1"
MAINLAND_SESSION_CALENDAR_PROVENANCE = "observed_point_in_time"
MAINLAND_REFERENCE_MARKET = "mainland-cn"
MAINLAND_TIMEZONE_NAME = "Asia/Shanghai"
MAINLAND_CALENDAR_PROVIDER = "baostock"
MAINLAND_CALENDAR_ENDPOINT = "query_trade_dates"
PROVIDER_MEMBERSHIP_SOURCE = "provider_returned"
CONTRACT_DERIVED_SESSION_TIME_SOURCE = "contract_derived"


class MarketSessionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class MarketSessionCalendarReadFailure(str, Enum):
    CALENDAR_MISSING = "calendar_missing"
    REVISION_IDENTITY_MISMATCH = "revision_identity_mismatch"


class MarketSessionCalendarIntegrityError(RuntimeError):
    """Closed payload-free failure from an exact immutable calendar read."""

    def __init__(self, reason: MarketSessionCalendarReadFailure) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


class MainlandSessionCalendarReadinessFailure(str, Enum):
    CALENDAR_MISSING = "calendar_missing"
    AUTHORITY_MISMATCH = "authority_mismatch"
    INCOMPLETE_RANGE = "incomplete_range"
    MISSING_SESSION_TIMES = "missing_session_times"
    UNRESOLVED_COMPLETED_FRONTIER = "unresolved_completed_frontier"
    MISSING_FORWARD_HORIZON = "missing_forward_horizon"
    STALE_COVERAGE = "stale_coverage"
    REVISION_IDENTITY_MISMATCH = "revision_identity_mismatch"


class MainlandSessionCalendarReadinessError(RuntimeError):
    """Closed safe failure from structural persisted-calendar readiness."""

    def __init__(self, reason: MainlandSessionCalendarReadinessFailure) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


@dataclass(frozen=True)
class MarketSession:
    session_date: date
    status: MarketSessionStatus
    opens_at: datetime | None = None
    closes_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.status is MarketSessionStatus.CLOSED and (
            self.opens_at is not None or self.closes_at is not None
        ):
            raise ValueError("a closed market session cannot have open or close times")
        if (self.opens_at is None) != (self.closes_at is None):
            raise ValueError("market session open and close times must be supplied together")
        if self.opens_at is not None:
            if self.opens_at.tzinfo is None or self.closes_at.tzinfo is None:
                raise ValueError("market session times must be timezone-aware")
            if self.closes_at <= self.opens_at:
                raise ValueError("market session close must follow its open")


@dataclass(frozen=True)
class MarketSessionCalendarPublication:
    provider: ProviderDatasetSpec
    reference_market: str
    timezone_name: str
    observed_at: datetime
    provenance_class: ProvenanceClass
    raw_payload: bytes = field(repr=False)
    sessions: tuple[MarketSession, ...]
    endpoint: str = ""
    normalizer_version: str = ""
    payload_contract: str = ""
    session_time_contract: str = ""
    membership_source: str = ""
    session_time_source: str = ""

    def __post_init__(self) -> None:
        if not self.reference_market.strip():
            raise ValueError("reference_market must not be blank")
        ZoneInfo(self.timezone_name)
        if self.observed_at.tzinfo is None:
            raise ValueError("calendar observed_at must be timezone-aware")
        if not self.raw_payload:
            raise ValueError("calendar raw payload must not be empty")
        if not self.sessions:
            raise ValueError("calendar must include at least one session")
        dates = tuple(session.session_date for session in self.sessions)
        if dates != tuple(sorted(set(dates))):
            raise ValueError("calendar sessions must be unique and sorted by date")

    @property
    def payload_digest(self) -> str:
        return sha256(self.raw_payload).hexdigest()


@dataclass(frozen=True)
class PublishedSessionCalendar:
    calendar_revision_id: str
    ingestion_run_id: str


@dataclass(frozen=True)
class StoredSessionCalendar:
    calendar_revision_id: str
    reference_market: str
    timezone_name: str
    provider_dataset_id: str
    observed_at: datetime
    provenance_class: ProvenanceClass
    sessions: tuple[MarketSession, ...]
    provider_name: str = ""
    upstream_service_id: str = ""
    upstream_service_name: str = ""
    dataset_name: str = ""
    endpoint: str = ""
    normalizer_version: str = ""
    payload_contract: str = ""
    session_time_contract: str = ""
    membership_source: str = ""
    session_time_source: str = ""
    payload_digest: str = ""
    ingestion_run_id: str = ""

    @property
    def session_count(self) -> int:
        return len(self.sessions)


@dataclass(frozen=True)
class MainlandSessionCalendarReadiness:
    contract_version: str
    calendar_revision_id: str
    requested_start_date: date
    requested_end_date: date
    evaluated_at: datetime
    latest_completed_session: date
    latest_completed_session_close: datetime
    next_open_session: date
    next_open_session_close: datetime
    session_count: int


def publication_from_baostock_session_calendar_acquisition(
    acquisition: BaoStockSessionCalendarAcquisition,
) -> MarketSessionCalendarPublication:
    """Project one validated CAL-01 result into the sole calendar writer input."""

    from tradingagents.dataflows.baostock_calendar import (
        BaoStockSessionCalendarAcquisition,
    )

    if not isinstance(acquisition, BaoStockSessionCalendarAcquisition):
        raise TypeError("acquisition must be a BaoStockSessionCalendarAcquisition")
    from tradingagents.market_history.coordinator import (
        upstream_service_identity_for_provider,
    )

    expected_upstream_id, expected_upstream_name = (
        upstream_service_identity_for_provider(MAINLAND_CALENDAR_PROVIDER)
    )
    expected_provider_dataset_id = _provider_dataset_identity(expected_upstream_id)
    if (
        acquisition.upstream_service_id != expected_upstream_id
        or acquisition.upstream_service_name != expected_upstream_name
        or acquisition.provider_dataset_id != expected_provider_dataset_id
    ):
        raise ValueError("calendar acquisition authority is invalid")
    market_timezone = ZoneInfo(acquisition.timezone_name)
    sessions = tuple(
        MarketSession(
            session_date=item.calendar_date,
            status=(
                MarketSessionStatus.OPEN
                if item.is_trading_day
                else MarketSessionStatus.CLOSED
            ),
            opens_at=(
                datetime.combine(
                    item.calendar_date,
                    time(9, 30),
                    tzinfo=market_timezone,
                )
                if item.is_trading_day
                else None
            ),
            closes_at=(
                datetime.combine(
                    item.calendar_date,
                    time(15, 0),
                    tzinfo=market_timezone,
                )
                if item.is_trading_day
                else None
            ),
        )
        for item in sorted(
            acquisition.membership,
            key=lambda candidate: candidate.calendar_date,
        )
    )
    provider = ProviderDatasetSpec(
        upstream_service_id=acquisition.upstream_service_id,
        upstream_service_name=acquisition.upstream_service_name,
        provider_dataset_id=acquisition.provider_dataset_id,
        provider_name=acquisition.provider,
        dataset_name=acquisition.dataset_name,
        adjustment_methodology="not-applicable",
        strict_history_qualified=False,
    )
    payload = {
        "contract_version": acquisition.payload_contract,
        "dataset_name": acquisition.dataset_name,
        "endpoint": acquisition.endpoint,
        "membership_source": "provider_returned",
        "normalizer_version": acquisition.normalizer_version,
        "provenance_class": acquisition.provenance,
        "provider": acquisition.provider,
        "provider_dataset_id": acquisition.provider_dataset_id,
        "reference_market": acquisition.reference_market,
        "session_time_contract": acquisition.session_time_contract,
        "session_time_source": "contract_derived",
        "sessions": _session_fields(sessions),
        "timezone_name": acquisition.timezone_name,
        "upstream_service_id": acquisition.upstream_service_id,
        "upstream_service_name": acquisition.upstream_service_name,
    }
    return MarketSessionCalendarPublication(
        provider=provider,
        reference_market=acquisition.reference_market,
        timezone_name=acquisition.timezone_name,
        observed_at=acquisition.observed_at,
        provenance_class=ProvenanceClass(acquisition.provenance),
        raw_payload=_canonical_payload_bytes(payload),
        sessions=sessions,
        endpoint=acquisition.endpoint,
        normalizer_version=acquisition.normalizer_version,
        payload_contract=acquisition.payload_contract,
        session_time_contract=acquisition.session_time_contract,
        membership_source="provider_returned",
        session_time_source="contract_derived",
    )


def publish_session_calendar(
    store: MarketHistoryStore,
    publication: MarketSessionCalendarPublication,
) -> PublishedSessionCalendar:
    publication = canonical_session_calendar_publication(publication)
    from tradingagents.market_history.store import MarketHistoryCorruptionError

    install_failed = False
    try:
        artifact = store.install_payload(
            publication.raw_payload,
            media_type="application/json",
        )
    except (MarketHistoryCorruptionError, OSError):
        install_failed = True
    if install_failed:
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
        ) from None
    observed_at = publication.observed_at.astimezone(timezone.utc).isoformat()
    provider = publication.provider
    session_fields = _session_fields(publication.sessions)
    calendar_revision_id = revision_identity(
        RevisionKind.MARKET_SESSION_CALENDAR,
        {
            "payload_digest": artifact.digest,
            "provider_dataset_id": provider.provider_dataset_id,
            "reference_market": publication.reference_market,
            "sessions": session_fields,
            "timezone_name": publication.timezone_name,
        },
    )
    ingestion_run_id = revision_identity(
        RevisionKind.INGESTION_RUN,
        {
            "calendar_revision_id": calendar_revision_id,
            "observed_at": observed_at,
            "operation": "calendar_refresh",
        },
    )
    connection = store._connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT OR IGNORE INTO upstream_services "
            "(upstream_service_id, service_name, account_scope, created_at) "
            "VALUES (?, ?, '', ?)",
            (provider.upstream_service_id, provider.upstream_service_name, observed_at),
        )
        _verify_row(
            connection,
            "SELECT service_name, account_scope FROM upstream_services "
            "WHERE upstream_service_id = ?",
            (provider.upstream_service_id,),
            (provider.upstream_service_name, ""),
        )
        _calendar_phase(store, publication, "calendar_upstream_verified", calendar_revision_id)
        connection.execute(
            "INSERT OR IGNORE INTO provider_datasets "
            "(provider_dataset_id, provider_name, upstream_service_id, dataset_name, "
            "adjustment_methodology, strict_history_qualified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                provider.provider_dataset_id,
                provider.provider_name,
                provider.upstream_service_id,
                provider.dataset_name,
                provider.adjustment_methodology,
                int(provider.strict_history_qualified),
                observed_at,
            ),
        )
        _verify_row(
            connection,
            "SELECT provider_name, upstream_service_id, dataset_name, "
            "adjustment_methodology, strict_history_qualified "
            "FROM provider_datasets WHERE provider_dataset_id = ?",
            (provider.provider_dataset_id,),
            (
                provider.provider_name,
                provider.upstream_service_id,
                provider.dataset_name,
                provider.adjustment_methodology,
                int(provider.strict_history_qualified),
            ),
        )
        _calendar_phase(
            store,
            publication,
            "calendar_provider_dataset_verified",
            calendar_revision_id,
        )
        connection.execute(
            "INSERT OR IGNORE INTO ingestion_runs "
            "(run_id, provider_dataset_id, instrument_id, operation, provenance_class, "
            "requested_start, requested_end, started_at, completed_at, status, payload_digest) "
            "VALUES (?, ?, NULL, 'calendar_refresh', ?, ?, ?, ?, ?, 'published', ?)",
            (
                ingestion_run_id,
                provider.provider_dataset_id,
                publication.provenance_class.value,
                publication.sessions[0].session_date.isoformat(),
                publication.sessions[-1].session_date.isoformat(),
                observed_at,
                observed_at,
                artifact.digest,
            ),
        )
        _verify_row(
            connection,
            "SELECT provider_dataset_id, instrument_id, operation, provenance_class, "
            "requested_start, requested_end, started_at, completed_at, status, "
            "payload_digest, error_code FROM ingestion_runs WHERE run_id = ?",
            (ingestion_run_id,),
            (
                provider.provider_dataset_id,
                None,
                "calendar_refresh",
                publication.provenance_class.value,
                publication.sessions[0].session_date.isoformat(),
                publication.sessions[-1].session_date.isoformat(),
                observed_at,
                observed_at,
                "published",
                artifact.digest,
                None,
            ),
        )
        _calendar_phase(
            store,
            publication,
            "calendar_ingestion_completed",
            calendar_revision_id,
        )
        connection.execute(
            "INSERT OR IGNORE INTO market_session_calendars "
            "(calendar_revision_id, reference_market, timezone_name, provider_dataset_id, "
            "observed_at, first_observed_at, provenance_class, payload_digest, ingestion_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                calendar_revision_id,
                publication.reference_market,
                publication.timezone_name,
                provider.provider_dataset_id,
                observed_at,
                observed_at,
                publication.provenance_class.value,
                artifact.digest,
                ingestion_run_id,
            ),
        )
        _verify_calendar_revision_metadata(
            connection,
            calendar_revision_id=calendar_revision_id,
            reference_market=publication.reference_market,
            timezone_name=publication.timezone_name,
            provider_dataset_id=provider.provider_dataset_id,
            provenance_class=publication.provenance_class.value,
            payload_digest=artifact.digest,
        )
        _calendar_phase(
            store,
            publication,
            "calendar_revision_written",
            calendar_revision_id,
        )
        connection.executemany(
            "INSERT OR IGNORE INTO market_sessions "
            "(calendar_revision_id, session_date, session_status, opens_at, closes_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (
                    calendar_revision_id,
                    session.session_date.isoformat(),
                    session.status.value,
                    _optional_utc_text(session.opens_at),
                    _optional_utc_text(session.closes_at),
                )
                for session in publication.sessions
            ),
        )
        _calendar_phase(
            store,
            publication,
            "calendar_sessions_written",
            calendar_revision_id,
        )
        _verify_session_rows(
            connection,
            calendar_revision_id=calendar_revision_id,
            expected=session_fields,
        )
        if publication.payload_contract:
            _read_session_calendar_revision(
                connection,
                calendar_revision_id,
                read_payload=store.read_payload,
            )
        _calendar_phase(
            store,
            publication,
            "calendar_revision_verified",
            calendar_revision_id,
        )
        _calendar_phase(
            store,
            publication,
            "calendar_before_commit",
            calendar_revision_id,
        )
        connection.commit()
    except BaseException:
        with suppress(BaseException):
            connection.rollback()
        raise
    return PublishedSessionCalendar(calendar_revision_id, ingestion_run_id)


def canonical_session_calendar_publication(
    publication: MarketSessionCalendarPublication,
) -> MarketSessionCalendarPublication:
    """Return the canonical semantic bytes used by the existing store authority."""

    canonical_payload = _validate_publication_contract(publication)
    if canonical_payload is None or publication.raw_payload == canonical_payload:
        return publication
    return replace(publication, raw_payload=canonical_payload)


def read_current_session_calendar(
    store: MarketHistoryStore,
    reference_market: str,
) -> StoredSessionCalendar:
    return _read_current_session_calendar(store._connection, reference_market)


def read_session_calendar_revision(
    store: MarketHistoryStore,
    calendar_revision_id: str,
) -> StoredSessionCalendar:
    return _read_session_calendar_revision(
        store._connection,
        calendar_revision_id,
        read_payload=store.read_payload,
    )


def assess_mainland_session_calendar_readiness(
    store: MarketHistoryStore,
    calendar_revision_id: str,
    *,
    requested_start_date: date,
    requested_end_date: date,
    evaluated_at: datetime,
) -> MainlandSessionCalendarReadiness:
    """Assess Ticket-07 readiness from one exact persisted calendar revision."""

    if requested_end_date < requested_start_date:
        raise ValueError("requested calendar range is invalid")
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")

    revision_exists = store._connection.execute(
        "SELECT 1 FROM market_session_calendars WHERE calendar_revision_id = ?",
        (calendar_revision_id,),
    ).fetchone()
    if revision_exists is None:
        _readiness_fail(MainlandSessionCalendarReadinessFailure.CALENDAR_MISSING)
    metadata = store._connection.execute(
        "SELECT c.reference_market, c.timezone_name, c.provenance_class, "
        "d.provider_name, d.dataset_name, d.provider_dataset_id, "
        "d.upstream_service_id, s.service_name, c.payload_digest "
        "FROM market_session_calendars AS c "
        "JOIN provider_datasets AS d ON d.provider_dataset_id = c.provider_dataset_id "
        "JOIN upstream_services AS s "
        "ON s.upstream_service_id = d.upstream_service_id "
        "WHERE c.calendar_revision_id = ?",
        (calendar_revision_id,),
    ).fetchone()
    if metadata is None:
        _readiness_fail(
            MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH
        )
    assert metadata is not None
    reopen_failure = None
    try:
        reopened = read_session_calendar_revision(store, calendar_revision_id)
    except MarketSessionCalendarIntegrityError as exc:
        reopen_failure = exc.reason
    if reopen_failure is MarketSessionCalendarReadFailure.CALENDAR_MISSING:
        _readiness_fail(MainlandSessionCalendarReadinessFailure.CALENDAR_MISSING)
    if reopen_failure is not None:
        _readiness_fail(
            MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH
        )
    from tradingagents.market_history.coordinator import (
        upstream_service_identity_for_provider,
    )

    expected_upstream_id, expected_upstream_name = (
        upstream_service_identity_for_provider(MAINLAND_CALENDAR_PROVIDER)
    )
    if metadata[:8] != (
        MAINLAND_REFERENCE_MARKET,
        MAINLAND_TIMEZONE_NAME,
        MAINLAND_SESSION_CALENDAR_PROVENANCE,
        MAINLAND_CALENDAR_PROVIDER,
        MAINLAND_SESSION_CALENDAR_DATASET,
        _provider_dataset_identity(expected_upstream_id),
        expected_upstream_id,
        expected_upstream_name,
    ):
        _readiness_fail(MainlandSessionCalendarReadinessFailure.AUTHORITY_MISMATCH)
    payload = None
    try:
        payload = json.loads(store.read_payload(str(metadata[8])))
    except Exception:
        payload = None
    if payload is None:
        _readiness_fail(
            MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH
        )
    if not isinstance(payload, dict):
        _readiness_fail(
            MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH
        )
    if (
        payload.get("contract_version")
        != MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT
        or payload.get("normalizer_version")
        != MAINLAND_SESSION_CALENDAR_NORMALIZER
        or payload.get("session_time_contract")
        != MAINLAND_REGULAR_SESSION_TIME_CONTRACT
        or payload.get("membership_source") != PROVIDER_MEMBERSHIP_SOURCE
        or payload.get("session_time_source")
        != CONTRACT_DERIVED_SESSION_TIME_SOURCE
        or payload.get("endpoint") != MAINLAND_CALENDAR_ENDPOINT
    ):
        _readiness_fail(MainlandSessionCalendarReadinessFailure.AUTHORITY_MISMATCH)

    raw_rows = tuple(
        store._connection.execute(
            "SELECT session_date, session_status, opens_at, closes_at "
            "FROM market_sessions WHERE calendar_revision_id = ? "
            "ORDER BY session_date",
            (calendar_revision_id,),
        )
    )
    dates = None
    try:
        dates = tuple(date.fromisoformat(str(item[0])) for item in raw_rows)
    except (TypeError, ValueError):
        dates = None
    if dates is None:
        _readiness_fail(MainlandSessionCalendarReadinessFailure.INCOMPLETE_RANGE)
    statuses = tuple(str(item[1]) for item in raw_rows)
    if (
        dates != tuple(sorted(set(dates)))
        or any(status not in {"open", "closed"} for status in statuses)
    ):
        _readiness_fail(MainlandSessionCalendarReadinessFailure.INCOMPLETE_RANGE)
    expected_range = tuple(
        requested_start_date + timedelta(days=offset)
        for offset in range((requested_end_date - requested_start_date).days + 1)
    )
    represented_range = tuple(
        session_date
        for session_date in dates
        if requested_start_date <= session_date <= requested_end_date
    )
    if represented_range != expected_range:
        _readiness_fail(MainlandSessionCalendarReadinessFailure.INCOMPLETE_RANGE)

    sessions = None
    try:
        sessions = tuple(
            MarketSession(
                session_date=date.fromisoformat(str(item[0])),
                status=MarketSessionStatus(str(item[1])),
                opens_at=_optional_datetime(item[2]),
                closes_at=_optional_datetime(item[3]),
            )
            for item in raw_rows
        )
        _validate_mainland_session_times(
            sessions,
            timezone_name=MAINLAND_TIMEZONE_NAME,
        )
    except (TypeError, ValueError):
        sessions = None
    if sessions is None:
        _readiness_fail(MainlandSessionCalendarReadinessFailure.MISSING_SESSION_TIMES)

    market_timezone = ZoneInfo(MAINLAND_TIMEZONE_NAME)
    local_evaluated_at = evaluated_at.astimezone(market_timezone)
    completed = tuple(
        session
        for session in sessions
        if session.status is MarketSessionStatus.OPEN
        and (
            session.session_date < local_evaluated_at.date()
            or (
                session.session_date == local_evaluated_at.date()
                and session.closes_at is not None
                and local_evaluated_at >= session.closes_at.astimezone(market_timezone)
            )
        )
    )
    if not completed:
        _readiness_fail(
            MainlandSessionCalendarReadinessFailure.UNRESOLVED_COMPLETED_FRONTIER
        )
    latest = max(completed, key=lambda session: session.session_date)

    if requested_end_date < local_evaluated_at.date():
        _readiness_fail(MainlandSessionCalendarReadinessFailure.STALE_COVERAGE)

    later_open = next(
        (
            session
            for session in sessions
            if session.status is MarketSessionStatus.OPEN
            and session.session_date > latest.session_date
        ),
        None,
    )
    if later_open is None:
        if max(dates) <= local_evaluated_at.date():
            _readiness_fail(MainlandSessionCalendarReadinessFailure.STALE_COVERAGE)
        _readiness_fail(
            MainlandSessionCalendarReadinessFailure.MISSING_FORWARD_HORIZON
        )
    if later_open.closes_at is None:
        _readiness_fail(
            MainlandSessionCalendarReadinessFailure.MISSING_FORWARD_HORIZON
        )

    if reopened.sessions != sessions:
        _readiness_fail(
            MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH
        )
    assert latest.closes_at is not None
    return MainlandSessionCalendarReadiness(
        contract_version=MAINLAND_SESSION_CALENDAR_READINESS_CONTRACT,
        calendar_revision_id=calendar_revision_id,
        requested_start_date=requested_start_date,
        requested_end_date=requested_end_date,
        evaluated_at=evaluated_at,
        latest_completed_session=latest.session_date,
        latest_completed_session_close=latest.closes_at,
        next_open_session=later_open.session_date,
        next_open_session_close=later_open.closes_at,
        session_count=len(sessions),
    )


def _read_current_session_calendar(
    connection: sqlite3.Connection,
    reference_market: str,
) -> StoredSessionCalendar:
    """Read one pinned calendar through the caller's lifecycle-owned connection."""

    row = connection.execute(
        "SELECT calendar_revision_id, timezone_name, provider_dataset_id, observed_at, "
        "provenance_class FROM market_session_calendars WHERE reference_market = ? "
        "ORDER BY observed_at DESC, calendar_revision_id DESC LIMIT 1",
        (reference_market,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no stored session calendar for {reference_market}")
    sessions = tuple(
        MarketSession(
            session_date=date.fromisoformat(str(item[0])),
            status=MarketSessionStatus(str(item[1])),
            opens_at=_optional_datetime(item[2]),
            closes_at=_optional_datetime(item[3]),
        )
        for item in connection.execute(
            "SELECT session_date, session_status, opens_at, closes_at "
            "FROM market_sessions WHERE calendar_revision_id = ? ORDER BY session_date",
            (str(row[0]),),
        )
    )
    return StoredSessionCalendar(
        calendar_revision_id=str(row[0]),
        reference_market=reference_market,
        timezone_name=str(row[1]),
        provider_dataset_id=str(row[2]),
        observed_at=datetime.fromisoformat(str(row[3])),
        provenance_class=ProvenanceClass(str(row[4])),
        sessions=sessions,
    )


def _read_session_calendar_revision(
    connection: sqlite3.Connection,
    calendar_revision_id: str,
    *,
    read_payload,
) -> StoredSessionCalendar:
    """Read and verify only the requested immutable CAL-02 revision."""

    revision_exists = connection.execute(
        "SELECT 1 FROM market_session_calendars WHERE calendar_revision_id = ?",
        (calendar_revision_id,),
    ).fetchone()
    if revision_exists is None:
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.CALENDAR_MISSING
        )
    row = connection.execute(
        "SELECT c.reference_market, c.timezone_name, c.provider_dataset_id, "
        "c.observed_at, c.first_observed_at, c.provenance_class, c.payload_digest, "
        "c.ingestion_run_id, "
        "d.provider_name, d.dataset_name, d.adjustment_methodology, "
        "d.strict_history_qualified, d.upstream_service_id, s.service_name, "
        "s.account_scope, r.operation, r.provenance_class, r.requested_start, "
        "r.requested_end, r.completed_at, r.status, r.payload_digest "
        "FROM market_session_calendars AS c "
        "JOIN provider_datasets AS d ON d.provider_dataset_id = c.provider_dataset_id "
        "JOIN upstream_services AS s "
        "ON s.upstream_service_id = d.upstream_service_id "
        "JOIN ingestion_runs AS r ON r.run_id = c.ingestion_run_id "
        "WHERE c.calendar_revision_id = ?",
        (calendar_revision_id,),
    ).fetchone()
    if row is None:
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
        )
    read_failed = False
    try:
        payload_digest = str(row[6])
        raw_payload = read_payload(payload_digest)
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            raise ValueError
        session_rows = tuple(
            connection.execute(
                "SELECT session_date, session_status, opens_at, closes_at "
                "FROM market_sessions WHERE calendar_revision_id = ? "
                "ORDER BY session_date",
                (calendar_revision_id,),
            )
        )
        sessions = tuple(
            MarketSession(
                session_date=date.fromisoformat(str(item[0])),
                status=MarketSessionStatus(str(item[1])),
                opens_at=_optional_datetime(item[2]),
                closes_at=_optional_datetime(item[3]),
            )
            for item in session_rows
        )
        session_fields = _session_fields(sessions)
        expected_revision_id = revision_identity(
            RevisionKind.MARKET_SESSION_CALENDAR,
            {
                "payload_digest": payload_digest,
                "provider_dataset_id": str(row[2]),
                "reference_market": str(row[0]),
                "sessions": session_fields,
                "timezone_name": str(row[1]),
            },
        )
        expected_payload = {
            "contract_version": MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT,
            "dataset_name": str(row[9]),
            "endpoint": MAINLAND_CALENDAR_ENDPOINT,
            "membership_source": PROVIDER_MEMBERSHIP_SOURCE,
            "normalizer_version": MAINLAND_SESSION_CALENDAR_NORMALIZER,
            "provenance_class": str(row[5]),
            "provider": str(row[8]),
            "provider_dataset_id": str(row[2]),
            "reference_market": str(row[0]),
            "session_time_contract": MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
            "session_time_source": CONTRACT_DERIVED_SESSION_TIME_SOURCE,
            "sessions": session_fields,
            "timezone_name": str(row[1]),
            "upstream_service_id": str(row[12]),
            "upstream_service_name": str(row[13]),
        }
        observed_at = datetime.fromisoformat(str(row[3]))
        first_observed_at = datetime.fromisoformat(str(row[4]))
        from tradingagents.market_history.coordinator import (
            upstream_service_identity_for_provider,
        )

        expected_upstream_id, expected_upstream_name = (
            upstream_service_identity_for_provider(MAINLAND_CALENDAR_PROVIDER)
        )
        requested_start = sessions[0].session_date.isoformat()
        requested_end = sessions[-1].session_date.isoformat()
        if (
            calendar_revision_id != expected_revision_id
            or raw_payload != _canonical_payload_bytes(expected_payload)
            or payload != expected_payload
            or not sessions
            or observed_at.tzinfo is None
            or first_observed_at != observed_at
            or str(row[5]) != MAINLAND_SESSION_CALENDAR_PROVENANCE
            or str(row[8]) != MAINLAND_CALENDAR_PROVIDER
            or str(row[9]) != MAINLAND_SESSION_CALENDAR_DATASET
            or str(row[2]) != _provider_dataset_identity(expected_upstream_id)
            or str(row[12]) != expected_upstream_id
            or str(row[13]) != expected_upstream_name
            or str(row[0]) != MAINLAND_REFERENCE_MARKET
            or str(row[1]) != MAINLAND_TIMEZONE_NAME
            or str(row[10]) != "not-applicable"
            or int(row[11]) != 0
            or str(row[14]) != ""
            or str(row[15]) != "calendar_refresh"
            or str(row[16]) != str(row[5])
            or str(row[17]) != requested_start
            or str(row[18]) != requested_end
            or row[19] is None
            or str(row[20]) != "published"
            or str(row[21]) != payload_digest
        ):
            raise ValueError
        _validate_mainland_session_times(sessions, timezone_name=str(row[1]))
    except Exception:
        read_failed = True
    if read_failed:
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
        ) from None
    return StoredSessionCalendar(
        calendar_revision_id=calendar_revision_id,
        reference_market=str(row[0]),
        timezone_name=str(row[1]),
        provider_dataset_id=str(row[2]),
        observed_at=observed_at,
        provenance_class=ProvenanceClass(str(row[5])),
        sessions=sessions,
        provider_name=str(row[8]),
        upstream_service_id=str(row[12]),
        upstream_service_name=str(row[13]),
        dataset_name=str(row[9]),
        endpoint=MAINLAND_CALENDAR_ENDPOINT,
        normalizer_version=MAINLAND_SESSION_CALENDAR_NORMALIZER,
        payload_contract=MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT,
        session_time_contract=MAINLAND_REGULAR_SESSION_TIME_CONTRACT,
        membership_source=PROVIDER_MEMBERSHIP_SOURCE,
        session_time_source=CONTRACT_DERIVED_SESSION_TIME_SOURCE,
        payload_digest=payload_digest,
        ingestion_run_id=str(row[7]),
    )


def _optional_utc_text(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _session_fields(sessions: tuple[MarketSession, ...]) -> list[dict[str, object]]:
    return [
        {
            "closes_at": _optional_utc_text(session.closes_at),
            "opens_at": _optional_utc_text(session.opens_at),
            "session_date": session.session_date.isoformat(),
            "status": session.status.value,
        }
        for session in sessions
    ]


def _canonical_payload_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _provider_dataset_identity(upstream_service_id: str) -> str:
    digest = sha256()
    for component in (
        "provider-dataset",
        upstream_service_id,
        MAINLAND_SESSION_CALENDAR_DATASET,
    ):
        encoded = component.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"provider-dataset=sha256:{digest.hexdigest()}"


def _validate_mainland_session_times(
    sessions: tuple[MarketSession, ...],
    *,
    timezone_name: str,
) -> None:
    if timezone_name != MAINLAND_TIMEZONE_NAME:
        raise ValueError("calendar timezone contract mismatch")
    market_timezone = ZoneInfo(timezone_name)
    for session in sessions:
        if session.status is MarketSessionStatus.CLOSED:
            if session.opens_at is not None or session.closes_at is not None:
                raise ValueError("closed session has times")
            continue
        expected_open = datetime.combine(
            session.session_date,
            time(9, 30),
            tzinfo=market_timezone,
        )
        expected_close = datetime.combine(
            session.session_date,
            time(15, 0),
            tzinfo=market_timezone,
        )
        if session.opens_at != expected_open or session.closes_at != expected_close:
            raise ValueError("open session times violate contract")


def _validate_publication_contract(
    publication: MarketSessionCalendarPublication,
) -> bytes | None:
    if not publication.payload_contract:
        return None
    provider = publication.provider
    expected_payload = {
        "contract_version": publication.payload_contract,
        "dataset_name": provider.dataset_name,
        "endpoint": publication.endpoint,
        "membership_source": publication.membership_source,
        "normalizer_version": publication.normalizer_version,
        "provenance_class": publication.provenance_class.value,
        "provider": provider.provider_name,
        "provider_dataset_id": provider.provider_dataset_id,
        "reference_market": publication.reference_market,
        "session_time_contract": publication.session_time_contract,
        "session_time_source": publication.session_time_source,
        "sessions": _session_fields(publication.sessions),
        "timezone_name": publication.timezone_name,
        "upstream_service_id": provider.upstream_service_id,
        "upstream_service_name": provider.upstream_service_name,
    }
    if (
        publication.payload_contract != MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT
        or provider.provider_name != MAINLAND_CALENDAR_PROVIDER
        or provider.dataset_name != MAINLAND_SESSION_CALENDAR_DATASET
        or publication.reference_market != MAINLAND_REFERENCE_MARKET
        or publication.timezone_name != MAINLAND_TIMEZONE_NAME
        or publication.provenance_class.value
        != MAINLAND_SESSION_CALENDAR_PROVENANCE
        or publication.endpoint != MAINLAND_CALENDAR_ENDPOINT
        or publication.normalizer_version != MAINLAND_SESSION_CALENDAR_NORMALIZER
        or publication.session_time_contract
        != MAINLAND_REGULAR_SESSION_TIME_CONTRACT
        or publication.membership_source != PROVIDER_MEMBERSHIP_SOURCE
        or publication.session_time_source
        != CONTRACT_DERIVED_SESSION_TIME_SOURCE
    ):
        raise ValueError("calendar publication contract is invalid")
    supplied_payload = None
    try:
        supplied_payload = json.loads(publication.raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        supplied_payload = None
    if supplied_payload is None:
        raise ValueError("calendar publication contract is invalid") from None
    if supplied_payload != expected_payload:
        raise ValueError("calendar publication contract is invalid")
    _validate_mainland_session_times(
        publication.sessions,
        timezone_name=publication.timezone_name,
    )
    return _canonical_payload_bytes(expected_payload)


def _verify_row(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...],
    expected: tuple[object, ...],
) -> None:
    if connection.execute(query, parameters).fetchone() != expected:
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
        )


def _verify_calendar_revision_metadata(
    connection: sqlite3.Connection,
    *,
    calendar_revision_id: str,
    reference_market: str,
    timezone_name: str,
    provider_dataset_id: str,
    provenance_class: str,
    payload_digest: str,
) -> None:
    row = connection.execute(
        "SELECT reference_market, timezone_name, provider_dataset_id, "
        "provenance_class, payload_digest FROM market_session_calendars "
        "WHERE calendar_revision_id = ?",
        (calendar_revision_id,),
    ).fetchone()
    expected = (
        reference_market,
        timezone_name,
        provider_dataset_id,
        provenance_class,
        payload_digest,
    )
    if row != expected:
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
        )


def _verify_session_rows(
    connection: sqlite3.Connection,
    *,
    calendar_revision_id: str,
    expected: list[dict[str, object]],
) -> None:
    actual = [
        {
            "closes_at": item[3],
            "opens_at": item[2],
            "session_date": item[0],
            "status": item[1],
        }
        for item in connection.execute(
            "SELECT session_date, session_status, opens_at, closes_at "
            "FROM market_sessions WHERE calendar_revision_id = ? "
            "ORDER BY session_date",
            (calendar_revision_id,),
        )
    ]
    if actual != expected:
        raise MarketSessionCalendarIntegrityError(
            MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
        )


def _readiness_fail(reason: MainlandSessionCalendarReadinessFailure) -> None:
    raise MainlandSessionCalendarReadinessError(reason) from None


def _calendar_phase(
    store: MarketHistoryStore,
    publication: MarketSessionCalendarPublication,
    phase: str,
    calendar_revision_id: str,
) -> None:
    if publication.payload_contract == MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT:
        store._phase_hook(phase, calendar_revision_id)

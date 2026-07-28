from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from tradingagents.market_history.models import ProvenanceClass, ProviderDatasetSpec
from tradingagents.market_history.revisions import RevisionKind, revision_identity

if TYPE_CHECKING:
    from tradingagents.market_history.store import MarketHistoryStore


class MarketSessionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


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
    raw_payload: bytes
    sessions: tuple[MarketSession, ...]

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


def publish_session_calendar(
    store: MarketHistoryStore,
    publication: MarketSessionCalendarPublication,
) -> PublishedSessionCalendar:
    artifact = store.install_payload(publication.raw_payload, media_type="application/json")
    observed_at = publication.observed_at.astimezone(timezone.utc).isoformat()
    provider = publication.provider
    session_fields = [
        {
            "closes_at": _optional_utc_text(session.closes_at),
            "opens_at": _optional_utc_text(session.opens_at),
            "session_date": session.session_date.isoformat(),
            "status": session.status.value,
        }
        for session in publication.sessions
    ]
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
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return PublishedSessionCalendar(calendar_revision_id, ingestion_run_id)


def read_current_session_calendar(
    store: MarketHistoryStore,
    reference_market: str,
) -> StoredSessionCalendar:
    row = store._connection.execute(
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
        for item in store._connection.execute(
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


def _optional_utc_text(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None

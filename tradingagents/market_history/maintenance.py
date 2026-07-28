from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from tradingagents.market_history.calendar import (
    MarketSessionStatus,
    StoredSessionCalendar,
)
from tradingagents.market_history.revisions import RevisionKind, revision_identity


class PublicationState(str, Enum):
    NOT_YET_PUBLISHED = "not_yet_published"
    AVAILABLE = "available"


@dataclass(frozen=True)
class PublicationWatermark:
    watermark_id: str
    instrument_id: str
    provider_dataset_id: str
    expected_session_date: date
    state: PublicationState
    observed_at: datetime
    cooldown_until: datetime | None
    ingestion_run_id: str


@dataclass(frozen=True)
class InstrumentMaintenanceState:
    instrument_id: str
    provider_dataset_id: str
    canonical_symbol: str
    latest_completed_session: date | None
    last_reconciled_at: datetime | None
    lifecycle_state: str


@dataclass(frozen=True)
class MarketHistoryMaintenanceSummary:
    active_instruments: int
    due_reconciliations: int
    orphan_payloads: int
    orphan_payload_digests: tuple[str, ...]
    active_publication_cooldowns: int
    active_provider_cooldowns: int
    running_ingestion_runs: int
    failed_ingestion_runs: int
    info_diagnostics: int
    warning_diagnostics: int
    error_diagnostics: int
    latest_diagnostic_at: datetime | None


@dataclass(frozen=True)
class HistoryRequestDecision:
    should_request: bool
    expected_session_date: date | None
    reason: str


@dataclass(frozen=True)
class HistoryGapAssessment:
    all_gaps: tuple[date, ...]
    blocking_gaps: tuple[date, ...]


@dataclass(frozen=True)
class HistoryRefreshPlan:
    is_seed: bool
    session_dates: tuple[date, ...]
    requested_start: date | None
    requested_end: date | None


def plan_incremental_refresh(
    *,
    sessions,
    as_of_date: date,
    retained_session_dates: set[date] | frozenset[date],
    seeded: bool,
    listed_on: date | None = None,
    refresh_window_sessions: int = 21,
) -> HistoryRefreshPlan:
    if refresh_window_sessions < 1:
        raise ValueError("refresh window must contain at least one session")
    eligible = tuple(
        session.session_date
        for session in sessions
        if session.status is MarketSessionStatus.OPEN
        and session.session_date <= as_of_date
    )
    if not seeded:
        seed_start = as_of_date - relativedelta(years=5)
        if listed_on is not None:
            seed_start = max(seed_start, listed_on)
        planned = tuple(day for day in eligible if day >= seed_start)
        return HistoryRefreshPlan(
            is_seed=True,
            session_dates=planned,
            requested_start=planned[0] if planned else None,
            requested_end=planned[-1] if planned else None,
        )
    missing = {day for day in eligible if day not in retained_session_dates}
    overlap = set(eligible[-refresh_window_sessions:])
    planned = tuple(sorted(missing | overlap))
    return HistoryRefreshPlan(
        is_seed=False,
        session_dates=planned,
        requested_start=planned[0] if planned else None,
        requested_end=planned[-1] if planned else None,
    )


def reconciliation_due(
    *,
    lifecycle_state: str,
    last_reconciled_at: date | None,
    as_of: date,
) -> bool:
    if lifecycle_state != "active":
        return False
    if last_reconciled_at is None:
        return True
    return as_of >= last_reconciled_at + relativedelta(months=1)


def assess_history_gaps(
    *,
    sessions,
    observed_session_dates: set[date] | frozenset[date],
    suspension_session_dates: set[date] | frozenset[date],
    lifecycle_start: date,
    lifecycle_end: date | None,
    required_start: date,
    required_end: date,
) -> HistoryGapAssessment:
    if required_end < required_start:
        raise ValueError("required history range is invalid")
    if lifecycle_end is not None and lifecycle_end < lifecycle_start:
        raise ValueError("instrument lifecycle range is invalid")
    covered = observed_session_dates | suspension_session_dates
    gaps = tuple(
        session.session_date
        for session in sessions
        if session.status is MarketSessionStatus.OPEN
        and session.session_date >= lifecycle_start
        and (lifecycle_end is None or session.session_date <= lifecycle_end)
        and session.session_date not in covered
    )
    blocking = tuple(
        session_date
        for session_date in gaps
        if required_start <= session_date <= required_end
    )
    return HistoryGapAssessment(gaps, blocking)


def latest_completed_session(
    calendar: StoredSessionCalendar,
    *,
    as_of: datetime,
) -> date | None:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    market_timezone = ZoneInfo(calendar.timezone_name)
    local_as_of = as_of.astimezone(market_timezone)
    completed: list[date] = []
    for session in calendar.sessions:
        if session.status is not MarketSessionStatus.OPEN:
            continue
        if session.session_date < local_as_of.date():
            completed.append(session.session_date)
            continue
        if session.session_date > local_as_of.date():
            continue
        closes_at = session.closes_at or datetime.combine(
            session.session_date,
            time(15, 0),
            tzinfo=market_timezone,
        )
        if local_as_of >= closes_at.astimezone(market_timezone):
            completed.append(session.session_date)
    return max(completed) if completed else None


def decide_history_request(
    calendar: StoredSessionCalendar,
    *,
    as_of: datetime,
    retained_session_dates: set[date] | frozenset[date],
    watermark: PublicationWatermark | None = None,
) -> HistoryRequestDecision:
    expected = latest_completed_session(calendar, as_of=as_of)
    if expected is None:
        return HistoryRequestDecision(False, None, "no_completed_session")
    if expected in retained_session_dates:
        return HistoryRequestDecision(
            False,
            expected,
            "latest_completed_session_retained",
        )
    if (
        watermark is not None
        and watermark.expected_session_date == expected
        and watermark.state is PublicationState.NOT_YET_PUBLISHED
        and watermark.cooldown_until is not None
        and as_of < watermark.cooldown_until
    ):
        return HistoryRequestDecision(False, expected, "publication_cooldown")
    return HistoryRequestDecision(True, expected, "latest_completed_session_missing")


def record_publication_watermark(
    store,
    *,
    instrument_id: str,
    provider_dataset_id: str,
    expected_session_date: date,
    state: PublicationState,
    observed_at: datetime,
    cooldown_until: datetime | None,
) -> PublicationWatermark:
    if observed_at.tzinfo is None:
        raise ValueError("watermark observed_at must be timezone-aware")
    if state is PublicationState.NOT_YET_PUBLISHED and cooldown_until is None:
        raise ValueError("not-yet-published watermark requires a cooldown")
    if cooldown_until is not None:
        if cooldown_until.tzinfo is None:
            raise ValueError("watermark cooldown must be timezone-aware")
        if cooldown_until <= observed_at:
            raise ValueError("watermark cooldown must follow observed_at")
    observed_text = observed_at.isoformat()
    cooldown_text = cooldown_until.isoformat() if cooldown_until is not None else None
    watermark_id = revision_identity(
        RevisionKind.PUBLICATION_WATERMARK,
        {
            "expected_session_date": expected_session_date.isoformat(),
            "instrument_id": instrument_id,
            "observed_at": observed_text,
            "provider_dataset_id": provider_dataset_id,
            "state": state.value,
        },
    )
    ingestion_run_id = revision_identity(
        RevisionKind.INGESTION_RUN,
        {
            "expected_session_date": expected_session_date.isoformat(),
            "instrument_id": instrument_id,
            "observed_at": observed_text,
            "operation": "incremental_refresh",
            "provider_dataset_id": provider_dataset_id,
        },
    )
    connection = store._connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT OR IGNORE INTO ingestion_runs "
            "(run_id, provider_dataset_id, instrument_id, operation, provenance_class, "
            "requested_start, requested_end, started_at, completed_at, status, error_code) "
            "VALUES (?, ?, ?, 'incremental_refresh', 'observed_point_in_time', ?, ?, ?, ?, ?, ?)",
            (
                ingestion_run_id,
                provider_dataset_id,
                instrument_id,
                expected_session_date.isoformat(),
                expected_session_date.isoformat(),
                observed_text,
                observed_text,
                "failed" if state is PublicationState.NOT_YET_PUBLISHED else "published",
                (
                    "not_yet_published"
                    if state is PublicationState.NOT_YET_PUBLISHED
                    else None
                ),
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO publication_watermarks "
            "(watermark_id, provider_dataset_id, instrument_id, expected_session_date, "
            "publication_state, observed_at, cooldown_until, ingestion_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                watermark_id,
                provider_dataset_id,
                instrument_id,
                expected_session_date.isoformat(),
                state.value,
                observed_text,
                cooldown_text,
                ingestion_run_id,
            ),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return PublicationWatermark(
        watermark_id,
        instrument_id,
        provider_dataset_id,
        expected_session_date,
        state,
        observed_at,
        cooldown_until,
        ingestion_run_id,
    )


def current_publication_watermark(
    store,
    instrument_id: str,
    provider_dataset_id: str,
    expected_session_date: date,
) -> PublicationWatermark:
    row = store._connection.execute(
        "SELECT watermark_id, publication_state, observed_at, cooldown_until, "
        "ingestion_run_id FROM publication_watermarks WHERE instrument_id = ? "
        "AND provider_dataset_id = ? AND expected_session_date = ? "
        "ORDER BY observed_at DESC, watermark_id DESC LIMIT 1",
        (instrument_id, provider_dataset_id, expected_session_date.isoformat()),
    ).fetchone()
    if row is None:
        raise KeyError("publication watermark is unavailable")
    return PublicationWatermark(
        watermark_id=str(row[0]),
        instrument_id=instrument_id,
        provider_dataset_id=provider_dataset_id,
        expected_session_date=expected_session_date,
        state=PublicationState(str(row[1])),
        observed_at=datetime.fromisoformat(str(row[2])),
        cooldown_until=(
            datetime.fromisoformat(str(row[3])) if row[3] is not None else None
        ),
        ingestion_run_id=str(row[4]),
    )


def list_due_reconciliations(store, as_of: date) -> tuple[InstrumentMaintenanceState, ...]:
    states: list[InstrumentMaintenanceState] = []
    rows = store._connection.execute(
        "SELECT s.instrument_id, s.provider_dataset_id, i.canonical_symbol, "
        "s.latest_completed_session, s.last_reconciled_at, s.lifecycle_state "
        "FROM instrument_provider_state AS s "
        "JOIN instruments AS i ON i.instrument_id = s.instrument_id "
        "WHERE s.lifecycle_state = 'active' ORDER BY s.instrument_id, s.provider_dataset_id"
    )
    for row in rows:
        last_reconciled = (
            datetime.fromisoformat(str(row[4])) if row[4] is not None else None
        )
        if reconciliation_due(
            lifecycle_state=str(row[5]),
            last_reconciled_at=(
                last_reconciled.date() if last_reconciled is not None else None
            ),
            as_of=as_of,
        ):
            states.append(
                InstrumentMaintenanceState(
                    instrument_id=str(row[0]),
                    provider_dataset_id=str(row[1]),
                    canonical_symbol=str(row[2]),
                    latest_completed_session=(
                        date.fromisoformat(str(row[3])) if row[3] is not None else None
                    ),
                    last_reconciled_at=last_reconciled,
                    lifecycle_state=str(row[5]),
                )
            )
    return tuple(states)


def maintenance_summary(
    store,
    *,
    as_of: date,
    now: datetime,
) -> MarketHistoryMaintenanceSummary:
    if now.tzinfo is None:
        raise ValueError("maintenance-summary time must be timezone-aware")
    connection = store._connection
    lifecycle_counts = {
        str(state): int(count)
        for state, count in connection.execute(
            "SELECT lifecycle_state, COUNT(*) FROM instrument_provider_state "
            "GROUP BY lifecycle_state"
        )
    }
    ingestion_counts = {
        str(status): int(count)
        for status, count in connection.execute(
            "SELECT status, COUNT(*) FROM ingestion_runs GROUP BY status"
        )
    }
    diagnostic_counts = {
        str(severity): int(count)
        for severity, count in connection.execute(
            "SELECT severity, COUNT(*) FROM history_store_diagnostics "
            "GROUP BY severity"
        )
    }
    latest_row = connection.execute(
        "SELECT MAX(occurred_at) FROM history_store_diagnostics"
    ).fetchone()
    latest_value = latest_row[0] if latest_row is not None else None
    publication_row = connection.execute(
        "SELECT COUNT(*) FROM publication_watermarks "
        "WHERE publication_state = 'not_yet_published' AND cooldown_until > ?",
        (now.isoformat(),),
    ).fetchone()
    provider_row = connection.execute(
        "SELECT COUNT(*) FROM request_cooldowns WHERE cooldown_until > ?",
        (now.isoformat(),),
    ).fetchone()
    assert publication_row is not None
    assert provider_row is not None
    orphan_digests = store.orphan_payload_digests()
    return MarketHistoryMaintenanceSummary(
        active_instruments=lifecycle_counts.get("active", 0),
        due_reconciliations=len(list_due_reconciliations(store, as_of)),
        orphan_payloads=len(orphan_digests),
        orphan_payload_digests=orphan_digests,
        active_publication_cooldowns=int(publication_row[0]),
        active_provider_cooldowns=int(provider_row[0]),
        running_ingestion_runs=ingestion_counts.get("running", 0),
        failed_ingestion_runs=ingestion_counts.get("failed", 0),
        info_diagnostics=diagnostic_counts.get("info", 0),
        warning_diagnostics=diagnostic_counts.get("warning", 0),
        error_diagnostics=diagnostic_counts.get("error", 0),
        latest_diagnostic_at=(
            datetime.fromisoformat(str(latest_value))
            if latest_value is not None
            else None
        ),
    )


def set_instrument_lifecycle_state(
    store,
    instrument_id: str,
    provider_dataset_id: str,
    lifecycle_state: str,
) -> None:
    allowed = {"unseeded", "active", "degraded", "retired"}
    if lifecycle_state not in allowed:
        raise ValueError(f"invalid instrument lifecycle state {lifecycle_state!r}")
    cursor = store._connection.execute(
        "UPDATE instrument_provider_state SET lifecycle_state = ? "
        "WHERE instrument_id = ? AND provider_dataset_id = ?",
        (lifecycle_state, instrument_id, provider_dataset_id),
    )
    if cursor.rowcount != 1:
        raise KeyError("instrument provider state is unavailable")


def mark_reconciled(
    store,
    instrument_id: str,
    provider_dataset_id: str,
    reconciled_at: datetime,
) -> None:
    if reconciled_at.tzinfo is None:
        raise ValueError("reconciled_at must be timezone-aware")
    next_reconcile_on = reconciled_at.date() + relativedelta(months=1)
    cursor = store._connection.execute(
        "UPDATE instrument_provider_state SET last_reconciled_at = ?, "
        "next_reconcile_on = ? WHERE instrument_id = ? AND provider_dataset_id = ? "
        "AND lifecycle_state = 'active'",
        (
            reconciled_at.isoformat(),
            next_reconcile_on.isoformat(),
            instrument_id,
            provider_dataset_id,
        ),
    )
    if cursor.rowcount != 1:
        raise KeyError("active instrument provider state is unavailable")

"""Bounded calendar discovery for an explicit current/authoritative analysis.

Persisted CAL-02 revisions remain independent and immutable. Their closed
payload contract cannot represent composite lineage, so composition happens only
in a local view. A view is not a CAL-02 revision or a qualification/readiness
artifact and must never be published or passed to strict replay as a calendar pin.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

from tradingagents.dataflows.baostock_calendar import (
    BaoStockSessionCalendarRequest,
    acquire_mainland_session_calendar,
)
from tradingagents.market_history.calendar import (
    MAINLAND_CALENDAR_PROVIDER,
    MAINLAND_REFERENCE_MARKET,
    MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT,
    MAINLAND_TIMEZONE_NAME,
    MarketSession,
    MarketSessionCalendarIntegrityError,
    MarketSessionCalendarReadFailure,
    MarketSessionStatus,
    StoredSessionCalendar,
    publication_from_baostock_session_calendar_acquisition,
)
from tradingagents.market_history.config import DataUsageMode, MarketHistoryMode
from tradingagents.market_history.coordinator import (
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.models import ProvenanceClass
from tradingagents.market_history.revisions import RevisionKind, revision_identity
from tradingagents.market_history.store import MarketHistoryStore

_VIEW_CONTRACT = "local-verified-calendar-view-v1"
_VIEW_PROVENANCE = "local_verified_calendar_view"
_MAX_ACQUISITION_DAYS = 732


@dataclass(frozen=True)
class VerifiedMainlandCalendarView(StoredSessionCalendar):
    """An in-memory projection with inspectable per-date source provenance.

    ``payload_digest`` and ``ingestion_run_id`` are empty because this view has
    neither a stored payload nor an ingestion run. The separate view manifest
    binds every represented date to an actual, integrity-checked source revision.
    ``observed_at`` is the latest source observation, not a fresh provider check.
    """

    source_calendar_revision_ids: tuple[str, ...] = ()
    view_manifest_json: str = ""


def read_mainland_calendar_window(
    store: MarketHistoryStore, start: date, end: date,
) -> StoredSessionCalendar | None:
    """Read a complete natural-date view without any provider request or write.

    Missing dates return None. Corrupt revisions and equally recent conflicting
    memberships raise rather than being treated as permission to reacquire.
    """
    _validate_window(store, start, end)
    membership = _read_verified_membership(store, start, end)
    if len(membership) != (end - start).days + 1:
        return None
    return _calendar_view(membership, start, end)


def ensure_mainland_calendar_window(
    store: MarketHistoryStore,
    start: date,
    end: date,
    *,
    coordinator: ProviderRequestCoordinator,
    owner_id: str,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> StoredSessionCalendar:
    """Fill only missing calendar dates for this current-analysis request.

    Calling this helper from the explicit current/authoritative analysis path
    authorizes the bounded missing-date calendar requests below. It does not
    authorize price work, prewarming, strict replay, or shadow/disabled traffic.
    Each <=732-day chunk uses the existing three-operation coordinator workflow
    with no added retry. Successful chunks remain saved if a later chunk fails.
    The caller owns the coordinator and store lifecycle.
    """
    _validate_window(store, start, end)
    membership = _read_verified_membership(store, start, end)
    missing_windows = _missing_windows(membership, start, end)
    if not missing_windows:
        return _calendar_view(membership, start, end)
    if store.config.data_usage_mode is not DataUsageMode.PERSONAL_RESEARCH:
        raise ValueError("calendar discovery requires personal_research data usage")

    upstream_id, upstream_name = upstream_service_identity_for_provider("baostock")
    coordinator.register_upstream_service(upstream_id, upstream_name)
    for chunk_start, chunk_end in missing_windows:
        request = BaoStockSessionCalendarRequest(
            start_date=chunk_start,
            end_date=chunk_end,
            maximum_inclusive_range_days=_MAX_ACQUISITION_DAYS,
            data_usage_mode=store.config.data_usage_mode,
            # Authority is this explicit current/authoritative analysis call,
            # bounded to absent natural dates; not a global provider-I/O switch.
            operator_provider_io_authorized=True,
        )
        acquisition = acquire_mainland_session_calendar(
            request,
            coordinator=coordinator,
            owner_id=owner_id,
            now=now,
            sleep=sleep,
            lease_duration=timedelta(seconds=30),
        )
        publication = publication_from_baostock_session_calendar_acquisition(acquisition)
        # Publish exactly the provider-returned chunk. No synthetic union or
        # rewritten historical observation timestamp enters the CAL-02 writer.
        published = store.publish_session_calendar(publication)
        store.read_session_calendar_revision(published.calendar_revision_id)

    complete = read_mainland_calendar_window(store, start, end)
    if complete is None:
        raise ValueError("calendar discovery did not cover every requested natural date")
    return complete


def _validate_window(store: MarketHistoryStore, start: date, end: date) -> None:
    if store.config.mode not in {MarketHistoryMode.CURRENT, MarketHistoryMode.AUTHORITATIVE}:
        raise ValueError("calendar discovery is limited to current/authoritative analysis")
    if type(start) is not date or type(end) is not date:
        raise TypeError("calendar window boundaries must be dates")
    if start > end:
        raise ValueError("calendar window start must not follow end")


def _read_verified_membership(
    store: MarketHistoryStore, start: date, end: date,
) -> dict[date, tuple[MarketSession, StoredSessionCalendar]]:
    # Read all relevant immutable revisions, not just the latest revision whose
    # narrower range may have displaced older coverage in a latest-only lookup.
    revision_ids = tuple(str(row[0]) for row in store._connection.execute(
        "SELECT c.calendar_revision_id FROM market_session_calendars AS c "
        "WHERE c.reference_market = ? AND EXISTS ("
        "SELECT 1 FROM market_sessions AS s "
        "WHERE s.calendar_revision_id = c.calendar_revision_id "
        "AND s.session_date >= ? AND s.session_date <= ?) "
        "ORDER BY c.observed_at DESC, c.calendar_revision_id DESC",
        (MAINLAND_REFERENCE_MARKET, start.isoformat(), end.isoformat()),
    ))
    membership: dict[date, tuple[MarketSession, StoredSessionCalendar]] = {}
    for revision_id in revision_ids:
        calendar = _read_verified_revision(store, revision_id)
        for session in calendar.sessions:
            day = session.session_date
            if not start <= day <= end:
                continue
            prior = membership.get(day)
            if prior is None or calendar.observed_at > prior[1].observed_at:
                membership[day] = (session, calendar)
            elif calendar.observed_at == prior[1].observed_at:
                if session != prior[0]:
                    raise _integrity_failure()
                if calendar.provenance_class is ProvenanceClass.RETROSPECTIVE_BACKFILL:
                    membership[day] = (session, calendar)
    return membership


def _read_verified_revision(
    store: MarketHistoryStore,
    revision_id: str,
) -> StoredSessionCalendar:
    try:
        row = store._connection.execute(
            "SELECT c.reference_market, c.timezone_name, c.provider_dataset_id, "
            "c.observed_at, c.first_observed_at, c.provenance_class, c.payload_digest, "
            "c.ingestion_run_id, d.provider_name, d.dataset_name, "
            "d.adjustment_methodology, d.strict_history_qualified, d.upstream_service_id, "
            "u.service_name, u.account_scope, r.provider_dataset_id, r.instrument_id, "
            "r.operation, r.provenance_class, r.requested_start, r.requested_end, "
            "r.started_at, r.completed_at, r.status, r.payload_digest, r.error_code "
            "FROM market_session_calendars AS c "
            "JOIN provider_datasets AS d ON d.provider_dataset_id = c.provider_dataset_id "
            "JOIN upstream_services AS u ON u.upstream_service_id = d.upstream_service_id "
            "JOIN ingestion_runs AS r ON r.run_id = c.ingestion_run_id "
            "WHERE c.calendar_revision_id = ?", (revision_id,),
        ).fetchone()
        if row is None:
            raise _integrity_failure()
        raw_payload = store.read_payload(str(row[6]))
        # Legacy publication accepted opaque payload bytes. Only recognized
        # CAL-02 payloads enter its stricter semantic reader; generic integrity
        # is bound by the persisted revision formula, not a new normalizer.
        try:
            payload = json.loads(raw_payload)
        except (ValueError, UnicodeError):
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("contract_version") == MAINLAND_SESSION_CALENDAR_PAYLOAD_CONTRACT
        ):
            return store.read_session_calendar_revision(revision_id)
        return _verify_generic_revision(store, revision_id, row, raw_payload)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise _integrity_failure() from exc


def _verify_generic_revision(
    store: MarketHistoryStore,
    revision_id: str,
    row: tuple,
    raw_payload: bytes,
) -> StoredSessionCalendar:
    """Rebuild the legacy writer's identity without granting CAL-02 status."""
    session_rows = tuple(store._connection.execute(
        "SELECT session_date, session_status, opens_at, closes_at FROM market_sessions "
        "WHERE calendar_revision_id = ? ORDER BY session_date", (revision_id,),
    ))
    sessions = tuple(
        MarketSession(
            date.fromisoformat(str(item[0])), MarketSessionStatus(str(item[1])),
            datetime.fromisoformat(str(item[2])) if item[2] is not None else None,
            datetime.fromisoformat(str(item[3])) if item[3] is not None else None,
        )
        for item in session_rows
    )
    if not sessions:
        raise _integrity_failure()
    session_fields = [
        {"session_date": session.session_date.isoformat(), "status": session.status.value,
         "opens_at": _utc_text(session.opens_at), "closes_at": _utc_text(session.closes_at)}
        for session in sessions
    ]
    persisted_sessions = [
        {"session_date": item[0], "status": item[1], "opens_at": item[2], "closes_at": item[3]}
        for item in session_rows
    ]
    upstream_id, upstream_name = upstream_service_identity_for_provider("baostock")
    # Generic publication retained the caller's dataset and methodology. Its
    # revision identity binds that actual dataset ID, not the CAL-02 identity.
    dataset_id = str(row[2])
    observed_at = datetime.fromisoformat(str(row[3]))
    provenance = ProvenanceClass(str(row[5]))
    expected_revision = revision_identity(RevisionKind.MARKET_SESSION_CALENDAR, {
        "payload_digest": str(row[6]), "provider_dataset_id": str(row[2]),
        "reference_market": str(row[0]), "sessions": session_fields,
        "timezone_name": str(row[1]),
    })
    expected_run = revision_identity(RevisionKind.INGESTION_RUN, {
        "calendar_revision_id": revision_id, "observed_at": str(row[3]),
        "operation": "calendar_refresh",
    })
    if (
        not raw_payload or sha256(raw_payload).hexdigest() != str(row[6])
        or revision_id != expected_revision or str(row[7]) != expected_run
        or persisted_sessions != session_fields
        or observed_at.tzinfo is None or str(row[4]) != str(row[3])
        or row[:2] != (MAINLAND_REFERENCE_MARKET, MAINLAND_TIMEZONE_NAME)
        or row[8] != MAINLAND_CALENDAR_PROVIDER
        or row[12:15] != (upstream_id, upstream_name, "")
        or row[15:] != (
            dataset_id, None, "calendar_refresh", provenance.value,
            sessions[0].session_date.isoformat(), sessions[-1].session_date.isoformat(),
            str(row[3]), str(row[3]), "published", str(row[6]), None,
        )
    ):
        raise _integrity_failure()
    return StoredSessionCalendar(
        calendar_revision_id=revision_id,
        reference_market=MAINLAND_REFERENCE_MARKET, timezone_name=MAINLAND_TIMEZONE_NAME,
        provider_dataset_id=dataset_id, observed_at=observed_at,
        provenance_class=provenance, sessions=sessions,
        provider_name=MAINLAND_CALENDAR_PROVIDER,
        upstream_service_id=upstream_id, upstream_service_name=upstream_name,
        dataset_name=str(row[9]),
        normalizer_version="verified-legacy-calendar-membership-v1",
        # The legacy writer did not persist semantic/time contracts. Its
        # identity authenticates membership and any times, not their origin.
        session_time_contract="", session_time_source="unavailable",
        membership_source="verified_legacy_publication",
        payload_digest=str(row[6]), ingestion_run_id=str(row[7]),
    )


def _integrity_failure() -> MarketSessionCalendarIntegrityError:
    return MarketSessionCalendarIntegrityError(
        MarketSessionCalendarReadFailure.REVISION_IDENTITY_MISMATCH
    )


def _missing_windows(
    membership: dict[date, tuple[MarketSession, StoredSessionCalendar]],
    start: date,
    end: date,
) -> tuple[tuple[date, date], ...]:
    windows: list[tuple[date, date]] = []
    chunk_start: date | None = None
    chunk_end: date | None = None
    for ordinal in range(start.toordinal(), end.toordinal() + 1):
        day = date.fromordinal(ordinal)
        if day in membership:
            if chunk_start is not None:
                assert chunk_end is not None
                windows.append((chunk_start, chunk_end))
                chunk_start = chunk_end = None
            continue
        if chunk_start is None:
            chunk_start = day
        chunk_end = day
        if (day - chunk_start).days + 1 == _MAX_ACQUISITION_DAYS:
            windows.append((chunk_start, day))
            chunk_start = chunk_end = None
    if chunk_start is not None:
        assert chunk_end is not None
        windows.append((chunk_start, chunk_end))
    return tuple(windows)


def _calendar_view(
    membership: dict[date, tuple[MarketSession, StoredSessionCalendar]],
    start: date,
    end: date,
) -> VerifiedMainlandCalendarView:
    sources = {
        calendar.calendar_revision_id: calendar
        for _, calendar in membership.values()
    }
    revisions = tuple(sorted(sources))
    source = sources[revisions[0]]
    sessions = tuple(membership[day][0] for day in sorted(membership))
    provenance = (
        ProvenanceClass.RETROSPECTIVE_BACKFILL
        if any(calendar.provenance_class is ProvenanceClass.RETROSPECTIVE_BACKFILL
               for calendar in sources.values())
        else ProvenanceClass.OBSERVED_POINT_IN_TIME
    )
    time_contracts = {calendar.session_time_contract for calendar in sources.values()}
    time_sources = {calendar.session_time_source for calendar in sources.values()}
    time_contract = next(iter(time_contracts)) if len(time_contracts) == 1 else ""
    time_source = (
        next(iter(time_sources)) if time_contract and len(time_sources) == 1 else "unavailable"
    )
    manifest = {
        "contract": _VIEW_CONTRACT,
        "provenance": _VIEW_PROVENANCE,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "source_calendar_revision_ids": list(revisions),
        "provenance_class": provenance.value,
        "sources": [
            {
                "calendar_revision_id": revision_id,
                "provider_dataset_id": sources[revision_id].provider_dataset_id,
                "dataset_name": sources[revision_id].dataset_name,
                "provenance_class": sources[revision_id].provenance_class.value,
                "payload_contract": sources[revision_id].payload_contract,
                "membership_source": sources[revision_id].membership_source,
                "session_time_contract": sources[revision_id].session_time_contract,
                "session_time_source": sources[revision_id].session_time_source,
            }
            for revision_id in revisions
        ],
        "sessions": [
            {
                "session_date": session.session_date.isoformat(),
                "status": session.status.value,
                "opens_at": _utc_text(session.opens_at),
                "closes_at": _utc_text(session.closes_at),
                "calendar_revision_id": membership[session.session_date][1].calendar_revision_id,
                "source_observed_at": _utc_text(membership[session.session_date][1].observed_at),
            }
            for session in sessions
        ],
    }
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    )
    digest = sha256(manifest_json.encode("utf-8")).hexdigest()
    return VerifiedMainlandCalendarView(
        calendar_revision_id=f"view:sha256:{digest}",
        reference_market=MAINLAND_REFERENCE_MARKET,
        timezone_name=MAINLAND_TIMEZONE_NAME,
        provider_dataset_id=source.provider_dataset_id,
        observed_at=max(calendar.observed_at for calendar in sources.values()),
        provenance_class=provenance,
        sessions=sessions,
        provider_name=source.provider_name,
        upstream_service_id=source.upstream_service_id,
        upstream_service_name=source.upstream_service_name,
        dataset_name=source.dataset_name,
        endpoint=_VIEW_PROVENANCE,
        normalizer_version=_VIEW_CONTRACT,
        payload_contract=_VIEW_CONTRACT,
        session_time_contract=time_contract,
        membership_source=_VIEW_PROVENANCE,
        session_time_source=time_source,
        source_calendar_revision_ids=revisions,
        view_manifest_json=manifest_json,
    )


def _utc_text(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None

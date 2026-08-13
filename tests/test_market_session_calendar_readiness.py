from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tests.test_market_session_calendar_publication import _acquisition, _config
from tradingagents.dataflows.baostock_calendar import MainlandSessionCalendarMembership
from tradingagents.market_history import (
    MainlandSessionCalendarReadinessError,
    MainlandSessionCalendarReadinessFailure,
    MarketHistoryStore,
    publication_from_baostock_session_calendar_acquisition,
)


def _membership(*states: tuple[int, bool]):
    return tuple(
        MainlandSessionCalendarMembership(
            calendar_date=date(2026, 8, day),
            is_trading_day=is_open,
        )
        for day, is_open in states
    )


def _publish(store: MarketHistoryStore, membership):
    acquisition = _acquisition(
        requested_start_date=membership[0].calendar_date,
        requested_end_date=membership[-1].calendar_date,
        membership=membership,
    )
    return store.publish_session_calendar(
        publication_from_baostock_session_calendar_acquisition(acquisition)
    )


@pytest.mark.unit
def test_complete_exact_revision_is_structurally_ready(tmp_path) -> None:
    membership = _membership(
        (7, True),
        (8, False),
        (9, False),
        (10, True),
        (11, False),
        (12, True),
        (13, True),
        (14, True),
    )
    evaluated_at = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        published = _publish(store, membership)
        ready = store.assess_mainland_session_calendar_readiness(
            published.calendar_revision_id,
            requested_start_date=date(2026, 8, 7),
            requested_end_date=date(2026, 8, 14),
            evaluated_at=evaluated_at,
        )

    assert ready.contract_version == "mainland-session-calendar-readiness-v1"
    assert ready.calendar_revision_id == published.calendar_revision_id
    assert ready.requested_start_date == date(2026, 8, 7)
    assert ready.requested_end_date == date(2026, 8, 14)
    assert ready.evaluated_at == evaluated_at
    assert ready.latest_completed_session == date(2026, 8, 12)
    assert ready.latest_completed_session_close == datetime(
        2026, 8, 12, 7, 0, tzinfo=timezone.utc
    )
    assert ready.next_open_session == date(2026, 8, 13)
    assert ready.next_open_session_close == datetime(
        2026, 8, 13, 7, 0, tzinfo=timezone.utc
    )
    assert ready.session_count == 8


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "failure"),
    (
        ("missing", MainlandSessionCalendarReadinessFailure.CALENDAR_MISSING),
        (
            "authority",
            MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH,
        ),
        ("incomplete", MainlandSessionCalendarReadinessFailure.INCOMPLETE_RANGE),
        (
            "missing_times",
            MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH,
        ),
        (
            "unresolved",
            MainlandSessionCalendarReadinessFailure.UNRESOLVED_COMPLETED_FRONTIER,
        ),
        (
            "missing_forward",
            MainlandSessionCalendarReadinessFailure.MISSING_FORWARD_HORIZON,
        ),
        ("stale", MainlandSessionCalendarReadinessFailure.STALE_COVERAGE),
        (
            "identity",
            MainlandSessionCalendarReadinessFailure.REVISION_IDENTITY_MISMATCH,
        ),
    ),
)
def test_readiness_has_closed_structural_failures(tmp_path, case, failure) -> None:
    complete = _membership(
        (7, True),
        (8, False),
        (9, False),
        (10, True),
        (11, False),
        (12, True),
        (13, True),
        (14, True),
    )
    membership = (
        _membership(
            (7, True),
            (8, False),
            (9, False),
            (10, True),
            (11, False),
        )
        if case == "stale"
        else _membership(
            (7, True),
            (8, False),
            (9, False),
            (10, True),
            (11, False),
            (12, True),
            (13, False),
            (14, False),
        )
        if case == "missing_forward"
        else complete
    )
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        revision_id = "market-session-calendar=sha256:" + ("0" * 64)
        if case != "missing":
            revision_id = _publish(store, membership).calendar_revision_id
        if case == "authority":
            store._connection.execute(
                "INSERT INTO upstream_services "
                "(upstream_service_id, service_name, account_scope, created_at) "
                "VALUES ('upstream-other', 'Other service', '', ?) ",
                ("2026-08-12T08:00:00+00:00",),
            )
            store._connection.execute(
                "INSERT INTO provider_datasets "
                "(provider_dataset_id, provider_name, upstream_service_id, "
                "dataset_name, adjustment_methodology, strict_history_qualified, "
                "created_at) VALUES ('provider-other', 'other', "
                "'upstream-other', 'other-calendar', 'not-applicable', 0, ?)",
                ("2026-08-12T08:00:00+00:00",),
            )
            store._connection.execute(
                "UPDATE market_session_calendars SET provider_dataset_id = "
                "'provider-other' WHERE calendar_revision_id = ?",
                (revision_id,),
            )
        elif case == "missing_times":
            store._connection.execute(
                "UPDATE market_sessions SET opens_at = NULL, closes_at = NULL "
                "WHERE calendar_revision_id = ? AND session_date = '2026-08-12'",
                (revision_id,),
            )
        elif case == "identity":
            store._connection.execute(
                "UPDATE market_session_calendars SET observed_at = ? "
                "WHERE calendar_revision_id = ?",
                ("", revision_id),
            )

        requested_start = (
            date(2026, 8, 6) if case == "incomplete" else membership[0].calendar_date
        )
        evaluated_at = (
            datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
            if case == "unresolved"
            else datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
        )
        with pytest.raises(MainlandSessionCalendarReadinessError) as captured:
            store.assess_mainland_session_calendar_readiness(
                revision_id,
                requested_start_date=requested_start,
                requested_end_date=membership[-1].calendar_date,
                evaluated_at=evaluated_at,
            )

    assert captured.value.reason is failure
    assert str(captured.value) == failure.value
    assert "history" not in repr(captured.value).casefold()


@pytest.mark.unit
def test_readiness_uses_persisted_holiday_membership_not_weekdays(tmp_path) -> None:
    membership = _membership(
        (10, True),
        (11, False),
        (12, True),
        (13, True),
    )
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        published = _publish(store, membership)
        ready = store.assess_mainland_session_calendar_readiness(
            published.calendar_revision_id,
            requested_start_date=date(2026, 8, 10),
            requested_end_date=date(2026, 8, 13),
            evaluated_at=datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc),
        )

    assert ready.latest_completed_session == date(2026, 8, 10)
    assert ready.next_open_session == date(2026, 8, 12)


@pytest.mark.unit
def test_complete_coverage_ending_at_evaluated_frontier_is_stale(tmp_path) -> None:
    membership = _membership(
        (10, True),
        (11, False),
        (12, True),
    )
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        published = _publish(store, membership)
        with pytest.raises(MainlandSessionCalendarReadinessError) as captured:
            store.assess_mainland_session_calendar_readiness(
                published.calendar_revision_id,
                requested_start_date=date(2026, 8, 10),
                requested_end_date=date(2026, 8, 12),
                evaluated_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
            )

    assert captured.value.reason is MainlandSessionCalendarReadinessFailure.STALE_COVERAGE


@pytest.mark.unit
def test_coverage_ending_on_current_open_session_is_ready_before_close(
    tmp_path,
) -> None:
    membership = _membership(
        (10, True),
        (11, False),
        (12, True),
    )
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        published = _publish(store, membership)
        ready = store.assess_mainland_session_calendar_readiness(
            published.calendar_revision_id,
            requested_start_date=date(2026, 8, 10),
            requested_end_date=date(2026, 8, 12),
            evaluated_at=datetime(2026, 8, 12, 6, 59, 59, tzinfo=timezone.utc),
        )

    assert ready.latest_completed_session == date(2026, 8, 10)
    assert ready.next_open_session == date(2026, 8, 12)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("evaluated_at", "expected_latest"),
    (
        (datetime(2026, 8, 12, 6, 59, 59, tzinfo=timezone.utc), date(2026, 8, 10)),
        (datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc), date(2026, 8, 12)),
        (datetime(2026, 8, 12, 7, 0, 1, tzinfo=timezone.utc), date(2026, 8, 12)),
    ),
)
def test_readiness_frontier_changes_exactly_at_session_close(
    tmp_path,
    evaluated_at: datetime,
    expected_latest: date,
) -> None:
    membership = _membership(
        (10, True),
        (11, False),
        (12, True),
        (13, True),
    )
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        published = _publish(store, membership)
        ready = store.assess_mainland_session_calendar_readiness(
            published.calendar_revision_id,
            requested_start_date=date(2026, 8, 10),
            requested_end_date=date(2026, 8, 13),
            evaluated_at=evaluated_at,
        )

    assert ready.latest_completed_session == expected_latest


@pytest.mark.unit
def test_zero_calendar_historical_database_remains_valid(tmp_path) -> None:
    with MarketHistoryStore.open(_config(tmp_path)) as store:
        revision_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_session_calendars"
        ).fetchone()
        session_count = store._connection.execute(
            "SELECT COUNT(*) FROM market_sessions"
        ).fetchone()

    assert revision_count == (0,)
    assert session_count == (0,)

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from tradingagents.dataflows.financial_temporal_eligibility import (
    MARKET_SESSION_ELIGIBILITY_BINDING_VERSION,
    build_market_session_eligibility_binding,
)
from tradingagents.market_history import (
    MarketSession,
    MarketSessionStatus,
    ProvenanceClass,
    StoredSessionCalendar,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=SHANGHAI)


@pytest.fixture
def pinned_calendar() -> StoredSessionCalendar:
    return StoredSessionCalendar(
        calendar_revision_id="market-session-calendar:v1:" + "a" * 64,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provider_dataset_id="provider-dataset:baostock-calendar-v1",
        observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        sessions=(
            MarketSession(date(2026, 8, 7), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 8), MarketSessionStatus.CLOSED),
            MarketSession(date(2026, 8, 9), MarketSessionStatus.CLOSED),
            MarketSession(date(2026, 8, 10), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 11), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 12), MarketSessionStatus.CLOSED),
            MarketSession(date(2026, 8, 13), MarketSessionStatus.OPEN),
        ),
    )


@pytest.mark.unit
def test_calendar_binding_is_content_derived_from_the_authoritative_calendar(
    pinned_calendar: StoredSessionCalendar,
) -> None:
    binding = build_market_session_eligibility_binding(
        pinned_calendar,
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(11, 0, 48),
    )

    assert binding.contract_version == MARKET_SESSION_ELIGIBILITY_BINDING_VERSION
    assert binding.binding_identity.startswith(
        "market-session-eligibility-binding:v1:"
    )
    assert binding.calendar_revision_id == pinned_calendar.calendar_revision_id
    assert binding.reference_market == "mainland-cn"
    assert binding.timezone_name == "Asia/Shanghai"
    assert binding.requested_session_open is True
    assert binding.requested_session_close == _at(10, 15)
    assert binding.next_open_session == date(2026, 8, 11)
    assert binding.next_session_close == _at(11, 15)
    assert binding.latest_completed_session == date(2026, 8, 10)

    reordered = dict(reversed(tuple(binding.model_dump(mode="python").items())))
    assert type(binding).model_validate(reordered) == binding

    changed = build_market_session_eligibility_binding(
        pinned_calendar,
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(11, 0, 49),
    )
    assert changed.binding_identity != binding.binding_identity


@pytest.mark.unit
def test_calendar_binding_is_invariant_to_session_collection_order(
    pinned_calendar: StoredSessionCalendar,
) -> None:
    reordered_calendar = replace(
        pinned_calendar,
        sessions=tuple(reversed(pinned_calendar.sessions)),
    )

    original = build_market_session_eligibility_binding(
        pinned_calendar,
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(11, 0, 48),
    )
    reordered = build_market_session_eligibility_binding(
        reordered_calendar,
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(11, 0, 48),
    )

    assert reordered == original

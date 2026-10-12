"""Canonical CAL-02 fixtures made from synthetic membership; no provider I/O."""
from datetime import timedelta

from tests.test_market_session_calendar_publication import _acquisition
from tradingagents.dataflows.baostock_calendar import (
    MainlandSessionCalendarMembership,
    _content_identity,
)
from tradingagents.market_history import (
    MarketSessionStatus,
    publication_from_baostock_session_calendar_acquisition,
)


def canonical_calendar(sessions, observed_at):
    sessions = tuple(sessions)
    days = {item.session_date: item.status is MarketSessionStatus.OPEN for item in sessions}
    first, last = min(days), max(days)
    membership = tuple(
        MainlandSessionCalendarMembership(calendar_date=first + timedelta(days=index),
            is_trading_day=days.get(first + timedelta(days=index), False))
        for index in range((last - first).days + 1)
    )
    return publication_from_baostock_session_calendar_acquisition(_acquisition(
        requested_start_date=first, requested_end_date=last,
        requested_at=observed_at, retrieved_at=observed_at, observed_at=observed_at,
        membership=membership, content_identity=_content_identity(membership),
    ))

"""Exercise calendar admission with a synthetic operation and no transport."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from tradingagents.market_history import calendar_preparation as calendar


@pytest.mark.parametrize("case", ["expired", "wrong_exchange", "single_attempt"])
def test_calendar_send_requires_matching_scope_time_and_unused_attempt(case):
    request = calendar.Request.create(
        session="fixture", symbol="601328.SH", identity_revision="identity",
        source_id="source", store_binding="store", venue="XSHG", currency="CNY",
        kind="equity", analysis_date="2026-11-25", now=datetime(2026, 11, 26, tzinfo=timezone.utc),
    )
    ledger = Mock()
    operation = Mock(return_value="fixture")
    gate = calendar.PhysicalGate(request, ledger, lambda: 10, 10 if case == "expired" else 20)
    arguments = dict(
        destination=calendar.DESTINATION, exchange="SZSE" if case == "wrong_exchange" else "SSE",
        start="20211125", end="20261125", limit=5000, offset=0, operation=operation,
    )
    if case == "single_attempt":
        assert gate.send(**arguments) == "fixture"
    with pytest.raises(calendar.CalendarStopped):
        gate.send(**arguments)
    assert ledger.admit_calendar_once.call_count == operation.call_count == int(case == "single_attempt")

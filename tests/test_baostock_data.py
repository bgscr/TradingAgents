import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest

from tradingagents.dataflows import baostock_data
from tradingagents.dataflows.errors import VendorRateLimitError


class FakeLogin:
    error_code = "0"
    error_msg = ""


class FakeQuery:
    error_code = "0"
    error_msg = ""

    def __init__(self, rows):
        self.rows = rows
        self.index = -1

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


class FakeNamedQuery(FakeQuery):
    def __init__(self, fields, rows):
        super().__init__(rows)
        self.fields = fields


@pytest.mark.unit
def test_baostock_snapshot_workflow_builds_complete_strict_history_bundle(
    monkeypatch,
):
    factor_calls = []
    physical_requests = []

    def query_adjust_factor(*args, **kwargs):
        factor_calls.append((args, kwargs))
        return FakeNamedQuery(
            [
                "code",
                "dividOperateDate",
                "foreAdjustFactor",
                "backAdjustFactor",
                "adjustFactor",
            ],
            [["sh.600519", "2026-07-01", "0.5", "2", "1"]],
        )

    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: FakeNamedQuery(
            baostock_data.RAW_HISTORY_FIELDS.split(","),
            [
                [
                    "2026-07-23",
                    "sh.600519",
                    "20",
                    "22",
                    "18",
                    "21",
                    "20",
                    "100",
                    "2000",
                    "1",
                ],
                [
                    "2026-07-24",
                    "sh.600519",
                    "",
                    "",
                    "",
                    "",
                    "21",
                    "",
                    "",
                    "0",
                ],
            ],
        ),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "query_adjust_factor",
        query_adjust_factor,
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "query_trade_dates",
        lambda *args, **kwargs: FakeNamedQuery(
            ["calendar_date", "is_trading_day"],
            [["2026-07-23", "1"], ["2026-07-24", "1"]],
        ),
    )

    candidate = baostock_data.load_snapshot_with_history(
        "600519.SS",
        "2026-07-23",
        "2026-07-24",
        before_physical_request=physical_requests.append,
    )

    bundle = candidate.history_bundle
    assert bundle.provider.strict_history_qualified is True
    assert bundle.observations[0].close == Decimal("21")
    assert bundle.observations[1].close == Decimal("21")
    assert bundle.trading_statuses[1].status.value == "suspended"
    assert candidate.current_tradeability == "suspended"
    assert candidate.current_status_provenance.provider == "baostock"
    assert (
        candidate.current_status_provenance.provider_dataset_id
        == bundle.provider.provider_dataset_id
    )
    assert candidate.current_status_provenance.session_date == date(2026, 7, 24)
    assert candidate.current_status_provenance.status.value == "suspended"
    assert candidate.latest_traded_close == Decimal("10.5")
    assert candidate.latest_traded_close_diagnostic is None
    assert candidate.carried_suspension_close == Decimal("10.5")
    assert bundle.adjustment_factors[0].effective_date == date(2026, 7, 1)
    assert factor_calls == [
        (("sh.600519",), {"end_date": "2026-07-24"}),
    ]
    assert physical_requests == [
        "raw-history",
        "adjustment-factors",
        "session-calendar",
    ]
    assert candidate.frame["Close"].tolist() == [10.5, 10.5]
    assert candidate.frame["Volume"].tolist() == [100.0, 0.0]
    assert tuple(session.session_date for session in candidate.calendar.sessions) == (
        date(2026, 7, 23),
        date(2026, 7, 24),
    )


@pytest.mark.unit
def test_baostock_suspension_without_retained_trade_reports_unavailable_close(
    monkeypatch,
):
    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: FakeNamedQuery(
            baostock_data.RAW_HISTORY_FIELDS.split(","),
            [
                [
                    "2026-07-24",
                    "sh.600519",
                    "",
                    "",
                    "",
                    "",
                    "21",
                    "0",
                    "",
                    "0",
                ]
            ],
        ),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "query_adjust_factor",
        lambda *args, **kwargs: FakeNamedQuery(
            ["dividOperateDate", "foreAdjustFactor"],
            [["2026-07-01", "0.5"]],
        ),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "query_trade_dates",
        lambda *args, **kwargs: FakeNamedQuery(
            ["calendar_date", "is_trading_day"],
            [["2026-07-24", "1"]],
        ),
    )

    candidate = baostock_data.load_snapshot_with_history(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
    )

    assert candidate.current_tradeability == "suspended"
    assert candidate.latest_traded_close is None
    assert (
        candidate.latest_traded_close_diagnostic
        == "no_genuinely_traded_close_in_retained_history"
    )
    assert candidate.frame.iloc[-1]["Close"] == 10.5


@pytest.mark.unit
def test_baostock_zero_volume_without_authoritative_status_is_malformed(monkeypatch):
    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: FakeNamedQuery(
            baostock_data.RAW_HISTORY_FIELDS.split(","),
            [
                [
                    "2026-07-24",
                    "sh.600519",
                    "21",
                    "21",
                    "21",
                    "21",
                    "21",
                    "0",
                    "0",
                    "",
                ]
            ],
        ),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "query_adjust_factor",
        lambda *args, **kwargs: FakeNamedQuery(
            ["dividOperateDate", "foreAdjustFactor"],
            [["2026-07-01", "1"]],
        ),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "query_trade_dates",
        lambda *args, **kwargs: FakeNamedQuery(
            ["calendar_date", "is_trading_day"],
            [["2026-07-24", "1"]],
        ),
    )

    with pytest.raises(
        baostock_data.NoMarketDataError,
        match="unknown trading status",
    ):
        baostock_data.load_snapshot_with_history(
            "600519.SS",
            "2026-07-24",
            "2026-07-24",
        )


@pytest.mark.unit
def test_baostock_login_count_cap_is_typed_as_provider_capacity(monkeypatch):
    class LoginCountCap:
        error_code = "10001005"
        error_msg = "account login count reached upper limit"

    monkeypatch.setattr(baostock_data.bs, "login", lambda: LoginCountCap())

    with pytest.raises(VendorRateLimitError) as captured:
        baostock_data.load_ohlcv_range("600519.SS", "2026-07-01", "2026-07-24")

    assert captured.value.error_code == "10001005"


@pytest.mark.unit
def test_baostock_current_range_reports_its_physical_request(monkeypatch):
    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: FakeQuery(
            [
                [
                    "2026-07-24",
                    "sh.600519",
                    "20",
                    "22",
                    "18",
                    "21",
                    "100",
                    "2000",
                ]
            ]
        ),
    )
    physical_requests = []

    frame = baostock_data.load_ohlcv_range(
        "600519.SS",
        "2026-07-24",
        "2026-07-24",
        before_physical_request=physical_requests.append,
    )

    assert frame["Close"].tolist() == [21]
    assert physical_requests == ["adjusted-history"]


@pytest.mark.unit
def test_get_stock_data_formats_baostock_rows(monkeypatch):
    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *a, **k: FakeQuery(
            [
                [
                    "2026-06-29",
                    "sh.601138",
                    "69.3",
                    "71.46",
                    "66.5",
                    "69.61",
                    "1964970",
                    "13558013854.0",
                ],
            ]
        ),
    )

    out = baostock_data.get_stock_data("601138.SH", "2026-06-01", "2026-06-29")

    assert "# Stock data for 601138.SS" in out
    assert "# Primary source: Baostock query_history_k_data_plus" in out
    assert "2026-06-29,69.3,71.46,66.5,69.61,1964970,13558013854.0" in out


@pytest.mark.unit
def test_baostock_logout_runs_after_query(monkeypatch):
    events = []
    monkeypatch.setattr(baostock_data.bs, "login", lambda: events.append("login") or FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: events.append("logout"))
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *a, **k: FakeQuery(
            [
                [
                    "2026-06-29",
                    "sh.601138",
                    "69.3",
                    "71.46",
                    "66.5",
                    "69.61",
                    "1964970",
                    "13558013854.0",
                ],
            ]
        ),
    )

    baostock_data.get_stock_data("601138.SS", "2026-06-01", "2026-06-29")

    assert events == ["login", "logout"]


@pytest.mark.unit
def test_baostock_login_logout_output_is_suppressed(monkeypatch, capsys):
    monkeypatch.setattr(
        baostock_data.bs,
        "login",
        lambda: print("login success!") or FakeLogin(),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "logout",
        lambda: print("logout success!"),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *a, **k: FakeQuery(
            [
                [
                    "2026-06-29",
                    "sh.601138",
                    "69.3",
                    "71.46",
                    "66.5",
                    "69.61",
                    "1964970",
                    "13558013854.0",
                ],
            ]
        ),
    )

    baostock_data.get_stock_data("601138.SS", "2026-06-01", "2026-06-29")

    captured = capsys.readouterr()
    assert "login success!" not in captured.out
    assert "logout success!" not in captured.out


@pytest.mark.unit
def test_baostock_sessions_serialize_concurrent_callers_without_cross_contamination(
    monkeypatch,
):
    symbols = {
        "600895.SH": ("sh.600895", "600895.SS", "35.17"),
        "601658.SH": ("sh.601658", "601658.SS", "4.94"),
        "600000.SH": ("sh.600000", "600000.SS", "12.31"),
        "600036.SH": ("sh.600036", "600036.SS", "41.28"),
    }
    first_code = symbols["600895.SH"][0]
    first_query_entered = threading.Event()
    release_first_query = threading.Event()
    waiter_query_entered = threading.Event()
    state_lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def fake_login():
        with state_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        return FakeLogin()

    def fake_logout():
        with state_lock:
            state["active"] -= 1

    def fake_query(code, *args, **kwargs):
        if code == first_code:
            first_query_entered.set()
            assert release_first_query.wait(timeout=2.0)
        else:
            waiter_query_entered.set()
        price = next(value[2] for value in symbols.values() if value[0] == code)
        return FakeQuery([["2026-06-29", code, price, price, price, price, "1000", "10000"]])

    monkeypatch.setattr(baostock_data.bs, "login", fake_login)
    monkeypatch.setattr(baostock_data.bs, "logout", fake_logout)
    monkeypatch.setattr(baostock_data.bs, "query_history_k_data_plus", fake_query)

    with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
        first = pool.submit(
            baostock_data.get_stock_data,
            "600895.SH",
            "2026-06-29",
            "2026-06-29",
        )
        assert first_query_entered.wait(timeout=1.0)
        waiters = [
            (
                symbol,
                pool.submit(
                    baostock_data.get_stock_data,
                    symbol,
                    "2026-06-29",
                    "2026-06-29",
                ),
            )
            for symbol in symbols
            if symbol != "600895.SH"
        ]
        try:
            overlapped = waiter_query_entered.wait(timeout=0.25)
        finally:
            release_first_query.set()

        outputs = {"600895.SH": first.result(timeout=2.0)}
        outputs.update({symbol: future.result(timeout=2.0) for symbol, future in waiters})

    assert overlapped is False
    assert state == {"active": 0, "max_active": 1}
    for symbol, (_, canonical, price) in symbols.items():
        assert f"# Stock data for {canonical}" in outputs[symbol]
        assert f"2026-06-29,{price},{price},{price},{price},1000,10000" in outputs[symbol]


@pytest.mark.unit
def test_baostock_nested_session_times_out_instead_of_reentering(monkeypatch, caplog):
    events = []
    monkeypatch.setattr(
        baostock_data,
        "_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "login",
        lambda: events.append("login") or FakeLogin(),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "logout",
        lambda: events.append("logout"),
    )

    with (
        caplog.at_level("WARNING", logger=baostock_data.__name__),
        baostock_data._session(),
        pytest.raises(TimeoutError, match="waiting for the BaoStock session lock"),
        baostock_data._session(),
    ):
        pytest.fail("nested BaoStock session must not be entered")

    assert events == ["login", "logout"]
    assert "another session may be hung" in caplog.text


@pytest.mark.unit
def test_baostock_waiter_times_out_without_starting_a_second_login(monkeypatch, caplog):
    holder_entered = threading.Event()
    release_holder = threading.Event()
    login_calls = 0

    def fake_login():
        nonlocal login_calls
        login_calls += 1
        return FakeLogin()

    def hold_session():
        with baostock_data._session():
            holder_entered.set()
            assert release_holder.wait(timeout=2.0)

    monkeypatch.setattr(
        baostock_data,
        "_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(baostock_data.bs, "login", fake_login)
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)

    holder = threading.Thread(target=hold_session)
    holder.start()
    assert holder_entered.wait(timeout=1.0)
    try:
        with (
            caplog.at_level("WARNING", logger=baostock_data.__name__),
            pytest.raises(TimeoutError, match="waiting for the BaoStock session lock"),
            baostock_data._session(),
        ):
            pytest.fail("timed-out waiter must not enter the session")
    finally:
        release_holder.set()
        holder.join(timeout=2.0)

    assert holder.is_alive() is False
    assert login_calls == 1
    assert "another session may be hung" in caplog.text


@pytest.mark.unit
def test_baostock_session_lock_is_released_after_query_exception(monkeypatch):
    query_calls = 0
    events = []

    def fake_query(*args, **kwargs):
        nonlocal query_calls
        query_calls += 1
        if query_calls == 1:
            raise RuntimeError("query failed")
        return FakeQuery(
            [
                [
                    "2026-06-29",
                    "sh.600895",
                    "35.17",
                    "35.17",
                    "35.17",
                    "35.17",
                    "1000",
                    "10000",
                ]
            ]
        )

    monkeypatch.setattr(
        baostock_data.bs,
        "login",
        lambda: events.append("login") or FakeLogin(),
    )
    monkeypatch.setattr(
        baostock_data.bs,
        "logout",
        lambda: events.append("logout"),
    )
    monkeypatch.setattr(baostock_data.bs, "query_history_k_data_plus", fake_query)

    with pytest.raises(RuntimeError, match="query failed"):
        baostock_data.get_stock_data("600895.SH", "2026-06-29", "2026-06-29")

    out = baostock_data.get_stock_data("600895.SH", "2026-06-29", "2026-06-29")

    assert "# Stock data for 600895.SS" in out
    assert events == ["login", "logout", "login", "logout"]


@pytest.mark.unit
def test_baostock_session_lock_is_released_after_logout_exception(monkeypatch):
    logout_calls = 0

    def fake_logout():
        nonlocal logout_calls
        logout_calls += 1
        if logout_calls == 1:
            raise RuntimeError("logout failed")

    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", fake_logout)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *args, **kwargs: FakeQuery(
            [
                [
                    "2026-06-29",
                    "sh.600895",
                    "35.17",
                    "35.17",
                    "35.17",
                    "35.17",
                    "1000",
                    "10000",
                ]
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="logout failed"):
        baostock_data.get_stock_data("600895.SH", "2026-06-29", "2026-06-29")

    out = baostock_data.get_stock_data("600895.SH", "2026-06-29", "2026-06-29")

    assert "# Stock data for 600895.SS" in out
    assert logout_calls == 2


@pytest.mark.unit
def test_get_fundamentals_reports_limited_baostock_scope():
    out = baostock_data.get_fundamentals("600895.SH", "2026-06-29")

    assert "# Company Fundamentals for 600895.SS" in out
    assert "# Primary source: Baostock" in out
    assert "limited in phase 1" in out


@pytest.mark.unit
def test_interface_registers_real_baostock_functions():
    from tradingagents.dataflows import interface

    assert interface.VENDOR_METHODS["get_stock_data"]["baostock"] is baostock_data.get_stock_data
    assert (
        interface.VENDOR_METHODS["get_fundamentals"]["baostock"] is baostock_data.get_fundamentals
    )


@pytest.mark.unit
def test_get_stock_stats_indicators_window_uses_baostock_rows(monkeypatch):
    rows = []
    for day in range(1, 31):
        rows.append(
            [
                f"2026-06-{day:02d}",
                "sh.600895",
                str(10 + day),
                str(11 + day),
                str(9 + day),
                str(10.5 + day),
                str(100000 + day),
                str(1000000 + day),
            ]
        )
    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(
        baostock_data.bs,
        "query_history_k_data_plus",
        lambda *a, **k: FakeQuery(rows),
    )

    out = baostock_data.get_stock_stats_indicators_window("600895.SH", "rsi", "2026-06-30", 3)

    assert "## rsi values from 2026-06-27 to 2026-06-30" in out
    assert "Source: Baostock query_history_k_data_plus" in out
    assert "2026-06-30:" in out


@pytest.mark.unit
def test_indicators_reuse_cached_baostock_ohlcv(monkeypatch):
    cache = getattr(baostock_data, "_load_ohlcv_cached", None)
    if cache is not None:
        cache.cache_clear()
    calls = 0
    rows = []
    for day in range(1, 31):
        rows.append(
            [
                f"2026-06-{day:02d}",
                "sh.600895",
                str(10 + day),
                str(11 + day),
                str(9 + day),
                str(10.5 + day),
                str(100000 + day),
                str(1000000 + day),
            ]
        )

    def fake_query(*a, **k):
        nonlocal calls
        calls += 1
        return FakeQuery(rows)

    monkeypatch.setattr(baostock_data.bs, "login", lambda: FakeLogin())
    monkeypatch.setattr(baostock_data.bs, "logout", lambda: None)
    monkeypatch.setattr(baostock_data.bs, "query_history_k_data_plus", fake_query)

    baostock_data.get_stock_stats_indicators_window("600895.SH", "close_10_ema", "2026-06-30", 3)
    baostock_data.get_stock_stats_indicators_window("600895.SH", "rsi", "2026-06-30", 3)

    assert calls == 1

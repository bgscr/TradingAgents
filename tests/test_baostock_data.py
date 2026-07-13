import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.dataflows import baostock_data


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

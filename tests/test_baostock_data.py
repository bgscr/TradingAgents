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
        lambda *a, **k: FakeQuery([
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
        ]),
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
        lambda *a, **k: FakeQuery([
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
        ]),
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
        lambda *a, **k: FakeQuery([
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
        ]),
    )

    baostock_data.get_stock_data("601138.SS", "2026-06-01", "2026-06-29")

    captured = capsys.readouterr()
    assert "login success!" not in captured.out
    assert "logout success!" not in captured.out


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
    assert interface.VENDOR_METHODS["get_fundamentals"]["baostock"] is baostock_data.get_fundamentals


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

    out = baostock_data.get_stock_stats_indicators_window(
        "600895.SH", "rsi", "2026-06-30", 3
    )

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

    baostock_data.get_stock_stats_indicators_window(
        "600895.SH", "close_10_ema", "2026-06-30", 3
    )
    baostock_data.get_stock_stats_indicators_window(
        "600895.SH", "rsi", "2026-06-30", 3
    )

    assert calls == 1

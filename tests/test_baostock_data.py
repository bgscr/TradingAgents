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

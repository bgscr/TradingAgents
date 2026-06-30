from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import baostock as bs
import pandas as pd

from .errors import NoMarketDataError, VendorNotConfiguredError
from .stockstats_utils import _assert_ohlcv_not_stale
from .symbol_utils import resolve_china_a_symbol


FIELDS = "date,code,open,high,low,close,volume,amount"


@contextmanager
def _session():
    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        raise VendorNotConfiguredError(f"Baostock login failed: {login.error_msg}")
    try:
        yield
    finally:
        bs.logout()


def _rows_to_frame(rows: list[list[str]], symbol: str, canonical: str) -> pd.DataFrame:
    if not rows:
        raise NoMarketDataError(symbol, canonical, "Baostock returned no rows")

    frame = pd.DataFrame(
        rows,
        columns=["Date", "Code", "Open", "High", "Low", "Close", "Volume", "Amount"],
    )
    frame = frame[["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]]
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"])
    for col in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["Close"])
    if frame.empty:
        raise NoMarketDataError(symbol, canonical, "Baostock returned no usable rows")
    return frame.sort_values("Date")


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(symbol, symbol, "Baostock supports China A-share symbols only")

    with _session():
        rs = bs.query_history_k_data_plus(
            instrument.baostock_code,
            FIELDS,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",
        )
        if getattr(rs, "error_code", "0") != "0":
            raise NoMarketDataError(symbol, instrument.yahoo_symbol, rs.error_msg)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

    frame = _rows_to_frame(rows, symbol, instrument.yahoo_symbol)
    _assert_ohlcv_not_stale(frame, end_date, symbol, instrument.yahoo_symbol)
    out = frame.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    header = f"# Stock data for {instrument.yahoo_symbol} from {start_date} to {end_date}\n"
    header += "# Primary source: Baostock query_history_k_data_plus\n"
    header += "# Fallback source used: Baostock\n"
    header += f"# Total records: {len(out)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + out.to_csv(index=False)


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    instrument = resolve_china_a_symbol(ticker)
    if instrument is None:
        raise NoMarketDataError(ticker, ticker, "Baostock supports China A-share symbols only")
    return (
        f"# Company Fundamentals for {instrument.yahoo_symbol}\n"
        "# Primary source: Baostock\n"
        "Baostock fallback fundamentals are limited in phase 1. "
        "Use AKShare and Yahoo supplemental fundamentals when available."
    )

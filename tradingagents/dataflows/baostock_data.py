from __future__ import annotations

import contextlib
import io
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache

import baostock as bs
import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from .akshare_data import INDICATOR_DESCRIPTIONS
from .errors import NoMarketDataError, VendorNotConfiguredError
from .stockstats_utils import _assert_ohlcv_not_stale
from .symbol_utils import resolve_china_a_symbol

logger = logging.getLogger(__name__)

FIELDS = "date,code,open,high,low,close,volume,amount"
_BAOSTOCK_SESSION_LOCK = threading.Lock()
_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS = 30.0


@contextmanager
def _session():
    acquired = _BAOSTOCK_SESSION_LOCK.acquire(timeout=_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS)
    if not acquired:
        message = (
            f"Timed out after {_BAOSTOCK_SESSION_LOCK_TIMEOUT_SECONDS:g}s "
            "waiting for the BaoStock session lock; another session may be hung."
        )
        logger.warning(message)
        raise TimeoutError(message)

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise VendorNotConfiguredError(f"Baostock login failed: {login.error_msg}")
        try:
            yield
        finally:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                bs.logout()
    finally:
        _BAOSTOCK_SESSION_LOCK.release()


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


@lru_cache(maxsize=64)
def _load_ohlcv_cached(symbol: str, curr_date: str, years: int) -> pd.DataFrame:
    curr = pd.to_datetime(curr_date)
    start = (curr - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(symbol, symbol, "Baostock supports China A-share symbols only")

    with _session():
        rs = bs.query_history_k_data_plus(
            instrument.baostock_code,
            FIELDS,
            start_date=start,
            end_date=curr.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="2",
        )
        if getattr(rs, "error_code", "0") != "0":
            raise NoMarketDataError(symbol, instrument.yahoo_symbol, rs.error_msg)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

    frame = _rows_to_frame(rows, symbol, instrument.yahoo_symbol)
    filtered = frame[frame["Date"] <= curr].copy()
    _assert_ohlcv_not_stale(filtered, curr_date, symbol, instrument.yahoo_symbol)
    return filtered


def load_ohlcv(symbol: str, curr_date: str, years: int = 5) -> pd.DataFrame:
    return _load_ohlcv_cached(symbol, curr_date, years).copy()


def _indicator_values(symbol: str, indicator: str, curr_date: str) -> dict[str, str]:
    data = load_ohlcv(symbol, curr_date)
    df = wrap(data)
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    df[indicator]
    values = {}
    for _, row in df.iterrows():
        value = row[indicator]
        values[row["Date"]] = "N/A" if pd.isna(value) else str(value)
    return values


def get_stock_stats_indicators_window(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int,
) -> str:
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS.keys())}"
        )

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)
    values = _indicator_values(symbol, indicator, curr_date)
    lines = []
    current = curr_date_dt
    while current >= before:
        date_str = current.strftime("%Y-%m-%d")
        lines.append(
            f"{date_str}: {values.get(date_str, 'N/A: Not a trading day (weekend or holiday)')}"
        )
        current = current - relativedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\nSource: Baostock query_history_k_data_plus\n\n"
        + INDICATOR_DESCRIPTIONS[indicator]
    )


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

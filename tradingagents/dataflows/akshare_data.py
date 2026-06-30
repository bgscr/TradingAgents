from __future__ import annotations

from datetime import datetime

import akshare as ak
import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from .errors import NoMarketDataError
from .stockstats_utils import _assert_ohlcv_not_stale
from .symbol_utils import resolve_china_a_symbol


INDICATOR_DESCRIPTIONS = {
    "close_50_sma": (
        "50 SMA: A medium-term trend indicator. Usage: Identify trend direction "
        "and serve as dynamic support/resistance. Tips: It lags price; combine "
        "with faster indicators for timely signals."
    ),
    "close_200_sma": (
        "200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend "
        "and identify golden/death cross setups. Tips: It reacts slowly; best for "
        "strategic trend confirmation rather than frequent trading entries."
    ),
    "close_10_ema": (
        "10 EMA: A responsive short-term average. Usage: Capture quick shifts in "
        "momentum and potential entry points. Tips: Prone to noise in choppy markets; "
        "use alongside longer averages for filtering false signals."
    ),
    "macd": "MACD: Computes momentum via differences of EMAs.",
    "macds": "MACD Signal: An EMA smoothing of the MACD line.",
    "macdh": "MACD Histogram: Shows the gap between the MACD line and its signal.",
    "rsi": "RSI: Measures momentum to flag overbought/oversold conditions.",
    "boll": "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: Typically 2 standard deviations above the middle line.",
    "boll_lb": "Bollinger Lower Band: Typically 2 standard deviations below the middle line.",
    "atr": "ATR: Averages true range to measure volatility.",
    "vwma": "VWMA: A moving average weighted by volume.",
    "mfi": "MFI: Money Flow Index uses price and volume to measure buying/selling pressure.",
}


def _require_china_a(symbol: str):
    instrument = resolve_china_a_symbol(symbol)
    if instrument is None:
        raise NoMarketDataError(symbol, symbol, "AKShare supports China A-share symbols only")
    return instrument


def _date_for_akshare(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")


def _normalize_hist_frame(data: pd.DataFrame, symbol: str, canonical: str) -> pd.DataFrame:
    if data is None or data.empty:
        raise NoMarketDataError(symbol, canonical, "AKShare returned no rows")

    rename_map = {
        "日期": "Date",
        "开盘": "Open",
        "最高": "High",
        "最低": "Low",
        "收盘": "Close",
        "成交量": "Volume",
        "成交额": "Amount",
    }
    missing = [col for col in rename_map if col not in data.columns]
    if missing:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"AKShare history missing expected columns: {', '.join(missing)}",
        )

    frame = data.rename(columns=rename_map).copy()
    keep = ["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]
    frame = frame[keep]
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"])
    for col in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["Close"])
    frame = frame.sort_values("Date")
    if frame.empty:
        raise NoMarketDataError(symbol, canonical, "AKShare returned no usable rows")
    return frame


def _fetch_hist(symbol: str, start_date: str, end_date: str) -> tuple[str, pd.DataFrame]:
    instrument = _require_china_a(symbol)
    raw = ak.stock_zh_a_hist(
        symbol=instrument.akshare_code,
        period="daily",
        start_date=_date_for_akshare(start_date),
        end_date=_date_for_akshare(end_date),
        adjust="qfq",
    )
    frame = _normalize_hist_frame(raw, symbol, instrument.yahoo_symbol)
    _assert_ohlcv_not_stale(frame, end_date, symbol, instrument.yahoo_symbol)
    return instrument.yahoo_symbol, frame


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    canonical, frame = _fetch_hist(symbol, start_date, end_date)
    out = frame.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    header = f"# Stock data for {canonical} from {start_date} to {end_date}\n"
    header += "# Primary source: AKShare stock_zh_a_hist\n"
    header += "# Fallback source used: none\n"
    header += f"# Total records: {len(out)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + out.to_csv(index=False)


def load_ohlcv(symbol: str, curr_date: str, years: int = 5) -> pd.DataFrame:
    curr = pd.to_datetime(curr_date)
    start = (curr - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    canonical, frame = _fetch_hist(symbol, start, curr.strftime("%Y-%m-%d"))
    filtered = frame[frame["Date"] <= curr].copy()
    _assert_ohlcv_not_stale(filtered, curr_date, symbol, canonical)
    return filtered


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
        lines.append(f"{date_str}: {values.get(date_str, 'N/A: Not a trading day (weekend or holiday)')}")
        current = current - relativedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\nSource: AKShare stock_zh_a_hist\n\n"
        + INDICATOR_DESCRIPTIONS[indicator]
    )

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


def _format_optional_error(endpoint: str, exc: Exception) -> str:
    return f"DATA_DEGRADED: AKShare {endpoint} unavailable ({exc})."


def _get_yfinance_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    from .y_finance import get_fundamentals as get_yfinance_fundamentals

    return get_yfinance_fundamentals(ticker, curr_date)


def _yahoo_supplemental_section(
    yahoo_symbol: str, curr_date: str | None = None
) -> tuple[list[str], list[str]]:
    try:
        supplemental = _get_yfinance_fundamentals(yahoo_symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 - Yahoo is supplemental only
        return [], [f"DATA_DEGRADED: Yahoo supplemental fundamentals unavailable ({exc})."]

    if not isinstance(supplemental, str) or not supplemental.strip():
        return [], ["DATA_DEGRADED: Yahoo supplemental fundamentals unavailable (empty response)."]

    stripped = supplemental.strip()
    unavailable_markers = (
        "Error retrieving fundamentals",
        "NO_DATA_AVAILABLE",
        "no fundamental fields returned",
        "no fundamentals returned",
    )
    if any(marker in stripped for marker in unavailable_markers):
        return [], [f"DATA_DEGRADED: Yahoo supplemental fundamentals unavailable ({stripped})."]

    return ["## Yahoo Supplemental Profile", stripped], []


def _safe_frame(endpoint: str, fn):
    try:
        data = fn()
    except Exception as exc:  # noqa: BLE001 - degrade optional endpoints into report text
        return None, _format_optional_error(endpoint, exc)
    if data is None or data.empty:
        return None, f"DATA_DEGRADED: AKShare {endpoint} returned no rows."
    return data, None


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    instrument = _require_china_a(ticker)
    raw = ak.stock_news_em(symbol=instrument.akshare_code)
    if raw is None or raw.empty:
        return f"No news found for {ticker} (resolved to {instrument.yahoo_symbol})"

    frame = raw.copy()
    frame["发布时间"] = pd.to_datetime(frame["发布时间"], errors="coerce")
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date) + pd.Timedelta(days=1)
    frame = frame[(frame["发布时间"] >= start) & (frame["发布时间"] < end)]
    if frame.empty:
        return (
            f"No news found for {ticker} (resolved to {instrument.yahoo_symbol}) "
            f"between {start_date} and {end_date}"
        )

    lines = [
        f"## {instrument.yahoo_symbol} News, from {start_date} to {end_date}:",
        "",
        "Source: AKShare stock_news_em",
        "",
    ]
    for _, row in frame.iterrows():
        title = row.get("新闻标题", "No title")
        source = row.get("文章来源", "Unknown")
        lines.append(f"### {title} (source: {source})")
        content = row.get("新闻内容")
        if isinstance(content, str) and content.strip():
            lines.append(content.strip())
        link = row.get("新闻链接")
        if isinstance(link, str) and link.strip():
            lines.append(f"Link: {link.strip()}")
        lines.append("")
    return "\n".join(lines)


def _business_section(code: str) -> tuple[list[str], list[str]]:
    data, error = _safe_frame("stock_zyjs_ths", lambda: ak.stock_zyjs_ths(symbol=code))
    if error:
        return [], [error]
    row = data.iloc[0]
    lines = ["## Business Description"]
    for col in ("主营业务", "产品类型", "产品名称", "经营范围"):
        value = row.get(col)
        if pd.notna(value) and str(value).strip():
            lines.append(f"{col}: {value}")
    return lines, []


def _financial_abstract_section_from_frame(data: pd.DataFrame) -> tuple[list[str], list[str]]:
    latest_cols = [col for col in data.columns if str(col).isdigit()]
    latest_cols = sorted(latest_cols, reverse=True)[:4]
    if not latest_cols:
        return [], ["DATA_DEGRADED: AKShare stock_financial_abstract returned no period columns."]
    label_cols = ["选项", "指标"]
    missing_labels = [col for col in label_cols if col not in data.columns]
    if missing_labels:
        return [], [
            "DATA_DEGRADED: AKShare stock_financial_abstract missing label columns: "
            f"{', '.join(missing_labels)}."
        ]
    keep = ["选项", "指标", *latest_cols]
    lines = ["## Financial Abstract", data[keep].head(20).to_csv(index=False)]
    return lines, []


def _financial_abstract_section(code: str) -> tuple[list[str], list[str]]:
    data, error = _safe_frame(
        "stock_financial_abstract", lambda: ak.stock_financial_abstract(symbol=code)
    )
    if error:
        return [], [error]
    return _financial_abstract_section_from_frame(data)


def _fund_flow_section(code: str, exchange: str) -> tuple[list[str], list[str]]:
    market = "sh" if exchange == "shanghai" else "sz"
    data, error = _safe_frame(
        "stock_individual_fund_flow",
        lambda: ak.stock_individual_fund_flow(stock=code, market=market),
    )
    if error:
        return [], [error]
    keep = [
        col
        for col in ("日期", "收盘价", "涨跌幅", "主力净流入-净额", "主力净流入-净占比")
        if col in data.columns
    ]
    if not keep:
        return [], ["DATA_DEGRADED: AKShare stock_individual_fund_flow returned no expected columns."]
    lines = ["## Recent Fund Flow", data[keep].tail(10).to_csv(index=False)]
    return lines, []


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    instrument = _require_china_a(ticker)
    sections = [
        f"# Company Fundamentals for {instrument.yahoo_symbol}",
        "# Primary source: AKShare",
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    degraded: list[str] = []

    for builder in (
        lambda: _business_section(instrument.akshare_code),
        lambda: _financial_abstract_section(instrument.akshare_code),
        lambda: _fund_flow_section(instrument.akshare_code, instrument.exchange),
        lambda: _yahoo_supplemental_section(instrument.yahoo_symbol, curr_date),
    ):
        lines, errors = builder()
        if lines:
            sections.extend(lines)
            sections.append("")
        degraded.extend(errors)

    if degraded:
        sections.append("## Degraded Fields")
        sections.extend(degraded)
        sections.append("Do not fabricate degraded or missing AKShare values.")

    return "\n".join(sections)


def get_china_a_identity(ticker: str) -> dict[str, str]:
    instrument = resolve_china_a_symbol(ticker)
    if instrument is None:
        return {}

    identity = {"exchange": instrument.exchange}
    data, error = _safe_frame("stock_zyjs_ths", lambda: ak.stock_zyjs_ths(symbol=instrument.akshare_code))
    if error or data is None:
        return identity

    row = data.iloc[0]
    for source_key in ("股票简称", "股票名称", "证券简称", "证券名称"):
        company_name = row.get(source_key)
        if pd.notna(company_name) and str(company_name).strip():
            identity["company_name"] = str(company_name).strip()
            break

    business = row.get("主营业务")
    if pd.notna(business) and str(business).strip():
        identity["industry"] = str(business).strip()

    return identity

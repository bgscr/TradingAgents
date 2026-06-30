from __future__ import annotations

import akshare as ak
import pandas as pd

from .symbol_utils import resolve_china_a_symbol


def _format_optional_error(endpoint: str, exc: Exception) -> str:
    return f"DATA_DEGRADED: AKShare {endpoint} unavailable ({exc})."


def _safe_frame(endpoint: str, fn) -> tuple[pd.DataFrame | None, str | None]:
    try:
        data = fn()
    except Exception as exc:  # noqa: BLE001 - optional enrichment only
        return None, _format_optional_error(endpoint, exc)
    if data is None or data.empty:
        return None, f"DATA_DEGRADED: AKShare {endpoint} returned no rows."
    return data, None


def _eastmoney_symbol(code: str, exchange: str) -> str:
    prefix = "SH" if exchange == "shanghai" else "SZ"
    return f"{prefix}{code}"


def _filter_date_range(
    data: pd.DataFrame,
    date_col: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    if date_col not in data.columns:
        return data
    frame = data.copy()
    dates = pd.to_datetime(frame[date_col], errors="coerce")
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date) + pd.Timedelta(days=1)
    return frame[(dates >= start) & (dates < end)]


def _csv_section(title: str, data: pd.DataFrame, columns: list[str], limit: int = 8) -> list[str]:
    keep = [col for col in columns if col in data.columns]
    if not keep:
        return [f"DATA_DEGRADED: {title} returned no expected columns."]
    return [f"## {title}", data[keep].tail(limit).to_csv(index=False)]


def _popularity_section(
    eastmoney_symbol: str,
    start_date: str,
    end_date: str,
) -> tuple[list[str], list[str]]:
    data, error = _safe_frame(
        "stock_hot_rank_detail_em",
        lambda: ak.stock_hot_rank_detail_em(symbol=eastmoney_symbol),
    )
    if error:
        return [], [error]

    frame = _filter_date_range(data, "\u65f6\u95f4", start_date, end_date)
    if frame.empty:
        return [], ["DATA_DEGRADED: AKShare stock_hot_rank_detail_em returned no rows in requested window."]
    return _csv_section(
        "Eastmoney popularity trend",
        frame,
        ["\u65f6\u95f4", "\u6392\u540d", "\u8bc1\u5238\u4ee3\u7801", "\u65b0\u664b\u7c89\u4e1d", "\u94c1\u6746\u7c89\u4e1d"],
    ), []


def _stock_comment_section(code: str) -> tuple[list[str], list[str]]:
    data, error = _safe_frame("stock_comment_em", ak.stock_comment_em)
    if error:
        return [], [error]

    frame = data.copy()
    if "\u4ee3\u7801" not in frame.columns:
        return [], ["DATA_DEGRADED: AKShare stock_comment_em missing code column."]
    frame = frame[frame["\u4ee3\u7801"].astype(str).str.zfill(6) == code]
    if frame.empty:
        return [], ["DATA_DEGRADED: AKShare stock_comment_em returned no row for this symbol."]
    return _csv_section(
        "Eastmoney stock comment",
        frame,
        [
            "\u4ee3\u7801",
            "\u540d\u79f0",
            "\u6700\u65b0\u4ef7",
            "\u673a\u6784\u53c2\u4e0e\u5ea6",
            "\u7efc\u5408\u5f97\u5206",
            "\u5173\u6ce8\u6307\u6570",
            "\u4ea4\u6613\u65e5",
        ],
        limit=1,
    ), []


def _northbound_section(
    code: str,
    start_date: str,
    end_date: str,
) -> tuple[list[str], list[str]]:
    data, error = _safe_frame(
        "stock_hsgt_individual_em",
        lambda: ak.stock_hsgt_individual_em(symbol=code),
    )
    if error:
        return [], [error]

    frame = _filter_date_range(data, "\u6301\u80a1\u65e5\u671f", start_date, end_date)
    if frame.empty:
        return [], ["DATA_DEGRADED: AKShare stock_hsgt_individual_em returned no rows."]
    return _csv_section(
        "Northbound holdings",
        frame,
        [
            "\u6301\u80a1\u65e5\u671f",
            "\u5f53\u65e5\u6536\u76d8\u4ef7",
            "\u6301\u80a1\u6570\u91cf",
            "\u6301\u80a1\u5e02\u503c",
            "\u4eca\u65e5\u589e\u6301\u80a1\u6570",
            "\u4eca\u65e5\u589e\u6301\u8d44\u91d1",
        ],
    ), []


def get_china_a_local_sentiment(ticker: str, start_date: str, end_date: str) -> str:
    instrument = resolve_china_a_symbol(ticker)
    if instrument is None:
        return (
            f"<china local sentiment not applicable: {ticker} is not a China A-share symbol; "
            "AKShare/Eastmoney local sentiment supports China A-share symbols only>"
        )

    sections = [
        f"# China A-share local sentiment for {instrument.yahoo_symbol}",
        "Source: AKShare/Eastmoney",
        f"Window: {start_date} to {end_date}",
        "",
    ]
    degraded: list[str] = []
    eastmoney_symbol = _eastmoney_symbol(instrument.akshare_code, instrument.exchange)

    for builder in (
        lambda: _popularity_section(eastmoney_symbol, start_date, end_date),
        lambda: _stock_comment_section(instrument.akshare_code),
        lambda: _northbound_section(instrument.akshare_code, start_date, end_date),
    ):
        lines, errors = builder()
        if lines:
            sections.extend(lines)
            sections.append("")
        degraded.extend(errors)

    if degraded:
        sections.append("## Degraded Fields")
        sections.extend(degraded)
        sections.append("Do not fabricate degraded or missing China local sentiment values.")

    return "\n".join(sections)

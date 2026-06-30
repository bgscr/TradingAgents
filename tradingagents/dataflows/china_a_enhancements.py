from __future__ import annotations

import contextlib
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import akshare as ak
import pandas as pd

from .config import get_config
from .symbol_utils import resolve_china_a_symbol

CATEGORY_FLOW_SENTIMENT = "flow_sentiment"
CATEGORY_ANNOUNCEMENTS = "announcements"
CATEGORY_INDUSTRY_POLICY = "industry_policy"

PRESET_CATEGORIES = {
    "basic": set(),
    "flow_sentiment": {CATEGORY_FLOW_SENTIMENT},
    "announcements": {CATEGORY_ANNOUNCEMENTS},
    "industry_policy": {CATEGORY_INDUSTRY_POLICY},
    "all": {
        CATEGORY_FLOW_SENTIMENT,
        CATEGORY_ANNOUNCEMENTS,
        CATEGORY_INDUSTRY_POLICY,
    },
}

SOURCE_TIMEOUT_SECONDS = 8
CACHE_TTL_SECONDS = 6 * 60 * 60
MAX_SOURCE_LINES = 8
_EXECUTOR = ThreadPoolExecutor(max_workers=4)


@dataclass(frozen=True)
class SourceResult:
    source: str
    status: str
    as_of: str | None
    records: tuple[str, ...] = ()
    error: str | None = None


def _cache_root() -> Path:
    return Path(get_config()["data_cache_dir"]) / "china_a_enhancements"


def _cache_path(ticker: str, curr_date: str, preset: str, categories: set[str]) -> Path:
    key = json.dumps(
        {
            "ticker": ticker,
            "curr_date": curr_date,
            "preset": preset,
            "categories": sorted(categories),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _cache_root() / f"{digest}.md"


def _read_cache(path: Path) -> str | None:
    if not path.exists():
        return None
    age = datetime.now().timestamp() - path.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        return None
    return path.read_text(encoding="utf-8")


def _write_cache(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _call_source(
    source: str,
    fn: Callable[[], pd.DataFrame],
    timeout_seconds: int = SOURCE_TIMEOUT_SECONDS,
) -> tuple[pd.DataFrame | None, SourceResult | None]:
    def _wrapped() -> pd.DataFrame:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn()

    future = _EXECUTOR.submit(_wrapped)
    try:
        frame = future.result(timeout=timeout_seconds)
    except TimeoutError:
        return None, SourceResult(
            source=source,
            status="unavailable",
            as_of=None,
            error=f"timed out after {timeout_seconds}s",
        )
    except Exception as exc:  # noqa: BLE001 - fail-open enrichment surface
        return None, SourceResult(
            source=source,
            status="unavailable",
            as_of=None,
            error=str(exc),
        )
    if frame is None or frame.empty:
        return None, SourceResult(
            source=source,
            status="unavailable",
            as_of=None,
            error="returned no rows",
        )
    return frame, None


def _string_value(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _first_present(row: pd.Series, names: tuple[str, ...]) -> str:
    for name in names:
        if name in row:
            value = _string_value(row.get(name))
            if value:
                return value
    return ""


def _find_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in frame.columns:
            return name
    return None


def _format_result(result: SourceResult) -> list[str]:
    if result.status == "ok":
        return [
            f"- Source: {result.source}; as_of: {result.as_of or 'unknown'}; {record}"
            for record in result.records[:MAX_SOURCE_LINES]
        ]
    return [f"- Source unavailable: {result.source} ({result.error or 'unknown error'})."]


def _format_snapshot(
    ticker: str,
    curr_date: str,
    preset: str,
    results: list[tuple[str, list[SourceResult]]],
) -> str:
    if not results:
        return ""

    lines = [
        "## China A-share Enhancement Snapshot",
        f"Ticker: {ticker}",
        f"Analysis date: {curr_date}",
        f"Preset: {preset}",
        "",
        "Use this source-labeled snapshot as supplemental China-local context.",
        "",
    ]
    for title, source_results in results:
        lines.append(f"### {title}")
        for result in source_results:
            lines.extend(_format_result(result))
        lines.append("")
    return "\n".join(lines).strip()


def _eastmoney_symbol(instrument) -> str:
    prefix = "SH" if instrument.exchange == "shanghai" else "SZ"
    return f"{prefix}{instrument.akshare_code}"


def _market_arg(instrument) -> str:
    return "sh" if instrument.exchange == "shanghai" else "sz"


def _collect_flow_sentiment(instrument, curr_date: str) -> list[SourceResult]:
    results: list[SourceResult] = []

    frame, error = _call_source(
        "stock_individual_fund_flow",
        lambda: ak.stock_individual_fund_flow(
            stock=instrument.akshare_code,
            market=_market_arg(instrument),
        ),
    )
    if error:
        results.append(error)
    else:
        records = []
        for _, row in frame.tail(5).iterrows():
            records.append(
                "date={date}; close={close}; pct_change={pct}; main_net_inflow={net}; "
                "main_net_ratio={ratio}".format(
                    date=_first_present(row, ("date", "日期", "鏃ユ湡")),
                    close=_first_present(row, ("close", "收盘价", "鏀剁洏浠�")),
                    pct=_first_present(row, ("pct_change", "涨跌幅", "娑ㄨ穼骞�")),
                    net=_first_present(
                        row,
                        ("main_net_inflow", "主力净流入-净额", "涓诲姏鍑€娴佸叆-鍑€棰�"),
                    ),
                    ratio=_first_present(
                        row,
                        ("main_net_ratio", "主力净流入-净占比", "涓诲姏鍑€娴佸叆-鍑€鍗犳瘮"),
                    ),
                )
            )
        results.append(
            SourceResult(
                source="stock_individual_fund_flow",
                status="ok",
                as_of=curr_date,
                records=tuple(records),
            )
        )

    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start = (end_dt - timedelta(days=14)).strftime("%Y%m%d")
    end = end_dt.strftime("%Y%m%d")
    frame, error = _call_source(
        "stock_lhb_detail_em",
        lambda: ak.stock_lhb_detail_em(start_date=start, end_date=end),
    )
    if error:
        results.append(error)
    else:
        code_col = _find_column(frame, ("代码", "证券代码", "code"))
        if code_col is None:
            results.append(
                SourceResult(
                    source="stock_lhb_detail_em",
                    status="unavailable",
                    as_of=None,
                    error="missing code column",
                )
            )
        else:
            stock_rows = frame[frame[code_col].astype(str).str.zfill(6) == instrument.akshare_code]
            records = []
            for _, row in stock_rows.head(3).iterrows():
                records.append(
                    "date={date}; reason={reason}; net_buy={net_buy}".format(
                        date=_first_present(row, ("上榜日", "日期", "date")),
                        reason=_first_present(row, ("解读", "上榜原因", "原因", "reason")),
                        net_buy=_first_present(
                            row,
                            ("龙虎榜净买额", "净买额", "net_buy"),
                        ),
                    )
                )
            if not records:
                records = ["no Dragon-Tiger listing found in the 14-day lookback window"]
            results.append(SourceResult("stock_lhb_detail_em", "ok", curr_date, tuple(records)))

    source_name = (
        "stock_margin_detail_sse" if instrument.exchange == "shanghai" else "stock_margin_detail_szse"
    )
    margin_fn = (
        ak.stock_margin_detail_sse
        if instrument.exchange == "shanghai"
        else ak.stock_margin_detail_szse
    )
    frame, error = _call_source(source_name, lambda: margin_fn(date=end))
    if error:
        results.append(error)
    else:
        code_col = _find_column(frame, ("证券代码", "标的证券代码", "code"))
        if code_col is None:
            results.append(
                SourceResult(source_name, "unavailable", None, error="missing code column")
            )
        else:
            stock_rows = frame[frame[code_col].astype(str).str.zfill(6) == instrument.akshare_code]
            records = []
            for _, row in stock_rows.head(2).iterrows():
                records.append(
                    "financing_balance={fin}; securities_lending_balance={lend}".format(
                        fin=_first_present(
                            row,
                            ("融资余额", "融资余额(元)", "financing_balance"),
                        ),
                        lend=_first_present(
                            row,
                            ("融券余额", "融券余额(元)", "securities_lending_balance"),
                        ),
                    )
                )
            if not records:
                records = ["no margin detail row found for this stock on the requested date"]
            results.append(SourceResult(source_name, "ok", curr_date, tuple(records)))

    em_symbol = _eastmoney_symbol(instrument)
    frame, error = _call_source(
        "stock_hot_rank_latest_em",
        lambda: ak.stock_hot_rank_latest_em(symbol=em_symbol),
    )
    if error:
        results.append(error)
    else:
        row = frame.iloc[0]
        rank = _first_present(row, ("rank", "排名", "当前排名"))
        results.append(
            SourceResult(
                "stock_hot_rank_latest_em",
                "ok",
                curr_date,
                (f"rank={rank or 'unknown'}",),
            )
        )

    frame, error = _call_source(
        "stock_hot_keyword_em",
        lambda: ak.stock_hot_keyword_em(symbol=em_symbol),
    )
    if error:
        results.append(error)
    else:
        keyword_col = _find_column(frame, ("title", "关键词", "概念名称"))
        values = []
        if keyword_col is not None:
            values = [_string_value(v) for v in frame[keyword_col].head(5).tolist()]
            values = [value for value in values if value]
        record = "top_keywords=" + (", ".join(values) if values else "none")
        results.append(SourceResult("stock_hot_keyword_em", "ok", curr_date, (record,)))

    return results


def _collect_announcements(instrument, curr_date: str) -> list[SourceResult]:
    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    begin = (end_dt - timedelta(days=90)).strftime("%Y%m%d")
    end = end_dt.strftime("%Y%m%d")
    results: list[SourceResult] = []

    frame, error = _call_source(
        "stock_individual_notice_report",
        lambda: ak.stock_individual_notice_report(
            security=instrument.akshare_code,
            symbol="全部",
            begin_date=begin,
            end_date=end,
        ),
    )
    if error:
        results.append(error)
    else:
        important = ("业绩", "分红", "回购", "减持", "质押", "重组", "诉讼", "关联交易", "合同", "公告")
        records = []
        for _, row in frame.head(20).iterrows():
            title = _first_present(row, ("title", "公告标题", "标题"))
            date = _first_present(row, ("date", "公告时间", "公告日期"))
            if title and any(token in title for token in important):
                records.append(f"date={date}; title={title}")
            if len(records) >= 8:
                break
        if not records:
            records = ["no high-priority company announcements found in the lookback window"]
        results.append(SourceResult("stock_individual_notice_report", "ok", curr_date, tuple(records)))

    frame, error = _call_source(
        "stock_zh_a_disclosure_report_cninfo",
        lambda: ak.stock_zh_a_disclosure_report_cninfo(
            symbol=instrument.akshare_code,
            market="沪深京",
            keyword="",
            category="",
            start_date=(end_dt - timedelta(days=90)).strftime("%Y-%m-%d"),
            end_date=curr_date,
        ),
    )
    if error:
        results.append(error)
    else:
        records = []
        for _, row in frame.head(5).iterrows():
            records.append(
                "date={date}; title={title}".format(
                    date=_first_present(row, ("date", "公告日期", "披露日期")),
                    title=_first_present(row, ("title", "公告标题", "标题")),
                )
            )
        if not records:
            records = ["no cninfo disclosures found in the lookback window"]
        results.append(
            SourceResult(
                "stock_zh_a_disclosure_report_cninfo",
                "ok",
                curr_date,
                tuple(records),
            )
        )

    return results


def _collect_industry_policy(instrument, curr_date: str) -> list[SourceResult]:
    del instrument
    results: list[SourceResult] = []

    frame, error = _call_source(
        "stock_sector_fund_flow_rank",
        lambda: ak.stock_sector_fund_flow_rank(indicator="今日"),
    )
    if error:
        results.append(error)
    else:
        records = []
        for _, row in frame.head(5).iterrows():
            records.append(
                "sector={sector}; pct_change={pct}; net_inflow={net}".format(
                    sector=_first_present(row, ("sector", "名称", "板块名称")),
                    pct=_first_present(row, ("pct_change", "涨跌幅")),
                    net=_first_present(row, ("net_inflow", "主力净流入-净额", "净流入")),
                )
            )
        results.append(SourceResult("stock_sector_fund_flow_rank", "ok", curr_date, tuple(records)))

    frame, error = _call_source("stock_info_global_em", ak.stock_info_global_em)
    if error:
        results.append(error)
    else:
        records = []
        for _, row in frame.head(5).iterrows():
            records.append(
                "title={title}; source={source}".format(
                    title=_first_present(row, ("title", "标题", "新闻标题")),
                    source=_first_present(row, ("source", "来源", "文章来源")),
                )
            )
        results.append(SourceResult("stock_info_global_em", "ok", curr_date, tuple(records)))

    return results


def _categories_for_preset(preset: str) -> set[str]:
    return set(PRESET_CATEGORIES.get(preset, set()))


def get_china_a_enhancements_for_categories(
    ticker: str,
    curr_date: str,
    preset: str,
    categories: set[str],
) -> str:
    instrument = resolve_china_a_symbol(ticker)
    if instrument is None:
        return ""

    allowed = _categories_for_preset(preset) & set(categories)
    if not allowed:
        return ""

    cache_path = _cache_path(ticker, curr_date, preset, allowed)
    cached = _read_cache(cache_path)
    if cached is not None:
        return cached

    sections: list[tuple[str, list[SourceResult]]] = []
    if CATEGORY_FLOW_SENTIMENT in allowed:
        sections.append(("Fund flow and trading activity", _collect_flow_sentiment(instrument, curr_date)))
    if CATEGORY_ANNOUNCEMENTS in allowed:
        sections.append(("Announcements and disclosures", _collect_announcements(instrument, curr_date)))
    if CATEGORY_INDUSTRY_POLICY in allowed:
        sections.append(
            ("Industry, sector, and policy context", _collect_industry_policy(instrument, curr_date))
        )

    text = _format_snapshot(instrument.yahoo_symbol, curr_date, preset, sections)
    if text:
        _write_cache(cache_path, text)
    return text


def get_china_a_enhancements(ticker: str, curr_date: str, preset: str) -> str:
    return get_china_a_enhancements_for_categories(
        ticker=ticker,
        curr_date=curr_date,
        preset=preset,
        categories=_categories_for_preset(preset),
    )

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

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
MAX_SOURCE_LINE_CHARS = 360
SOURCE_LINE_PREFIXES = ("- Source:", "- Source unavailable:")
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class SourceResult:
    source: str
    status: str
    as_of: str | None
    records: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class SnapshotSection:
    title: str
    results: Sequence[SourceResult]
    status: str


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

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_wrapped)
    try:
        frame = future.result(timeout=timeout_seconds)
    except TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return None, SourceResult(
            source=source,
            status="unavailable",
            as_of=None,
            error=f"timed out after {timeout_seconds}s",
        )
    except Exception as exc:  # noqa: BLE001 - fail-open enrichment surface
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return None, SourceResult(
            source=source,
            status="unavailable",
            as_of=None,
            error=str(exc),
        )
    executor.shutdown(wait=False, cancel_futures=True)
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


def _profile_pairs(frame: pd.DataFrame) -> dict[str, str]:
    if frame.empty:
        return {}

    item_col = _find_column(frame, ("item", "name", "项目", "字段"))
    value_col = _find_column(frame, ("value", "值", "内容"))
    if item_col and value_col:
        pairs = {}
        for _, row in frame.iterrows():
            key = _string_value(row.get(item_col))
            value = _string_value(row.get(value_col))
            if key and value:
                pairs[key] = value
        return pairs

    row = frame.iloc[0]
    return {
        str(column): _string_value(row.get(column))
        for column in frame.columns
        if _string_value(row.get(column))
    }


def _profile_value(pairs: dict[str, str], names: tuple[str, ...]) -> str:
    normalized_names = tuple(name.lower() for name in names)
    for key, value in pairs.items():
        normalized_key = key.strip().lower()
        if any(name in normalized_key for name in normalized_names):
            return value
    return ""


def _split_concepts(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace("，", ",").replace("、", ",").replace(";", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


def _matches_any_term(value: str, terms: list[str]) -> bool:
    lowered = value.lower()
    return any(term.lower() in lowered or lowered in term.lower() for term in terms if term)


def _truncate_text(text: str, limit: int = MAX_SOURCE_LINE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_result(result: SourceResult) -> list[str]:
    if result.status == STATUS_OK:
        return [
            f"- Source: {result.source}; as_of: {result.as_of or 'unknown'}; "
            f"{_truncate_text(record)}"
            for record in result.records
        ]
    return [
        f"- Source unavailable: {result.source} "
        f"({_truncate_text(result.error or 'unknown error')})."
    ]


def _source_result_is_ok(result: SourceResult) -> bool:
    return result.status == STATUS_OK and bool(result.records)


def _section_status(results: Sequence[SourceResult]) -> str:
    ok_count = sum(1 for result in results if _source_result_is_ok(result))
    if ok_count == 0:
        return STATUS_FAILED
    if ok_count < len(results):
        return STATUS_PARTIAL
    return STATUS_OK


def _snapshot_status(sections: Sequence[SnapshotSection]) -> str:
    if not sections:
        return STATUS_FAILED
    statuses = {section.status for section in sections}
    if statuses == {STATUS_OK}:
        return STATUS_OK
    if statuses == {STATUS_FAILED}:
        return STATUS_FAILED
    return STATUS_PARTIAL


def _should_cache_snapshot(sections: Sequence[SnapshotSection]) -> bool:
    return _snapshot_status(sections) != STATUS_FAILED


def _section_source_line_limit(section_count: int) -> int:
    if section_count <= 1:
        return MAX_SOURCE_LINES
    return max(1, MAX_SOURCE_LINES // section_count)


def _format_snapshot(
    ticker: str,
    curr_date: str,
    preset: str,
    sections: Sequence[SnapshotSection],
) -> str:
    if not sections:
        return ""

    lines = [
        "## China A-share Enhancement Snapshot",
        f"Ticker: {ticker}",
        f"Analysis date: {curr_date}",
        f"Preset: {preset}",
        f"Overall status: {_snapshot_status(sections)}",
        "",
        "Use this source-labeled snapshot as supplemental China-local context.",
        "",
    ]
    section_source_limit = _section_source_line_limit(len(sections))
    for section in sections:
        lines.append(f"### {section.title}")
        lines.append(f"Section status: {section.status}")
        source_line_count = 0
        for result in section.results:
            for line in _format_result(result):
                if line.startswith(SOURCE_LINE_PREFIXES):
                    if source_line_count >= section_source_limit:
                        continue
                    source_line_count += 1
                lines.append(line)
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
        results.append(SourceResult("stock_individual_fund_flow", "ok", curr_date, tuple(records)))

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
        code_col = _find_column(frame, ("code", "代码", "证券代码"))
        if code_col is None:
            results.append(SourceResult("stock_lhb_detail_em", "unavailable", None, error="missing code column"))
        else:
            stock_rows = frame[frame[code_col].astype(str).str.zfill(6) == instrument.akshare_code]
            records = []
            for _, row in stock_rows.head(3).iterrows():
                records.append(
                    "date={date}; reason={reason}; net_buy={net_buy}".format(
                        date=_first_present(row, ("date", "上榜日", "日期")),
                        reason=_first_present(row, ("reason", "解读", "上榜原因", "原因")),
                        net_buy=_first_present(row, ("net_buy", "龙虎榜净买额", "净买额")),
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
        code_col = _find_column(frame, ("code", "证券代码", "标的证券代码"))
        if code_col is None:
            results.append(SourceResult(source_name, "unavailable", None, error="missing code column"))
        else:
            stock_rows = frame[frame[code_col].astype(str).str.zfill(6) == instrument.akshare_code]
            records = []
            for _, row in stock_rows.head(2).iterrows():
                records.append(
                    "financing_balance={fin}; securities_lending_balance={lend}".format(
                        fin=_first_present(
                            row,
                            ("financing_balance", "融资余额", "铻嶈祫浣欓", "融资余额(元)", "铻嶈祫浣欓(鍏�"),
                        ),
                        lend=_first_present(
                            row,
                            (
                                "securities_lending_balance",
                                "融券余额",
                                "铻嶅埜浣欓",
                                "融券余额(元)",
                                "铻嶅埜浣欓(鍏�",
                            ),
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
        rank = _first_present(row, ("rank", "排名", "当前排名", "鎺掑悕", "褰撳墠鎺掑悕"))
        results.append(SourceResult("stock_hot_rank_latest_em", "ok", curr_date, (f"rank={rank or 'unknown'}",)))

    frame, error = _call_source(
        "stock_hot_keyword_em",
        lambda: ak.stock_hot_keyword_em(symbol=em_symbol),
    )
    if error:
        results.append(error)
    else:
        keyword_col = _find_column(frame, ("title", "关键词", "概念名称", "鍏抽敭璇�", "姒傚康鍚嶇О"))
        values = []
        if keyword_col is not None:
            values = [_string_value(v) for v in frame[keyword_col].head(5).tolist()]
            values = [value for value in values if value]
        results.append(
            SourceResult(
                "stock_hot_keyword_em",
                "ok",
                curr_date,
                (f"top_keywords={', '.join(values) if values else 'none'}",),
            )
        )

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
        important = (
            "业绩",
            "分红",
            "回购",
            "减持",
            "质押",
            "重组",
            "诉讼",
            "关联交易",
            "合同",
            "公告",
            "earnings",
            "dividend",
            "buyback",
            "stake reduction",
            "pledge",
            "restructuring",
            "litigation",
            "related party",
            "contract",
            "announcement",
        )
        records = []
        for _, row in frame.head(20).iterrows():
            title = _first_present(row, ("title", "公告标题", "标题", "鍏憡鏍囬", "鏍囬"))
            date = _first_present(row, ("date", "公告时间", "公告日期", "鍏憡鏃堕棿", "鍏憡鏃ユ湡"))
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
                    date=_first_present(row, ("date", "公告日期", "披露日期", "鍏憡鏃ユ湡", "鎶湶鏃ユ湡")),
                    title=_first_present(row, ("title", "公告标题", "标题", "鍏憡鏍囬", "鏍囬")),
                )
            )
        if not records:
            records = ["no cninfo disclosures found in the lookback window"]
        results.append(SourceResult("stock_zh_a_disclosure_report_cninfo", "ok", curr_date, tuple(records)))

    return results


def _collect_industry_policy(instrument, curr_date: str) -> list[SourceResult]:
    results: list[SourceResult] = []
    profile_terms: list[str] = []

    frame, error = _call_source(
        "stock_individual_info_em",
        lambda: ak.stock_individual_info_em(symbol=instrument.akshare_code),
    )
    if error:
        results.append(error)
    else:
        pairs = _profile_pairs(frame)
        industry = _profile_value(
            pairs,
            ("industry", "sector", "行业", "所属行业", "板块", "所属板块"),
        )
        concepts = _split_concepts(
            _profile_value(
                pairs,
                ("concept", "concepts", "概念", "所属概念", "概念板块"),
            )
        )
        profile_terms = [term for term in [industry, *concepts] if term]
        records = []
        if industry:
            records.append(f"stock_code={instrument.akshare_code}; industry={industry}")
        if concepts:
            records.append(
                f"stock_code={instrument.akshare_code}; concepts={', '.join(concepts[:5])}"
            )
        if not records:
            records = [
                f"stock_code={instrument.akshare_code}; no industry/concept fields returned"
            ]
        results.append(SourceResult("stock_individual_info_em", "ok", curr_date, tuple(records)))
    has_profile_terms = bool(profile_terms)

    frame, error = _call_source(
        "stock_sector_fund_flow_rank",
        lambda: ak.stock_sector_fund_flow_rank(indicator="今日"),
    )
    if error:
        results.append(error)
    else:
        records = []
        if has_profile_terms:
            for _, row in frame.head(30).iterrows():
                sector = _first_present(row, ("sector", "名称", "板块名称", "鍚嶇О", "鏉垮潡鍚嶇О"))
                if not _matches_any_term(sector, profile_terms):
                    continue
                records.append(
                    "sector={sector}; pct_change={pct}; net_inflow={net}".format(
                        sector=sector,
                        pct=_first_present(row, ("pct_change", "涨跌幅", "娑ㄨ穼骞�")),
                        net=_first_present(
                            row,
                            ("net_inflow", "主力净流入-净额", "净流入", "涓诲姏鍑€娴佸叆-鍑€棰�", "鍑€娴佸叆"),
                        ),
                    )
                )
                if len(records) >= 3:
                    break
        if not records:
            if has_profile_terms:
                records = [
                    "no sector fund-flow row matched ticker industry/concepts: "
                    + ", ".join(profile_terms[:5])
                ]
            else:
                records = [
                    "no ticker-specific industry/concept fields available to filter sector fund flow"
                ]
        results.append(SourceResult("stock_sector_fund_flow_rank", "ok", curr_date, tuple(records)))

    frame, error = _call_source("stock_info_global_em", ak.stock_info_global_em)
    if error:
        results.append(error)
    else:
        records = []
        if has_profile_terms:
            for _, row in frame.head(30).iterrows():
                title = _first_present(row, ("title", "标题", "新闻标题", "鏍囬", "鏂伴椈鏍囬"))
                if not _matches_any_term(title, profile_terms):
                    continue
                records.append(
                    "title={title}; source={source}".format(
                        title=title,
                        source=_first_present(row, ("source", "来源", "文章来源", "鏉ユ簮", "鏂囩珷鏉ユ簮")),
                    )
                )
                if len(records) >= 3:
                    break
        if not records:
            if has_profile_terms:
                records = [
                    "no policy/industry headline matched ticker industry/concepts: "
                    + ", ".join(profile_terms[:5])
                ]
            else:
                records = [
                    "no ticker-specific industry/concept fields available to filter policy headlines"
                ]
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

    sections: list[SnapshotSection] = []

    def add_section(title: str, results: list[SourceResult]) -> None:
        sections.append(
            SnapshotSection(
                title=title,
                results=tuple(results),
                status=_section_status(results),
            )
        )

    if CATEGORY_FLOW_SENTIMENT in allowed:
        add_section("Fund flow and trading activity", _collect_flow_sentiment(instrument, curr_date))
    if CATEGORY_ANNOUNCEMENTS in allowed:
        add_section("Announcements and disclosures", _collect_announcements(instrument, curr_date))
    if CATEGORY_INDUSTRY_POLICY in allowed:
        add_section(
            "Industry, sector, and policy context",
            _collect_industry_policy(instrument, curr_date),
        )

    text = _format_snapshot(instrument.yahoo_symbol, curr_date, preset, sections)
    if text and _should_cache_snapshot(sections):
        _write_cache(cache_path, text)
    return text


def get_china_a_enhancements(ticker: str, curr_date: str, preset: str) -> str:
    return get_china_a_enhancements_for_categories(
        ticker=ticker,
        curr_date=curr_date,
        preset=preset,
        categories=_categories_for_preset(preset),
    )

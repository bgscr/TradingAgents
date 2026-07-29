"""PROTOTYPE ONLY: execute live provider qualification calls into scratch JSON."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import io
import json
import math
import os
import re
import time
from pathlib import Path

import pandas as pd


AS_OF = pd.Timestamp("2026-07-29")
START_DATE = "20210101"
END_DATE = "20260729"
SYMBOLS = [
    {
        "normalized": "600895.SS",
        "requested": "600895.SH",
        "code": "600895",
        "exchange": "SH",
        "sina": "sh600895",
        "baostock": "sh.600895",
        "tushare": "600895.SH",
        "yahoo": "600895.SS",
        "role": "recent non-financial Shanghai run",
    },
    {
        "normalized": "601328.SS",
        "requested": "601328.SH",
        "code": "601328",
        "exchange": "SH",
        "sina": "sh601328",
        "baostock": "sh.601328",
        "tushare": "601328.SH",
        "yahoo": "601328.SS",
        "role": "recent bank / exceptional company taxonomy",
    },
    {
        "normalized": "000651.SZ",
        "requested": "000651.SZ",
        "code": "000651",
        "exchange": "SZ",
        "sina": "sz000651",
        "baostock": "sz.000651",
        "tushare": "000651.SZ",
        "yahoo": "000651.SZ",
        "role": "mature Shenzhen non-financial",
    },
    {
        "normalized": "301589.SZ",
        "requested": "301589.SZ",
        "code": "301589",
        "exchange": "SZ",
        "sina": "sz301589",
        "baostock": "sz.301589",
        "tushare": "301589.SZ",
        "yahoo": "301589.SZ",
        "role": "recent-listing non-financial / short history",
    },
]

CAPABILITIES = [
    "daily_ohlcv",
    "raw_and_adjusted_prices",
    "adjustment_factor_history",
    "suspension_trading_status",
    "name_st_delisting_history",
    "annual_balance_sheet",
    "quarterly_balance_sheet",
    "annual_income_statement",
    "quarterly_income_statement",
    "annual_cash_flow",
    "quarterly_cash_flow",
    "financial_indicators_ratios",
    "announcement_first_publication_dates",
    "restatement_update_identifiers",
    "units_currency_scope_company_type",
]

EMPTY_STRINGS = {"", "--", "none", "null", "nan", "nat", "n/a"}
META_FIELDS = {
    "ts_code", "code", "date", "trade_date", "end_date", "ann_date", "f_ann_date",
    "report_type", "comp_type", "update_flag", "statDate", "pubDate", "报告日", "报告日期",
    "公告日期", "截止日期", "股票代码", "证券代码", "币种", "单位",
}


class Recorder:
    def __init__(self, provider: str, version: str, run: int):
        self.provider = provider
        self.version = version
        self.run = run
        self.records = []
        self._call_index = 0

    def call(self, symbol: dict, endpoint: str, fn):
        self._call_index += 1
        call_id = f"{self.provider}:r{self.run}:{self._call_index}:{symbol['normalized']}:{endpoint}"
        started = time.perf_counter()
        try:
            value = fn()
            return value, None, round((time.perf_counter() - started) * 1000, 1), call_id
        except Exception as exc:  # qualification must capture all SDK failures
            return None, failure(exc), round((time.perf_counter() - started) * 1000, 1), call_id

    def add(
        self,
        symbol: dict,
        capability: str,
        *,
        endpoint: str,
        called: bool,
        status: str,
        analysis: dict | None = None,
        latency_ms: float = 0.0,
        typed_failure: str | None = None,
        failure_detail: str | None = None,
        permission_requirement: str = "keyless public endpoint",
        point_requirement: str = "not applicable",
        native_symbol: str | None = None,
        unit: str = "not supplied",
        currency: str = "not supplied",
        consolidation_scope: str = "not supplied",
        company_type: str = "not supplied",
        call_ids: list[str] | None = None,
        notes: str = "",
    ):
        analysis = analysis or blank_analysis()
        self.records.append(
            {
                "provider": self.provider,
                "provider_version": self.version,
                "run": self.run,
                "symbol_role": symbol["role"],
                "requested_symbol": symbol["requested"],
                "native_symbol": native_symbol or symbol["code"],
                "normalized_symbol": symbol["normalized"],
                "capability": capability,
                "called": called,
                "endpoint": endpoint,
                "permission_requirement": permission_requirement,
                "point_requirement": point_requirement,
                "status": status,
                **analysis,
                "unit": unit,
                "currency": currency,
                "consolidation_scope": consolidation_scope,
                "company_type": company_type,
                "latency_ms": latency_ms,
                "typed_failure": typed_failure,
                "failure_detail": failure_detail,
                "call_ids": call_ids or [],
                "notes": notes,
            }
        )

    def unsupported(self, symbol, capability, endpoint="none", reason="NO_PROVIDER_ENDPOINT"):
        self.add(
            symbol,
            capability,
            endpoint=endpoint,
            called=False,
            status="unavailable",
            typed_failure=reason,
            failure_detail="Provider SDK exposes no qualifying endpoint for this capability.",
        )


def sanitize(message: str) -> str:
    token = os.environ.get("TUSHARE_TOKEN", "")
    if token:
        message = message.replace(token, "<redacted>")
    return re.sub(r"(?i)(token|api[_ -]?key)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", message)[:1000]


def failure(exc: Exception) -> dict:
    detail = sanitize(f"{type(exc).__name__}: {exc}")
    lower = detail.lower()
    if any(text in detail for text in ("权限", "积分")) or "permission" in lower or "forbidden" in lower:
        kind = "PERMISSION_DENIED"
    elif "rate" in lower or "频率" in detail or "每分钟" in detail or "too many" in lower:
        kind = "RATE_LIMITED"
    elif "timeout" in lower or "timed out" in lower:
        kind = "TIMEOUT"
    elif "connection" in lower or "network" in lower or "disconnect" in lower or "ssl" in lower:
        kind = "NETWORK_ERROR"
    elif "no data" in lower or "empty" in lower or "没有数据" in detail:
        kind = "NO_DATA"
    else:
        kind = "PROVIDER_ERROR"
    points = re.findall(r"(?:至少|需要|达到|require[^0-9]{0,15})([0-9,]{3,})\s*(?:积分|points?)", detail, re.I)
    return {
        "typed_failure": kind,
        "failure_detail": detail,
        "point_requirement": points[0].replace(",", "") if points else "not stated by API error",
    }


def blank_analysis():
    return {
        "periods": [],
        "periods_returned": 0,
        "usable_non_sparse_periods": 0,
        "field_count": 0,
        "missing_field_ratio": 1.0,
        "announcement_date_available": False,
        "first_publication_date_available": False,
        "restatement_identifier_available": False,
    }


def as_frame(value) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.DataFrame(value)


def _present(value) -> bool:
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() not in EMPTY_STRINGS


def find_date_column(frame: pd.DataFrame) -> str | None:
    candidates = [
        "end_date", "报告日", "报告日期", "截止日期", "statDate", "trade_date", "date", "Date", "Datetime", "日期",
        "suspend_date", "start_date", "changeDate", "dividOperateDate",
    ]
    names = {str(col): col for col in frame.columns}
    for candidate in candidates:
        if candidate in names:
            return names[candidate]
    return None


def analyze_frame(frame, frequency: str = "all", max_periods: int | None = None) -> dict:
    frame = as_frame(frame)
    if frame.empty:
        return blank_analysis()
    frame.columns = [str(col) for col in frame.columns]
    date_col = find_date_column(frame)
    working = frame.copy()
    if date_col is not None:
        dates = pd.to_datetime(working[date_col].astype(str), errors="coerce")
        try:
            dates = dates.dt.tz_localize(None)
        except (AttributeError, TypeError):
            pass
        working = working.assign(_period=dates)
        working = working[working["_period"].notna() & (working["_period"] <= AS_OF)]
        if frequency == "annual":
            working = working[(working["_period"].dt.month == 12) & (working["_period"].dt.day == 31)]
        working = working.sort_values("_period", ascending=False).drop_duplicates("_period")
        if max_periods:
            working = working.head(max_periods)
        periods = [item.strftime("%Y-%m-%d") for item in working["_period"]]
    else:
        periods = []
        if max_periods:
            working = working.head(max_periods)

    metric_cols = [
        col for col in working.columns
        if col not in META_FIELDS and col != "_period" and not any(token in col.lower() for token in ("ann_date", "report_type", "update_flag"))
    ]
    if not metric_cols:
        metric_cols = [col for col in working.columns if col != "_period"]
    total_cells = max(1, len(working) * len(metric_cols))
    missing = sum(not _present(value) for col in metric_cols for value in working[col])
    missing_ratio = missing / total_cells
    usable = 0
    for _, row in working.iterrows():
        present = sum(_present(row[col]) for col in metric_cols)
        row_ratio = 1.0 - present / max(1, len(metric_cols))
        if present >= min(10, max(3, math.ceil(len(metric_cols) * 0.25))) and row_ratio <= 0.50:
            usable += 1
    ann_cols = [col for col in working.columns if str(col) in {"ann_date", "公告日期", "pubDate"}]
    first_cols = [col for col in working.columns if str(col) in {"f_ann_date", "首次公告日期", "首次披露日期"}]
    update_cols = [col for col in working.columns if str(col) in {"update_flag", "update_id", "更新标识", "修订标识"}]
    return {
        "periods": periods if periods else ([f"rows:{len(working)}"] if len(working) else []),
        "periods_returned": len(working),
        "usable_non_sparse_periods": usable,
        "field_count": len(metric_cols),
        "missing_field_ratio": round(missing_ratio, 4),
        "announcement_date_available": any(any(_present(v) for v in working[col]) for col in ann_cols),
        "first_publication_date_available": any(any(_present(v) for v in working[col]) for col in first_cols),
        "restatement_identifier_available": any(any(_present(v) for v in working[col]) for col in update_cols),
    }


def merge_frames(frames):
    valid = [as_frame(frame) for frame in frames if frame is not None and not as_frame(frame).empty]
    return pd.concat(valid, ignore_index=True, sort=False) if valid else pd.DataFrame()


def add_failure_record(rec, symbol, capability, endpoint, err, latency, call_ids, **kwargs):
    point_requirement = kwargs.pop("point_requirement", err["point_requirement"])
    rec.add(
        symbol,
        capability,
        endpoint=endpoint,
        called=True,
        status="unavailable",
        latency_ms=latency,
        typed_failure=err["typed_failure"],
        failure_detail=err["failure_detail"],
        point_requirement=point_requirement,
        call_ids=call_ids,
        **kwargs,
    )


def add_df_record(rec, symbol, capability, endpoint, frame, latency, call_ids, *, frequency="all", max_periods=None, status_override=None, **kwargs):
    analysis = analyze_frame(frame, frequency=frequency, max_periods=max_periods)
    status = status_override or ("available" if analysis["periods_returned"] else "unavailable")
    typed_failure = kwargs.pop("typed_failure", "NO_DATA" if status == "unavailable" else None)
    failure_detail = kwargs.pop("failure_detail", "Endpoint returned no rows." if status == "unavailable" else None)
    rec.add(
        symbol,
        capability,
        endpoint=endpoint,
        called=True,
        status=status,
        analysis=analysis,
        latency_ms=latency,
        typed_failure=typed_failure,
        failure_detail=failure_detail,
        call_ids=call_ids,
        **kwargs,
    )


def probe_akshare(rec: Recorder):
    import akshare as ak

    global_symbol = SYMBOLS[0]
    stopped, stopped_err, stopped_ms, stopped_id = rec.call(global_symbol, "stock_zh_a_stop_em", ak.stock_zh_a_stop_em)
    st, st_err, st_ms, st_id = rec.call(global_symbol, "stock_zh_a_st_em", ak.stock_zh_a_st_em)
    for symbol in SYMBOLS:
        native = symbol["code"]
        raw, raw_err, raw_ms, raw_id = rec.call(
            symbol, "stock_zh_a_hist(raw)",
            lambda s=symbol: ak.stock_zh_a_hist(symbol=s["code"], period="daily", start_date=START_DATE, end_date=END_DATE, adjust="", timeout=15),
        )
        qfq, qfq_err, qfq_ms, qfq_id = rec.call(
            symbol, "stock_zh_a_hist(qfq)",
            lambda s=symbol: ak.stock_zh_a_hist(symbol=s["code"], period="daily", start_date=START_DATE, end_date=END_DATE, adjust="qfq", timeout=15),
        )
        if qfq_err:
            add_failure_record(rec, symbol, "daily_ohlcv", "stock_zh_a_hist(adjust=qfq)", qfq_err, qfq_ms, [qfq_id], native_symbol=native)
        else:
            add_df_record(rec, symbol, "daily_ohlcv", "stock_zh_a_hist(adjust=qfq)", qfq, qfq_ms, [qfq_id], native_symbol=native, currency="CNY inferred from instrument; not payload", unit="price CNY / volume shares inferred")
        if raw_err or qfq_err:
            err = raw_err or qfq_err
            add_failure_record(rec, symbol, "raw_and_adjusted_prices", "stock_zh_a_hist(raw,qfq)", err, raw_ms + qfq_ms, [raw_id, qfq_id], native_symbol=native)
        else:
            combined = merge_frames([raw.assign(_basis="raw"), qfq.assign(_basis="qfq")])
            add_df_record(rec, symbol, "raw_and_adjusted_prices", "stock_zh_a_hist(adjust='',qfq)", combined, raw_ms + qfq_ms, [raw_id, qfq_id], native_symbol=native, currency="CNY inferred from instrument; not payload", unit="price CNY / volume shares inferred")

        factors, factor_err, factor_ms, factor_id = rec.call(
            symbol, "stock_zh_a_daily(qfq-factor)",
            lambda s=symbol: ak.stock_zh_a_daily(symbol=s["sina"], start_date=START_DATE, end_date=END_DATE, adjust="qfq-factor"),
        )
        if factor_err:
            add_failure_record(rec, symbol, "adjustment_factor_history", "stock_zh_a_daily(adjust=qfq-factor)", factor_err, factor_ms, [factor_id], native_symbol=symbol["sina"])
        else:
            add_df_record(rec, symbol, "adjustment_factor_history", "stock_zh_a_daily(adjust=qfq-factor)", factors, factor_ms, [factor_id], native_symbol=symbol["sina"], unit="dimensionless factor")

        if stopped_err:
            add_failure_record(rec, symbol, "suspension_trading_status", "stock_zh_a_stop_em", stopped_err, stopped_ms, [stopped_id], native_symbol=native)
        else:
            add_df_record(rec, symbol, "suspension_trading_status", "stock_zh_a_stop_em", stopped, stopped_ms, [stopped_id], native_symbol=native, status_override="partial", notes="Current suspended list only; no dated per-symbol status history.")

        changes, change_err, change_ms, change_id = rec.call(
            symbol, "stock_info_change_name", lambda s=symbol: ak.stock_info_change_name(symbol=s["code"])
        )
        if change_err and st_err:
            add_failure_record(rec, symbol, "name_st_delisting_history", "stock_info_change_name + stock_zh_a_st_em", change_err, change_ms + st_ms, [change_id, st_id], native_symbol=native)
        else:
            combined = merge_frames([changes, st])
            add_df_record(rec, symbol, "name_st_delisting_history", "stock_info_change_name + stock_zh_a_st_em", combined, change_ms + st_ms, [change_id, st_id], native_symbol=native, status_override="partial", notes="Name history plus current ST universe; no complete delisting event history.")

        statement_frames = {}
        statement_calls = []
        for label, chinese in (("balance", "资产负债表"), ("income", "利润表"), ("cash", "现金流量表")):
            frame, err, elapsed, call_id = rec.call(
                symbol, f"stock_financial_report_sina({chinese})",
                lambda s=symbol, c=chinese: ak.stock_financial_report_sina(stock=s["sina"], symbol=c),
            )
            statement_frames[label] = (frame, err, elapsed, call_id, chinese)
            statement_calls.append(call_id)
        capability_map = {
            "balance": ("annual_balance_sheet", "quarterly_balance_sheet"),
            "income": ("annual_income_statement", "quarterly_income_statement"),
            "cash": ("annual_cash_flow", "quarterly_cash_flow"),
        }
        for label, caps in capability_map.items():
            frame, err, elapsed, call_id, chinese = statement_frames[label]
            for cap, freq, limit in ((caps[0], "annual", 5), (caps[1], "all", 8)):
                if err:
                    add_failure_record(rec, symbol, cap, f"stock_financial_report_sina({chinese})", err, elapsed, [call_id], native_symbol=symbol["sina"])
                else:
                    add_df_record(rec, symbol, cap, f"stock_financial_report_sina({chinese})", frame, elapsed, [call_id], frequency=freq, max_periods=limit, native_symbol=symbol["sina"], currency="CNY inferred; payload metadata inspected", unit="provider field-dependent", consolidation_scope="provider field-dependent")

        ratios, ratio_err, ratio_ms, ratio_id = rec.call(
            symbol, "stock_financial_analysis_indicator",
            lambda s=symbol: ak.stock_financial_analysis_indicator(symbol=s["code"], start_year="2021"),
        )
        if ratio_err:
            add_failure_record(rec, symbol, "financial_indicators_ratios", "stock_financial_analysis_indicator", ratio_err, ratio_ms, [ratio_id], native_symbol=native)
        else:
            add_df_record(rec, symbol, "financial_indicators_ratios", "stock_financial_analysis_indicator", ratios, ratio_ms, [ratio_id], max_periods=8, native_symbol=native, unit="mixed ratios and amounts")

        combined_statements = merge_frames([item[0] for item in statement_frames.values()])
        metadata_analysis = analyze_frame(combined_statements, max_periods=13)
        metadata_ms = sum(item[2] for item in statement_frames.values())
        rec.add(symbol, "announcement_first_publication_dates", endpoint="stock_financial_report_sina(three statements)", called=True, status="available" if metadata_analysis["announcement_date_available"] else "partial", analysis=metadata_analysis, latency_ms=metadata_ms, native_symbol=symbol["sina"], call_ids=statement_calls, notes="Availability measured from returned announcement fields.")
        rec.add(symbol, "restatement_update_identifiers", endpoint="stock_financial_report_sina(three statements)", called=True, status="available" if metadata_analysis["restatement_identifier_available"] else "unavailable", analysis=metadata_analysis, latency_ms=metadata_ms, native_symbol=symbol["sina"], typed_failure=None if metadata_analysis["restatement_identifier_available"] else "METADATA_NOT_SUPPLIED", call_ids=statement_calls)
        meta_cols = {str(col) for col in combined_statements.columns}
        has_meta = bool(meta_cols & {"币种", "单位", "合并类型", "报表类型", "公司类型"})
        rec.add(symbol, "units_currency_scope_company_type", endpoint="stock_financial_report_sina(three statements)", called=True, status="partial" if has_meta else "unavailable", analysis=metadata_analysis, latency_ms=metadata_ms, native_symbol=symbol["sina"], typed_failure=None if has_meta else "METADATA_NOT_SUPPLIED", unit="only if present in payload", currency="only if present in payload", consolidation_scope="only if present in payload", company_type="not supplied", call_ids=statement_calls)


def _bs_frame(result):
    if getattr(result, "error_code", "0") != "0":
        raise RuntimeError(f"BaoStock {result.error_code}: {result.error_msg}")
    fields = list(getattr(result, "fields", []))
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=fields)


def probe_baostock(rec: Recorder):
    import baostock as bs

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        err = failure(RuntimeError(f"BaoStock login {login.error_code}: {login.error_msg}"))
        for symbol in SYMBOLS:
            for capability in CAPABILITIES:
                add_failure_record(rec, symbol, capability, "bs.login", err, 0.0, [], permission_requirement="anonymous BaoStock session")
        return
    financial_periods = sorted({
        (2021, 4), (2022, 4), (2023, 4), (2024, 4), (2025, 4),
        (2024, 2), (2024, 3), (2025, 1), (2025, 2), (2025, 3), (2026, 1), (2026, 2),
    })
    try:
        for symbol in SYMBOLS:
            native = symbol["baostock"]
            fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
            raw, raw_err, raw_ms, raw_id = rec.call(symbol, "query_history_k_data_plus(raw)", lambda s=symbol: _bs_frame(bs.query_history_k_data_plus(s["baostock"], fields, start_date="2021-01-01", end_date="2026-07-29", frequency="d", adjustflag="3")))
            qfq, qfq_err, qfq_ms, qfq_id = rec.call(symbol, "query_history_k_data_plus(qfq)", lambda s=symbol: _bs_frame(bs.query_history_k_data_plus(s["baostock"], fields, start_date="2021-01-01", end_date="2026-07-29", frequency="d", adjustflag="2")))
            if qfq_err:
                add_failure_record(rec, symbol, "daily_ohlcv", "query_history_k_data_plus(adjustflag=2)", qfq_err, qfq_ms, [qfq_id], native_symbol=native)
            else:
                add_df_record(rec, symbol, "daily_ohlcv", "query_history_k_data_plus(adjustflag=2)", qfq, qfq_ms, [qfq_id], native_symbol=native, currency="CNY inferred from market", unit="price CNY / volume shares")
            if raw_err or qfq_err:
                add_failure_record(rec, symbol, "raw_and_adjusted_prices", "query_history_k_data_plus(adjustflag=3,2)", raw_err or qfq_err, raw_ms + qfq_ms, [raw_id, qfq_id], native_symbol=native)
            else:
                add_df_record(rec, symbol, "raw_and_adjusted_prices", "query_history_k_data_plus(adjustflag=3,2)", merge_frames([raw.assign(_basis="raw"), qfq.assign(_basis="qfq")]), raw_ms + qfq_ms, [raw_id, qfq_id], native_symbol=native, currency="CNY inferred from market", unit="price CNY / volume shares")

            factors, factor_err, factor_ms, factor_id = rec.call(symbol, "query_adjust_factor", lambda s=symbol: _bs_frame(bs.query_adjust_factor(s["baostock"], start_date="2021-01-01", end_date="2026-07-29")))
            if factor_err:
                add_failure_record(rec, symbol, "adjustment_factor_history", "query_adjust_factor", factor_err, factor_ms, [factor_id], native_symbol=native)
            else:
                add_df_record(rec, symbol, "adjustment_factor_history", "query_adjust_factor", factors, factor_ms, [factor_id], native_symbol=native, unit="dimensionless factors")

            if raw_err:
                add_failure_record(rec, symbol, "suspension_trading_status", "query_history_k_data_plus(tradestatus,isST)", raw_err, raw_ms, [raw_id], native_symbol=native)
            else:
                add_df_record(rec, symbol, "suspension_trading_status", "query_history_k_data_plus(tradestatus,isST)", raw, raw_ms, [raw_id], native_symbol=native, notes="Dated tradestatus and isST fields returned with raw history.")

            basic, basic_err, basic_ms, basic_id = rec.call(symbol, "query_stock_basic", lambda s=symbol: _bs_frame(bs.query_stock_basic(code=s["baostock"])))
            if basic_err:
                add_failure_record(rec, symbol, "name_st_delisting_history", "query_stock_basic", basic_err, basic_ms, [basic_id], native_symbol=native)
            else:
                add_df_record(rec, symbol, "name_st_delisting_history", "query_stock_basic", basic, basic_ms, [basic_id], native_symbol=native, status_override="partial", notes="IPO/out date and current status/name; no full name/ST event history.")

            finance = {}
            functions = {
                "profit": bs.query_profit_data,
                "operation": bs.query_operation_data,
                "growth": bs.query_growth_data,
                "balance": bs.query_balance_data,
                "cash_flow": bs.query_cash_flow_data,
                "dupont": bs.query_dupont_data,
            }
            for label, func in functions.items():
                parts, errors, elapsed_total, ids = [], [], 0.0, []
                for year, quarter in financial_periods:
                    frame, err, elapsed, call_id = rec.call(symbol, f"{func.__name__}({year}Q{quarter})", lambda s=symbol, f=func, y=year, q=quarter: _bs_frame(f(s["baostock"], year=y, quarter=q)))
                    elapsed_total += elapsed
                    ids.append(call_id)
                    if err:
                        errors.append(err)
                    elif frame is not None and not frame.empty:
                        parts.append(frame)
                finance[label] = (merge_frames(parts), errors, elapsed_total, ids, func.__name__)

            mapping = {
                "balance": ("annual_balance_sheet", "quarterly_balance_sheet"),
                "profit": ("annual_income_statement", "quarterly_income_statement"),
                "cash_flow": ("annual_cash_flow", "quarterly_cash_flow"),
            }
            for label, caps in mapping.items():
                frame, errors, elapsed, ids, endpoint = finance[label]
                for cap, freq, limit in ((caps[0], "annual", 5), (caps[1], "all", 8)):
                    if frame.empty and errors:
                        add_failure_record(rec, symbol, cap, endpoint, errors[0], elapsed, ids, native_symbol=native)
                    else:
                        add_df_record(rec, symbol, cap, endpoint, frame, elapsed, ids, frequency=freq, max_periods=limit, native_symbol=native, status_override="partial" if not frame.empty else "unavailable", typed_failure="CAPABILITY_PARTIAL_METRICS_NOT_FULL_STATEMENT" if not frame.empty else "NO_DATA", notes="BaoStock endpoint returns selected metrics/ratios, not a complete native statement.", currency="CNY inferred where amounts appear", unit="mixed ratios / provider-defined", consolidation_scope="not supplied")

            all_finance = merge_frames([item[0] for item in finance.values()])
            all_ids = [call_id for item in finance.values() for call_id in item[3]]
            total_ms = sum(item[2] for item in finance.values())
            add_df_record(rec, symbol, "financial_indicators_ratios", "query_profit/operation/growth/balance/cash_flow/dupont_data", all_finance, total_ms, all_ids, max_periods=13, native_symbol=native, status_override="available" if not all_finance.empty else "unavailable", unit="mixed ratios / provider-defined", currency="CNY inferred where amounts appear")
            analysis = analyze_frame(all_finance, max_periods=13)
            rec.add(symbol, "announcement_first_publication_dates", endpoint="six BaoStock financial metric endpoints", called=True, status="available" if analysis["announcement_date_available"] else "unavailable", analysis=analysis, latency_ms=total_ms, native_symbol=native, typed_failure=None if analysis["announcement_date_available"] else "METADATA_NOT_SUPPLIED", call_ids=all_ids)
            rec.add(symbol, "restatement_update_identifiers", endpoint="six BaoStock financial metric endpoints", called=True, status="unavailable", analysis=analysis, latency_ms=total_ms, native_symbol=native, typed_failure="METADATA_NOT_SUPPLIED", call_ids=all_ids)
            rec.add(symbol, "units_currency_scope_company_type", endpoint="six BaoStock financial metric endpoints + query_stock_basic", called=True, status="partial", analysis=analysis, latency_ms=total_ms + basic_ms, native_symbol=native, unit="mixed provider-defined; not explicit", currency="CNY inferred, not explicit", consolidation_scope="not supplied", company_type="security type only from query_stock_basic", call_ids=all_ids + [basic_id])
    finally:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bs.logout()


TUSHARE_POINTS = {
    "daily": ">=120 documented baseline",
    "adj_factor": ">=2000 documented baseline",
    "suspend_d": ">=2000 documented baseline",
    "namechange": "provider entitlement applies",
    "stock_basic": "provider entitlement applies",
    "balancesheet": ">=2000 documented baseline for single-stock history",
    "income": ">=2000 documented baseline for single-stock history",
    "cashflow": ">=2000 documented baseline for single-stock history",
    "fina_indicator": ">=2000 documented baseline for single-stock history",
}


def probe_tushare(rec: Recorder):
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        for symbol in SYMBOLS:
            for capability in CAPABILITIES:
                rec.add(symbol, capability, endpoint="not called", called=False, status="unavailable", permission_requirement="current TUSHARE_TOKEN required", point_requirement="unknown", typed_failure="CREDENTIAL_UNAVAILABLE", failure_detail="No non-empty TUSHARE_TOKEN was available.", native_symbol=symbol["tushare"])
        return
    pro = ts.pro_api(token)
    for symbol in SYMBOLS:
        native = symbol["tushare"]
        calls = {}
        specs = {
            "daily": lambda s=symbol: pro.daily(ts_code=s["tushare"], start_date=START_DATE, end_date=END_DATE),
            "adj_factor": lambda s=symbol: pro.adj_factor(ts_code=s["tushare"], start_date=START_DATE, end_date=END_DATE),
            "suspend_d": lambda s=symbol: pro.suspend_d(ts_code=s["tushare"], start_date=START_DATE, end_date=END_DATE),
            "namechange": lambda s=symbol: pro.namechange(ts_code=s["tushare"]),
            "stock_basic": lambda s=symbol: pro.stock_basic(ts_code=s["tushare"], fields="ts_code,symbol,name,industry,market,list_status,list_date,delist_date"),
            "balancesheet": lambda s=symbol: pro.balancesheet(ts_code=s["tushare"], start_date=START_DATE, end_date=END_DATE),
            "income": lambda s=symbol: pro.income(ts_code=s["tushare"], start_date=START_DATE, end_date=END_DATE),
            "cashflow": lambda s=symbol: pro.cashflow(ts_code=s["tushare"], start_date=START_DATE, end_date=END_DATE),
            "fina_indicator": lambda s=symbol: pro.fina_indicator(ts_code=s["tushare"], start_date=START_DATE, end_date=END_DATE),
        }
        for endpoint, fn in specs.items():
            calls[endpoint] = rec.call(symbol, endpoint, fn)
            time.sleep(0.12)

        def record_one(capability, endpoint, *, frequency="all", max_periods=None, status_override=None, unit="not supplied", currency="not supplied", scope="not supplied", company_type="not supplied", notes=""):
            frame, err, elapsed, call_id = calls[endpoint]
            kwargs = dict(native_symbol=native, permission_requirement="current configured Tushare token", point_requirement=TUSHARE_POINTS[endpoint])
            if err:
                kwargs["point_requirement"] = err["point_requirement"] if err["point_requirement"] != "not stated by API error" else TUSHARE_POINTS[endpoint]
                add_failure_record(rec, symbol, capability, endpoint, err, elapsed, [call_id], **kwargs)
            else:
                add_df_record(rec, symbol, capability, endpoint, frame, elapsed, [call_id], frequency=frequency, max_periods=max_periods, status_override=status_override, unit=unit, currency=currency, consolidation_scope=scope, company_type=company_type, notes=notes, **kwargs)

        record_one("daily_ohlcv", "daily", currency="CNY from A-share endpoint contract", unit="price CNY; vol 100-share lots; amount thousand CNY")
        daily_frame, daily_err, daily_ms, daily_id = calls["daily"]
        factor_frame, factor_err, factor_ms, factor_id = calls["adj_factor"]
        if daily_err or factor_err:
            err = daily_err or factor_err
            add_failure_record(rec, symbol, "raw_and_adjusted_prices", "daily + adj_factor", err, daily_ms + factor_ms, [daily_id, factor_id], native_symbol=native, permission_requirement="current configured Tushare token", point_requirement=err["point_requirement"])
        else:
            add_df_record(rec, symbol, "raw_and_adjusted_prices", "daily + adj_factor", merge_frames([daily_frame, factor_frame]), daily_ms + factor_ms, [daily_id, factor_id], native_symbol=native, permission_requirement="current configured Tushare token", point_requirement=TUSHARE_POINTS["adj_factor"], currency="CNY from endpoint contract", unit="raw price plus dimensionless adjustment factor", notes="Adjusted price is derivable; endpoint returns raw daily and native factor separately.")
        record_one("adjustment_factor_history", "adj_factor", unit="dimensionless factor")
        record_one("suspension_trading_status", "suspend_d", notes="Dated suspension/resumption events.")
        name_frame, name_err, name_ms, name_id = calls["namechange"]
        basic_frame, basic_err, basic_ms, basic_id = calls["stock_basic"]
        if name_err and basic_err:
            add_failure_record(rec, symbol, "name_st_delisting_history", "namechange + stock_basic", name_err, name_ms + basic_ms, [name_id, basic_id], native_symbol=native, permission_requirement="current configured Tushare token", point_requirement=name_err["point_requirement"])
        else:
            add_df_record(rec, symbol, "name_st_delisting_history", "namechange + stock_basic", merge_frames([name_frame, basic_frame]), name_ms + basic_ms, [name_id, basic_id], native_symbol=native, permission_requirement="current configured Tushare token", point_requirement="provider entitlement accepted", notes="Name-change events plus current listing/delisting metadata.")

        statement_map = {
            "balancesheet": ("annual_balance_sheet", "quarterly_balance_sheet"),
            "income": ("annual_income_statement", "quarterly_income_statement"),
            "cashflow": ("annual_cash_flow", "quarterly_cash_flow"),
        }
        for endpoint, caps in statement_map.items():
            record_one(caps[0], endpoint, frequency="annual", max_periods=5, unit="CNY amounts; provider contract, not explicit row field", currency="CNY", scope="report_type field", company_type="comp_type field")
            record_one(caps[1], endpoint, frequency="all", max_periods=8, unit="CNY amounts; provider contract, not explicit row field", currency="CNY", scope="report_type field", company_type="comp_type field")
        record_one("financial_indicators_ratios", "fina_indicator", max_periods=8, unit="mixed ratios, per-share values and amounts", currency="CNY where monetary")

        statements = merge_frames([calls[name][0] for name in ("balancesheet", "income", "cashflow")])
        statement_ids = [calls[name][3] for name in ("balancesheet", "income", "cashflow")]
        statement_ms = sum(calls[name][2] for name in ("balancesheet", "income", "cashflow"))
        errs = [calls[name][1] for name in ("balancesheet", "income", "cashflow") if calls[name][1]]
        analysis = analyze_frame(statements, max_periods=13)
        if statements.empty and errs:
            for cap in ("announcement_first_publication_dates", "restatement_update_identifiers", "units_currency_scope_company_type"):
                add_failure_record(rec, symbol, cap, "balancesheet + income + cashflow", errs[0], statement_ms, statement_ids, native_symbol=native, permission_requirement="current configured Tushare token", point_requirement=errs[0]["point_requirement"])
        else:
            rec.add(symbol, "announcement_first_publication_dates", endpoint="balancesheet + income + cashflow", called=True, status="available" if analysis["announcement_date_available"] and analysis["first_publication_date_available"] else "partial", analysis=analysis, latency_ms=statement_ms, native_symbol=native, permission_requirement="current configured Tushare token", point_requirement=">=2000 documented baseline", call_ids=statement_ids, notes="ann_date and f_ann_date measured independently.")
            rec.add(symbol, "restatement_update_identifiers", endpoint="balancesheet + income + cashflow", called=True, status="available" if analysis["restatement_identifier_available"] else "unavailable", analysis=analysis, latency_ms=statement_ms, native_symbol=native, permission_requirement="current configured Tushare token", point_requirement=">=2000 documented baseline", typed_failure=None if analysis["restatement_identifier_available"] else "METADATA_NOT_SUPPLIED", call_ids=statement_ids, notes="update_flag is the tested update/restatement indicator.")
            rec.add(symbol, "units_currency_scope_company_type", endpoint="balancesheet + income + cashflow", called=True, status="partial", analysis=analysis, latency_ms=statement_ms, native_symbol=native, permission_requirement="current configured Tushare token", point_requirement=">=2000 documented baseline", unit="CNY contract; not explicit row field", currency="CNY", consolidation_scope="report_type field", company_type="comp_type field", call_ids=statement_ids)
        if symbol is not SYMBOLS[-1]:
            # The current token's live typed response permits these endpoints at
            # one request per minute. Keep the prototype inside that entitlement.
            time.sleep(61)


def _yf_statement(ticker, name):
    value = getattr(ticker, name)
    frame = as_frame(value)
    if frame.empty:
        return frame
    out = frame.transpose().reset_index().rename(columns={"index": "end_date"})
    out.columns = [str(col) for col in out.columns]
    return out


def probe_yahoo(rec: Recorder, scratch: Path):
    import yfinance as yf

    yf.set_tz_cache_location(str(scratch / "yf_cache"))
    for symbol in SYMBOLS:
        native = symbol["yahoo"]
        ticker = yf.Ticker(native)
        hist, hist_err, hist_ms, hist_id = rec.call(symbol, "Ticker.history", lambda: ticker.history(start="2021-01-01", end="2026-07-30", auto_adjust=False, actions=True, timeout=15, raise_errors=True))
        if hist_err:
            add_failure_record(rec, symbol, "daily_ohlcv", "Ticker.history(auto_adjust=False)", hist_err, hist_ms, [hist_id], native_symbol=native)
            add_failure_record(rec, symbol, "raw_and_adjusted_prices", "Ticker.history(auto_adjust=False)", hist_err, hist_ms, [hist_id], native_symbol=native)
            add_failure_record(rec, symbol, "adjustment_factor_history", "derived Adj Close / Close", hist_err, hist_ms, [hist_id], native_symbol=native)
        else:
            frame = as_frame(hist).reset_index()
            add_df_record(rec, symbol, "daily_ohlcv", "Ticker.history(auto_adjust=False)", frame, hist_ms, [hist_id], native_symbol=native, currency="from quote metadata, not history row", unit="provider-defined")
            add_df_record(rec, symbol, "raw_and_adjusted_prices", "Ticker.history(Close, Adj Close)", frame, hist_ms, [hist_id], native_symbol=native, currency="from quote metadata, not history row", unit="provider-defined")
            factor = pd.DataFrame()
            if "Close" in frame and "Adj Close" in frame:
                factor = pd.DataFrame({"date": frame.get("Date", frame.index), "factor": pd.to_numeric(frame["Adj Close"], errors="coerce") / pd.to_numeric(frame["Close"], errors="coerce")})
            add_df_record(rec, symbol, "adjustment_factor_history", "derived Adj Close / Close", factor, hist_ms, [hist_id], native_symbol=native, status_override="partial" if not factor.empty else "unavailable", unit="derived dimensionless factor", notes="Derived ratio, not a native dated corporate-action factor endpoint.")

        statements = {}
        statement_specs = {
            "annual_balance_sheet": "balance_sheet",
            "quarterly_balance_sheet": "quarterly_balance_sheet",
            "annual_income_statement": "income_stmt",
            "quarterly_income_statement": "quarterly_income_stmt",
            "annual_cash_flow": "cash_flow",
            "quarterly_cash_flow": "quarterly_cash_flow",
        }
        for capability, attr in statement_specs.items():
            frame, err, elapsed, call_id = rec.call(symbol, f"Ticker.{attr}", lambda a=attr: _yf_statement(ticker, a))
            statements[capability] = (frame, err, elapsed, call_id, attr)
            if err:
                add_failure_record(rec, symbol, capability, f"Ticker.{attr}", err, elapsed, [call_id], native_symbol=native)
            else:
                add_df_record(rec, symbol, capability, f"Ticker.{attr}", frame, elapsed, [call_id], max_periods=5 if capability.startswith("annual_") else 8, native_symbol=native, currency="not explicit in statement payload", unit="provider-normalized amounts", consolidation_scope="not supplied", company_type="not supplied")

        info, info_err, info_ms, info_id = rec.call(symbol, "Ticker.get_info", ticker.get_info)
        info_frame = pd.DataFrame([info]) if isinstance(info, dict) else pd.DataFrame()
        if info_err:
            add_failure_record(rec, symbol, "financial_indicators_ratios", "Ticker.get_info", info_err, info_ms, [info_id], native_symbol=native)
        else:
            add_df_record(rec, symbol, "financial_indicators_ratios", "Ticker.get_info", info_frame, info_ms, [info_id], native_symbol=native, status_override="partial" if not info_frame.empty else "unavailable", unit="mixed", currency=str(info.get("financialCurrency") or info.get("currency") or "not supplied"), company_type=str(info.get("industry") or info.get("sector") or "not supplied"))

        earnings, earnings_err, earnings_ms, earnings_id = rec.call(symbol, "Ticker.get_earnings_dates", lambda: ticker.get_earnings_dates(limit=20))
        earnings_frame = as_frame(earnings).reset_index() if earnings is not None else pd.DataFrame()
        if earnings_err:
            add_failure_record(rec, symbol, "announcement_first_publication_dates", "Ticker.get_earnings_dates", earnings_err, earnings_ms, [earnings_id], native_symbol=native)
        else:
            add_df_record(rec, symbol, "announcement_first_publication_dates", "Ticker.get_earnings_dates", earnings_frame, earnings_ms, [earnings_id], native_symbol=native, status_override="partial" if not earnings_frame.empty else "unavailable", notes="Earnings-event dates are not statement filing announcement/first-publication lineage.")
        rec.unsupported(symbol, "restatement_update_identifiers", endpoint="Yahoo statement endpoints")
        info_currency = "not supplied" if not isinstance(info, dict) else str(info.get("financialCurrency") or info.get("currency") or "not supplied")
        rec.add(symbol, "units_currency_scope_company_type", endpoint="Ticker.get_info + statement endpoints", called=not bool(info_err), status="partial" if not info_err else "unavailable", analysis=analyze_frame(info_frame), latency_ms=info_ms, native_symbol=native, typed_failure=info_err["typed_failure"] if info_err else None, failure_detail=info_err["failure_detail"] if info_err else None, unit="statement amounts; scale not explicit", currency=info_currency, consolidation_scope="not supplied", company_type=str(info.get("industry") or "not supplied") if isinstance(info, dict) else "not supplied", call_ids=[info_id])
        rec.add(symbol, "suspension_trading_status", endpoint="Ticker.get_info / history metadata", called=not bool(info_err), status="partial" if not info_err else "unavailable", analysis=analyze_frame(info_frame), latency_ms=info_ms, native_symbol=native, typed_failure=info_err["typed_failure"] if info_err else None, failure_detail=info_err["failure_detail"] if info_err else None, call_ids=[info_id], notes="Current quote metadata only; no authoritative dated suspension history.")
        rec.add(symbol, "name_st_delisting_history", endpoint="Ticker.get_info", called=not bool(info_err), status="partial" if not info_err else "unavailable", analysis=analyze_frame(info_frame), latency_ms=info_ms, native_symbol=native, typed_failure=info_err["typed_failure"] if info_err else None, failure_detail=info_err["failure_detail"] if info_err else None, call_ids=[info_id], notes="Current name/profile only; no native mainland name/ST/delisting history.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["akshare", "baostock", "tushare", "yahoo"], required=True)
    parser.add_argument("--provider-label", required=True)
    parser.add_argument("--run", type=int, choices=[1, 2], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    package = {"akshare": "akshare", "baostock": "baostock", "tushare": "tushare", "yahoo": "yfinance"}[args.provider]
    version = importlib.metadata.version(package)
    rec = Recorder(args.provider_label, version, args.run)
    if args.provider == "akshare":
        probe_akshare(rec)
    elif args.provider == "baostock":
        probe_baostock(rec)
    elif args.provider == "tushare":
        probe_tushare(rec)
    else:
        probe_yahoo(rec, args.scratch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"provider": args.provider_label, "version": version, "run": args.run, "records": rec.records}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

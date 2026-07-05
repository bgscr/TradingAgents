from __future__ import annotations

import contextlib
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import akshare as ak
import pandas as pd
import requests

CODE_COL = "\u4ee3\u7801"
NAME_COL = "\u540d\u79f0"
PRICE_COL = "\u6700\u65b0\u4ef7"
CHANGE_COL = "\u6da8\u8dcc\u5e45"
AMOUNT_COL = "\u6210\u4ea4\u989d"
TURNOVER_COL = "\u6362\u624b\u7387"
DYNAMIC_PE_COL = "\u5e02\u76c8\u7387-\u52a8\u6001"

VALUATION_COLUMNS = (
    DYNAMIC_PE_COL,
    "\u5e02\u76c8\u7387-TTM",
    "\u5e02\u76c8\u7387-\u9759\u6001",
    "PE(TTM)",
    "pe_ttm",
)
VALUATION_STATUS_COL = "valuation_data_status"
CANDIDATE_WARNING_COL = "candidate_warning"
MOMENTUM_20D_COL = "momentum_20d"
MOMENTUM_60D_COL = "momentum_60d"
VOLATILITY_20D_COL = "volatility_20d"
MA_TREND_COL = "ma_trend_20_60"
AVG_AMOUNT_20D_COL = "avg_amount_20d"
RELATIVE_STRENGTH_20D_COL = "relative_strength_20d"
HISTORICAL_FACTOR_COLUMNS = [
    MOMENTUM_20D_COL,
    MOMENTUM_60D_COL,
    VOLATILITY_20D_COL,
    MA_TREND_COL,
    AVG_AMOUNT_20D_COL,
    RELATIVE_STRENGTH_20D_COL,
]

REQUIRED_COLS = [CODE_COL, NAME_COL, PRICE_COL, AMOUNT_COL]
NUMERIC_COLS = [PRICE_COL, CHANGE_COL, AMOUNT_COL, TURNOVER_COL, *VALUATION_COLUMNS]
DISPLAY_COLS = [
    CODE_COL,
    NAME_COL,
    PRICE_COL,
    CHANGE_COL,
    AMOUNT_COL,
    TURNOVER_COL,
    DYNAMIC_PE_COL,
    *HISTORICAL_FACTOR_COLUMNS,
    VALUATION_STATUS_COL,
    CANDIDATE_WARNING_COL,
    "score",
    "tradingagents_ticker",
]
EASTMONEY_SPOT_DIRECT_SOURCE = "stock_zh_a_spot_em_direct"
EASTMONEY_SPOT_AKSHARE_SOURCE = "stock_zh_a_spot_em"
SPOT_SOURCES = (
    EASTMONEY_SPOT_DIRECT_SOURCE,
    EASTMONEY_SPOT_AKSHARE_SOURCE,
    "stock_zh_a_spot",
)
BENCHMARK_INDEX_SYMBOL = "000300"
HISTORICAL_LOOKBACK_DAYS = 120
HISTORICAL_PREFILTER_LIMIT = 40
EASTMONEY_TIMEOUT = 15
EASTMONEY_PAGE_SLEEP_SECONDS = 0.2
EASTMONEY_DEBUG_ENV = "AK_PICK_DEBUG"
EASTMONEY_DEBUG_BODY_CHARS_ENV = "AK_PICK_DEBUG_BODY_CHARS"
AK_PICK_OUTPUT_PATH_ENV = "AK_PICK_OUTPUT_PATH"
EASTMONEY_DEBUG_TRUE_VALUES = {"1", "true", "yes", "on", "debug"}
EASTMONEY_REFERER = "https://quote.eastmoney.com/center/gridlist.html#hs_a_board"
EASTMONEY_SPOT_URLS = (
    "https://82.push2.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)
EASTMONEY_SPOT_PARAMS = {
    "pn": "1",
    "pz": "100",
    "po": "1",
    "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2",
    "invt": "2",
    "fid": "f12",
    "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
    "fields": (
        "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
        "f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152"
    ),
}
EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": EASTMONEY_REFERER,
}
EASTMONEY_FIELD_RENAMES = {
    "f2": PRICE_COL,
    "f3": CHANGE_COL,
    "f4": "涨跌额",
    "f5": "成交量",
    "f6": AMOUNT_COL,
    "f7": "振幅",
    "f8": TURNOVER_COL,
    "f9": DYNAMIC_PE_COL,
    "f10": "量比",
    "f11": "5分钟涨跌",
    "f12": CODE_COL,
    "f14": NAME_COL,
    "f15": "最高",
    "f16": "最低",
    "f17": "今开",
    "f18": "昨收",
    "f20": "总市值",
    "f21": "流通市值",
    "f22": "涨速",
    "f23": "市净率",
    "f24": "60日涨跌幅",
    "f25": "年初至今涨跌幅",
}
EASTMONEY_OUTPUT_COLS = [
    "序号",
    CODE_COL,
    NAME_COL,
    PRICE_COL,
    CHANGE_COL,
    "涨跌额",
    "成交量",
    AMOUNT_COL,
    "振幅",
    "最高",
    "最低",
    "今开",
    "昨收",
    "量比",
    TURNOVER_COL,
    DYNAMIC_PE_COL,
    "市净率",
    "总市值",
    "流通市值",
    "涨速",
    "5分钟涨跌",
    "60日涨跌幅",
    "年初至今涨跌幅",
]
EASTMONEY_NUMERIC_COLS = [
    PRICE_COL,
    CHANGE_COL,
    "涨跌额",
    "成交量",
    AMOUNT_COL,
    "振幅",
    "最高",
    "最低",
    "今开",
    "昨收",
    "量比",
    TURNOVER_COL,
    DYNAMIC_PE_COL,
    "市净率",
    "总市值",
    "流通市值",
    "涨速",
    "5分钟涨跌",
    "60日涨跌幅",
    "年初至今涨跌幅",
]


def normalize_stock_code(code: str) -> str:
    code = str(code).strip()
    lowered = code.lower()
    if lowered.startswith(("sh", "sz", "bj")):
        code = code[2:]
    return code.zfill(6) if code.isdigit() else code


def to_ta_ticker(code: str) -> str:
    code = normalize_stock_code(code)

    if code.startswith(("600", "601", "603", "605", "688", "900")):
        return f"{code}.SS"

    if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return f"{code}.SZ"

    return code


def _call_silently(fn, *args, **kwargs) -> pd.DataFrame:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def _call_akshare_source(source: str) -> pd.DataFrame:
    return _call_silently(getattr(ak, source))


def _eastmoney_debug_enabled() -> bool:
    return os.environ.get(EASTMONEY_DEBUG_ENV, "").strip().lower() in EASTMONEY_DEBUG_TRUE_VALUES


def _eastmoney_debug_body_chars() -> int:
    raw = os.environ.get(EASTMONEY_DEBUG_BODY_CHARS_ENV, "500")
    try:
        return max(0, int(raw))
    except ValueError:
        return 500


def _eastmoney_debug(message: str) -> None:
    if _eastmoney_debug_enabled():
        print(f"[eastmoney-debug] {message}", file=sys.stderr)


def _eastmoney_redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.netloc or "@" not in parts.netloc:
        return value

    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"<redacted>@{host}", parts.path, parts.query, parts.fragment))


def _eastmoney_debug_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _eastmoney_proxy_snapshot(url: str) -> dict[str, object]:
    env_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    env_values = {
        name: _eastmoney_redact_url(value)
        for name in env_names
        if (value := os.environ.get(name))
    }
    environ_proxies = {
        name: _eastmoney_redact_url(value)
        for name, value in requests.utils.get_environ_proxies(url).items()
    }
    return {"env": env_values, "requests_environ_proxies": environ_proxies}


def _eastmoney_prepared_url(url: str, params: dict[str, object]) -> str:
    request = requests.Request("GET", url, params=params)
    prepared = request.prepare()
    return prepared.url or url


def _eastmoney_response_elapsed_seconds(response: requests.Response) -> float | None:
    elapsed = getattr(response, "elapsed", None)
    if elapsed is None:
        return None
    try:
        return round(float(elapsed.total_seconds()), 3)
    except (TypeError, ValueError, AttributeError):
        return None


def _eastmoney_body_snippet(response: requests.Response) -> str:
    limit = _eastmoney_debug_body_chars()
    if limit <= 0:
        return ""
    try:
        text = response.text
    except Exception as exc:  # noqa: BLE001 - diagnostics must not hide original request failures
        return f"<unable to read response.text: {type(exc).__name__}: {exc}>"
    return " ".join(str(text)[:limit].split())


def _eastmoney_log_response(response: requests.Response, *, page: object) -> None:
    status = getattr(response, "status_code", "unknown")
    reason = getattr(response, "reason", "")
    headers = getattr(response, "headers", {}) or {}
    content_type = headers.get("Content-Type", "")
    elapsed = _eastmoney_response_elapsed_seconds(response)
    final_url = getattr(response, "url", "")
    _eastmoney_debug(
        "response "
        f"page={page} status={status} reason={reason} content_type={content_type} "
        f"elapsed_seconds={elapsed} final_url={final_url}"
    )

    should_log_body = str(page) == "1"
    try:
        should_log_body = should_log_body or int(status) >= 400
    except (TypeError, ValueError):
        should_log_body = True
    if should_log_body:
        _eastmoney_debug(f"response body page={page} snippet={_eastmoney_body_snippet(response)}")


def _eastmoney_log_json_shape(payload: object, *, page: object) -> None:
    if not isinstance(payload, dict):
        _eastmoney_debug(f"json page={page} payload_type={type(payload).__name__}")
        return

    data = payload.get("data")
    diff = data.get("diff") if isinstance(data, dict) else None
    total = data.get("total") if isinstance(data, dict) else None
    diff_len = len(diff) if isinstance(diff, list) else "n/a"
    _eastmoney_debug(
        f"json page={page} top_keys={list(payload.keys())} "
        f"data_type={type(data).__name__} diff_type={type(diff).__name__} "
        f"diff_len={diff_len} total={total}"
    )


def _eastmoney_spot_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(EASTMONEY_HEADERS)
    _eastmoney_debug(
        "session "
        f"trust_env={session.trust_env} headers={_eastmoney_debug_json(dict(session.headers))}"
    )
    return session


def _eastmoney_get_json(
    session: requests.Session,
    url: str,
    params: dict[str, object],
) -> dict[str, object]:
    page = params.get("pn", "?")
    _eastmoney_debug(
        "request "
        f"page={page} url={_eastmoney_prepared_url(url, params)} "
        f"timeout={EASTMONEY_TIMEOUT} proxy_snapshot={_eastmoney_debug_json(_eastmoney_proxy_snapshot(url))}"
    )
    try:
        response = session.get(url, params=params, timeout=EASTMONEY_TIMEOUT)
    except Exception as exc:
        _eastmoney_debug(
            f"request exception page={page} type={type(exc).__name__} message={exc}"
        )
        raise
    _eastmoney_log_response(response, page=page)
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception as exc:
        _eastmoney_debug(
            f"json exception page={page} type={type(exc).__name__} message={exc} "
            f"body_snippet={_eastmoney_body_snippet(response)}"
        )
        raise
    _eastmoney_log_json_shape(payload, page=page)
    if not isinstance(payload, dict):
        raise RuntimeError("Eastmoney returned a non-object JSON payload")
    return payload


def _eastmoney_diff(payload: dict[str, object], *, page: int) -> tuple[list[object], int]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Eastmoney page {page} missing data object")
    diff = data.get("diff")
    if not isinstance(diff, list):
        raise RuntimeError(f"Eastmoney page {page} missing data.diff rows")
    total = data.get("total", len(diff))
    try:
        total_rows = int(total)
    except (TypeError, ValueError):
        total_rows = len(diff)
    return diff, total_rows


def _normalize_eastmoney_spot_frame(raw: pd.DataFrame) -> pd.DataFrame:
    frame = raw.rename(columns=EASTMONEY_FIELD_RENAMES).copy()
    for col in EASTMONEY_OUTPUT_COLS:
        if col not in frame.columns and col != "序号":
            frame[col] = pd.NA

    for col in EASTMONEY_NUMERIC_COLS:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.sort_values(CHANGE_COL, ascending=False, na_position="last")
    frame = frame.reset_index(drop=True)
    frame.insert(0, "序号", frame.index + 1)
    return frame[EASTMONEY_OUTPUT_COLS]


def _fetch_eastmoney_spot_from_url(
    session: requests.Session,
    url: str,
) -> pd.DataFrame:
    params = EASTMONEY_SPOT_PARAMS.copy()
    payload = _eastmoney_get_json(session, url, params)
    diff, total_rows = _eastmoney_diff(payload, page=1)
    if not diff:
        _eastmoney_debug("page=1 returned no rows")
        return pd.DataFrame(columns=EASTMONEY_OUTPUT_COLS)

    frames = [pd.DataFrame(diff)]
    total_pages = max(math.ceil(total_rows / len(diff)), 1)
    _eastmoney_debug(
        f"page=1 rows={len(diff)} total_rows={total_rows} total_pages={total_pages}"
    )
    for page in range(2, total_pages + 1):
        time.sleep(EASTMONEY_PAGE_SLEEP_SECONDS)
        params["pn"] = page
        payload = _eastmoney_get_json(session, url, params)
        page_diff, _ = _eastmoney_diff(payload, page=page)
        _eastmoney_debug(f"page={page} rows={len(page_diff)}")
        if page_diff:
            frames.append(pd.DataFrame(page_diff))

    raw = pd.concat(frames, ignore_index=True)
    normalized = _normalize_eastmoney_spot_frame(raw)
    _eastmoney_debug(f"normalized rows={len(normalized)} columns={list(normalized.columns)}")
    return normalized


def _fetch_eastmoney_spot_direct() -> pd.DataFrame:
    errors = []
    _eastmoney_debug(f"direct source start urls={list(EASTMONEY_SPOT_URLS)}")
    for url in EASTMONEY_SPOT_URLS:
        try:
            _eastmoney_debug(f"trying url={url}")
            return _fetch_eastmoney_spot_from_url(_eastmoney_spot_session(), url)
        except Exception as exc:  # noqa: BLE001 - URLs are ordered fallbacks
            _eastmoney_debug(
                f"url failed url={url} type={type(exc).__name__} message={exc}"
            )
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise RuntimeError("Eastmoney direct request failed:\n" + "\n".join(f"- {e}" for e in errors))


def _call_spot_source(source: str) -> pd.DataFrame:
    if source == EASTMONEY_SPOT_DIRECT_SOURCE:
        return _fetch_eastmoney_spot_direct()
    return _call_akshare_source(source)


def load_spot_data() -> tuple[pd.DataFrame, str, list[str]]:
    errors = []
    for source in SPOT_SOURCES:
        try:
            data = _call_spot_source(source)
        except Exception as exc:  # noqa: BLE001 - user-facing helper should try fallback sources
            errors.append(f"{source}: {type(exc).__name__}: {exc}")
            continue
        if data is None or data.empty:
            errors.append(f"{source}: returned no rows")
            continue
        return data.copy(), source, errors

    raise RuntimeError("Unable to fetch A-share spot data:\n" + "\n".join(f"- {e}" for e in errors))


def _resolve_valuation_column(data: pd.DataFrame) -> str | None:
    for col in VALUATION_COLUMNS:
        if col in data.columns:
            return col
    return None


def _valuation_missing_warning(source: str) -> str:
    return f"valuation data unavailable from {source}"


def _date_for_akshare(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _prefixed_exchange_code(code: str) -> str:
    code = normalize_stock_code(code)
    if code.startswith(("600", "601", "603", "605", "688", "900")):
        return f"sh{code}"
    if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return f"sz{code}"
    return code


def load_stock_history(code: str) -> pd.DataFrame:
    end = datetime.now()
    start = end - timedelta(days=HISTORICAL_LOOKBACK_DAYS)
    start_date = _date_for_akshare(start)
    end_date = _date_for_akshare(end)
    errors = []

    for name, fn, kwargs in (
        (
            "stock_zh_a_hist",
            ak.stock_zh_a_hist,
            {
                "symbol": normalize_stock_code(code),
                "period": "daily",
                "start_date": start_date,
                "end_date": end_date,
                "adjust": "qfq",
            },
        ),
        (
            "stock_zh_a_daily",
            ak.stock_zh_a_daily,
            {
                "symbol": _prefixed_exchange_code(code),
                "start_date": start_date,
                "end_date": end_date,
                "adjust": "qfq",
            },
        ),
        (
            "stock_zh_a_hist_tx",
            ak.stock_zh_a_hist_tx,
            {
                "symbol": _prefixed_exchange_code(code),
                "start_date": start_date,
                "end_date": end_date,
                "adjust": "qfq",
            },
        ),
    ):
        try:
            data = _call_silently(fn, **kwargs)
        except Exception as exc:  # noqa: BLE001 - history sources are ordered fallbacks
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if data is not None and not data.empty:
            return data
        errors.append(f"{name}: returned no rows")

    raise RuntimeError("Unable to fetch stock history:\n" + "\n".join(f"- {e}" for e in errors))


def load_benchmark_history() -> pd.DataFrame:
    end = datetime.now()
    start = end - timedelta(days=HISTORICAL_LOOKBACK_DAYS)
    start_date = _date_for_akshare(start)
    end_date = _date_for_akshare(end)
    errors = []

    for name, fn, kwargs in (
        (
            "index_zh_a_hist",
            ak.index_zh_a_hist,
            {
                "symbol": BENCHMARK_INDEX_SYMBOL,
                "period": "daily",
                "start_date": start_date,
                "end_date": end_date,
            },
        ),
        (
            "stock_zh_index_daily_tx",
            ak.stock_zh_index_daily_tx,
            {
                "symbol": f"sh{BENCHMARK_INDEX_SYMBOL}",
                "start_date": start_date,
                "end_date": end_date,
            },
        ),
        (
            "stock_zh_index_daily",
            ak.stock_zh_index_daily,
            {
                "symbol": f"sh{BENCHMARK_INDEX_SYMBOL}",
            },
        ),
    ):
        try:
            data = _call_silently(fn, **kwargs)
        except Exception as exc:  # noqa: BLE001 - benchmark sources are ordered fallbacks
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if data is not None and not data.empty:
            return data
        errors.append(f"{name}: returned no rows")

    raise RuntimeError("Unable to fetch benchmark history:\n" + "\n".join(f"- {e}" for e in errors))


def _normalize_history_frame(data: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "日期": "Date",
        "Date": "Date",
        "date": "Date",
        "收盘": "Close",
        "Close": "Close",
        "close": "Close",
        "成交额": "Amount",
        "Amount": "Amount",
        "amount": "Amount",
    }
    keep = [col for col in rename_map if col in data.columns]
    frame = data[keep].rename(columns=rename_map).copy()
    if "Date" not in frame.columns or "Close" not in frame.columns:
        return pd.DataFrame(columns=["Date", "Close", "Amount"])

    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
    if "Amount" not in frame.columns:
        frame["Amount"] = pd.NA
    frame["Amount"] = pd.to_numeric(frame["Amount"], errors="coerce")
    frame = frame.dropna(subset=["Date", "Close"]).sort_values("Date")
    return frame[["Date", "Close", "Amount"]]


def _period_return(frame: pd.DataFrame, periods: int) -> float | None:
    if len(frame) <= periods:
        return None
    start = frame["Close"].iloc[-periods - 1]
    end = frame["Close"].iloc[-1]
    if pd.isna(start) or start == 0 or pd.isna(end):
        return None
    return float(end / start - 1)


def _history_factors(history: pd.DataFrame, benchmark_momentum_20d: float | None) -> dict[str, float | None]:
    frame = _normalize_history_frame(history)
    if frame.empty:
        return dict.fromkeys(HISTORICAL_FACTOR_COLUMNS)

    momentum_20d = _period_return(frame, 20)
    momentum_60d = _period_return(frame, 60)
    daily_returns = frame["Close"].pct_change().tail(20)
    volatility_20d = daily_returns.std() if daily_returns.notna().sum() >= 2 else None
    ma20 = frame["Close"].tail(20).mean() if len(frame) >= 20 else None
    ma60 = frame["Close"].tail(60).mean() if len(frame) >= 60 else None
    ma_trend = float(ma20 / ma60 - 1) if ma20 and ma60 and ma60 != 0 else None
    avg_amount_20d = frame["Amount"].tail(20).mean()
    relative_strength_20d = (
        momentum_20d - benchmark_momentum_20d
        if momentum_20d is not None and benchmark_momentum_20d is not None
        else None
    )

    return {
        MOMENTUM_20D_COL: momentum_20d,
        MOMENTUM_60D_COL: momentum_60d,
        VOLATILITY_20D_COL: float(volatility_20d) if volatility_20d is not None and not pd.isna(volatility_20d) else None,
        MA_TREND_COL: ma_trend,
        AVG_AMOUNT_20D_COL: float(avg_amount_20d) if not pd.isna(avg_amount_20d) else None,
        RELATIVE_STRENGTH_20D_COL: relative_strength_20d,
    }


def _rank_bonus(series: pd.Series, weight: float, *, lower_is_better: bool = False) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    ranked = numeric.rank(pct=True, ascending=not lower_is_better)
    return ranked.fillna(0) * weight


def _add_historical_factors(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        for col in HISTORICAL_FACTOR_COLUMNS:
            df[col] = pd.NA
        return df

    out = df.copy()
    for col in HISTORICAL_FACTOR_COLUMNS:
        out[col] = pd.NA

    try:
        benchmark = _normalize_history_frame(load_benchmark_history())
        benchmark_momentum_20d = _period_return(benchmark, 20)
    except Exception:  # noqa: BLE001 - benchmark history is a scoring enhancement only
        benchmark_momentum_20d = None

    candidates = out.sort_values("score", ascending=False).head(HISTORICAL_PREFILTER_LIMIT)
    for idx, row in candidates.iterrows():
        try:
            factors = _history_factors(load_stock_history(row[CODE_COL]), benchmark_momentum_20d)
        except Exception:  # noqa: BLE001 - keep the candidate pool usable if one symbol fails
            factors = dict.fromkeys(HISTORICAL_FACTOR_COLUMNS)
        for col, value in factors.items():
            out.at[idx, col] = value

    out["score"] += _rank_bonus(out[MOMENTUM_20D_COL], 18)
    out["score"] += _rank_bonus(out[MOMENTUM_60D_COL], 12)
    out["score"] += _rank_bonus(out[RELATIVE_STRENGTH_20D_COL], 18)
    out["score"] += _rank_bonus(out[MA_TREND_COL], 12)
    out["score"] += _rank_bonus(out[AVG_AMOUNT_20D_COL], 10)
    out["score"] -= _rank_bonus(out[VOLATILITY_20D_COL], 8, lower_is_better=True)
    return out


def prepare_candidates(data: pd.DataFrame, source: str) -> pd.DataFrame:
    missing = [col for col in REQUIRED_COLS if col not in data.columns]
    if missing:
        raise ValueError(f"{source} missing required columns: {', '.join(missing)}")

    valuation_col = _resolve_valuation_column(data)
    optional_cols = [CHANGE_COL, TURNOVER_COL]
    if valuation_col:
        optional_cols.append(valuation_col)
    keep_cols = [col for col in [*REQUIRED_COLS, *optional_cols] if col in data.columns]
    df = data[keep_cols].copy()
    df[CODE_COL] = df[CODE_COL].apply(normalize_stock_code)

    if valuation_col and valuation_col != DYNAMIC_PE_COL:
        df[DYNAMIC_PE_COL] = df[valuation_col]

    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 基础过滤：排除 ST、低流动性、异常价格
    df = df[~df[NAME_COL].astype(str).str.contains("ST", case=False, na=False)]
    df = df[df[AMOUNT_COL] > 300_000_000]  # 成交额 > 3 亿
    df = df[df[PRICE_COL] > 3]  # 排除低价异常股

    if valuation_col:
        df[VALUATION_STATUS_COL] = "ok"
        df[CANDIDATE_WARNING_COL] = ""
        missing_mask = df[DYNAMIC_PE_COL].isna()
        df.loc[missing_mask, VALUATION_STATUS_COL] = "missing_row"
        df.loc[missing_mask, CANDIDATE_WARNING_COL] = "valuation missing for row"
        df = df[(df[DYNAMIC_PE_COL] > 0) & (df[DYNAMIC_PE_COL] < 80)]
    else:
        df[DYNAMIC_PE_COL] = pd.NA
        df[VALUATION_STATUS_COL] = "missing_source"
        df[CANDIDATE_WARNING_COL] = _valuation_missing_warning(source)

    df["score"] = 0.0
    df["score"] += df[AMOUNT_COL].rank(pct=True) * 40

    if CHANGE_COL in df.columns:
        df["score"] += df[CHANGE_COL].between(0, 5).astype(int) * 30
        df["score"] -= df[CHANGE_COL].abs().rank(pct=True) * 10

    if TURNOVER_COL in df.columns:
        df["score"] += df[TURNOVER_COL].between(1, 8).astype(int) * 20

    if not valuation_col:
        df["score"] -= 25

    df = _add_historical_factors(df)
    df["tradingagents_ticker"] = df[CODE_COL].apply(to_ta_ticker)
    return df.sort_values("score", ascending=False).head(10)


def resolve_candidate_output_path() -> Path:
    raw_path = os.environ.get(AK_PICK_OUTPUT_PATH_ENV, "").strip()
    return Path(raw_path) if raw_path else Path("ak_candidates.csv")


def write_candidates_csv(result: pd.DataFrame) -> Path:
    output_path = resolve_candidate_output_path()
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, encoding="utf-8-sig")
    return output_path


def main() -> int:
    try:
        data, source, fallback_reasons = load_spot_data()
        result = prepare_candidates(data, source)
        for reason in fallback_reasons:
            print(f"fallback reason: {reason}")
        if (
            VALUATION_STATUS_COL in result.columns
            and (result[VALUATION_STATUS_COL] == "missing_source").any()
        ):
            print(_valuation_missing_warning(source))
    except Exception as exc:  # noqa: BLE001 - script entrypoint reports concise failures
        print(str(exc))
        return 1

    for col in DISPLAY_COLS:
        if col not in result.columns:
            result[col] = pd.NA
    result = result[DISPLAY_COLS]

    print(f"source: {source}")
    print("columns:", list(data.columns))

    print("\n候选标的（筛选结果，不构成投资建议）：")
    print(result)

    output_path = write_candidates_csv(result)
    print(f"\n已输出: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

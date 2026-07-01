from __future__ import annotations

import sys

import akshare as ak
import pandas as pd

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
    VALUATION_STATUS_COL,
    CANDIDATE_WARNING_COL,
    "score",
    "tradingagents_ticker",
]
SPOT_SOURCES = ("stock_zh_a_spot_em", "stock_zh_a_spot")


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


def load_spot_data() -> tuple[pd.DataFrame, str]:
    errors = []
    for source in SPOT_SOURCES:
        try:
            data = getattr(ak, source)()
        except Exception as exc:  # noqa: BLE001 - user-facing helper should try fallback sources
            errors.append(f"{source}: {type(exc).__name__}: {exc}")
            continue
        if data is None or data.empty:
            errors.append(f"{source}: returned no rows")
            continue
        return data.copy(), source

    raise RuntimeError("Unable to fetch A-share spot data:\n" + "\n".join(f"- {e}" for e in errors))


def _resolve_valuation_column(data: pd.DataFrame) -> str | None:
    for col in VALUATION_COLUMNS:
        if col in data.columns:
            return col
    return None


def _valuation_missing_warning(source: str) -> str:
    return f"valuation data unavailable from {source}"


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

    df["tradingagents_ticker"] = df[CODE_COL].apply(to_ta_ticker)
    return df.sort_values("score", ascending=False).head(10)


def main() -> int:
    try:
        data, source = load_spot_data()
        result = prepare_candidates(data, source)
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

    print("\n候选股票：")
    print(result)

    result.to_csv("ak_candidates.csv", index=False, encoding="utf-8-sig")
    print("\n已输出: ak_candidates.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())

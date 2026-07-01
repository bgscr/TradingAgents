from __future__ import annotations

import sys

import akshare as ak
import pandas as pd

REQUIRED_COLS = ["代码", "名称", "最新价", "成交额"]
NUMERIC_COLS = ["最新价", "涨跌幅", "成交额", "换手率", "市盈率-动态"]
DISPLAY_COLS = [
    "代码",
    "名称",
    "最新价",
    "涨跌幅",
    "成交额",
    "换手率",
    "市盈率-动态",
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


def prepare_candidates(data: pd.DataFrame, source: str) -> pd.DataFrame:
    missing = [col for col in REQUIRED_COLS if col not in data.columns]
    if missing:
        raise ValueError(f"{source} missing required columns: {', '.join(missing)}")

    keep_cols = [col for col in [*REQUIRED_COLS, "涨跌幅", "换手率", "市盈率-动态"] if col in data.columns]
    df = data[keep_cols].copy()
    df["代码"] = df["代码"].apply(normalize_stock_code)
    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 基础过滤：排除 ST、低流动性、异常价格
    df = df[~df["名称"].astype(str).str.contains("ST", case=False, na=False)]
    df = df[df["成交额"] > 300_000_000]  # 成交额 > 3 亿
    df = df[df["最新价"] > 3]  # 排除低价异常股

    if "市盈率-动态" in df.columns:
        df = df[(df["市盈率-动态"] > 0) & (df["市盈率-动态"] < 80)]

    df["score"] = 0.0
    df["score"] += df["成交额"].rank(pct=True) * 40

    if "涨跌幅" in df.columns:
        df["score"] += df["涨跌幅"].between(0, 5).astype(int) * 30
        df["score"] -= df["涨跌幅"].abs().rank(pct=True) * 10

    if "换手率" in df.columns:
        df["score"] += df["换手率"].between(1, 8).astype(int) * 20

    df["tradingagents_ticker"] = df["代码"].apply(to_ta_ticker)
    return df.sort_values("score", ascending=False).head(10)


def main() -> int:
    try:
        data, source = load_spot_data()
        result = prepare_candidates(data, source)
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

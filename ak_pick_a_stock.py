import akshare as ak
import pandas as pd


def to_ta_ticker(code: str) -> str:
    code = str(code).zfill(6)

    if code.startswith(("600", "601", "603", "605", "688", "900")):
        return f"{code}.SS"

    if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return f"{code}.SZ"

    return code


def main():
    df = ak.stock_zh_a_spot_em()

    # 字段兼容：AkShare 字段可能随接口调整，先打印确认
    print("columns:", list(df.columns))

    # 常见字段：代码、名称、最新价、涨跌幅、成交额、换手率、市盈率-动态
    need_cols = ["代码", "名称", "最新价", "涨跌幅", "成交额", "换手率", "市盈率-动态"]
    exist_cols = [c for c in need_cols if c in df.columns]
    df = df[exist_cols].copy()

    # 转数值
    for col in ["最新价", "涨跌幅", "成交额", "换手率", "市盈率-动态"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 基础过滤：排除 ST、低流动性、异常 PE
    df = df[~df["名称"].astype(str).str.contains("ST", case=False, na=False)]
    df = df[df["成交额"] > 300_000_000]       # 成交额 > 3 亿
    df = df[df["最新价"] > 3]                 # 排除低价异常股

    if "市盈率-动态" in df.columns:
        df = df[(df["市盈率-动态"] > 0) & (df["市盈率-动态"] < 80)]

    # 简单打分：涨幅适中 + 换手不极端 + 成交额高
    df["score"] = 0.0
    df["score"] += df["成交额"].rank(pct=True) * 40

    if "涨跌幅" in df.columns:
        df["score"] += df["涨跌幅"].between(0, 5).astype(int) * 30
        df["score"] -= df["涨跌幅"].abs().rank(pct=True) * 10

    if "换手率" in df.columns:
        df["score"] += df["换手率"].between(1, 8).astype(int) * 20

    df["tradingagents_ticker"] = df["代码"].apply(to_ta_ticker)

    result = df.sort_values("score", ascending=False).head(10)

    print("\n候选股票：")
    print(result[["代码", "名称", "最新价", "涨跌幅", "成交额", "换手率", "市盈率-动态", "score", "tradingagents_ticker"]])

    result.to_csv("ak_candidates.csv", index=False, encoding="utf-8-sig")
    print("\n已输出: ak_candidates.csv")


if __name__ == "__main__":
    main()
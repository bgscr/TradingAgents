"""Deterministic provider-shaped text for projection tests; no captured responses."""


def market_projection_case():
    symbol, day, ref = "601328.SS", "2026-10-16", "snapshot:synthetic-market"
    frame = "b" * 64
    fields = {"Open": "8.1", "High": "8.2", "Low": "8.03", "Close": "8.09", "Volume": "289163320.0"}
    indicators = {
        "close_10_ema": "7.90",
        "close_50_sma": "7.36",
        "close_200_sma": "6.85",
        "rsi": "74.58",
    }
    row = ",".join([day, *fields.values()])
    stock = "\n".join(
        [
            f"# Authoritative Market Snapshot for {symbol} from {day} to {day}",
            "# Provider: tushare",
            "# Adjustment basis: qfq",
            f"# Requested date: {day}",
            f"# Effective trading date: {day}",
            f"# Frame SHA-256: {frame}",
            f"# Snapshot ID: {ref}",
            "# Total records: 1",
            "",
            "Date,Open,High,Low,Close,Volume",
            row,
        ]
    )
    snapshot = "\n".join(
        [
            f"## Verified market data snapshot for {symbol}",
            f"- Requested analysis date: {day}",
            f"- Latest trading row used: {day}",
            "- Provider: tushare",
            "- Adjustment basis: qfq",
            f"Frame SHA-256: {frame}",
            f"Snapshot ID: {ref}",
            "### Latest verified OHLCV row",
            "| Field | Value |",
            "|---|---:|",
            *(f"| {k} | {v} |" for k, v in fields.items()),
            "### Verified technical indicators (latest row)",
            "| Indicator | Value |",
            "|---|---:|",
            *(f"| {k} | {v} |" for k, v in indicators.items()),
        ]
    )
    companion = "\n".join(
        [
            f"Effective trading date: {day}",
            f"Frame SHA-256: {frame}",
            f"Snapshot ID: {ref}",
            *(["Provider: tushare", "Adjustment basis: qfq"] * 11),
        ]
    )
    tools = [
        {
            "name": "get_stock_data",
            "args": {"symbol": symbol, "start_date": day, "end_date": day},
            "content": stock,
        },
        {
            "name": "get_verified_market_snapshot",
            "args": {"symbol": symbol, "curr_date": day},
            "content": snapshot,
        },
        {
            "name": "get_indicators",
            "args": {"symbol": symbol, "curr_date": day, "indicator": "rsi"},
            "content": companion,
        },
    ]

    def claim(key, statement, quote):
        return {"claim_id": key, "source_ref": ref, "statement": statement, "source_quote": quote}

    claims = [claim("market.601328.ohlcv.20261016", f"{symbol} closed at 8.09 on {day}.", row)]
    for key in ("Open", "High", "Low", "Volume"):
        claims.append(
            claim(
                "market." + key.lower(), f"{key} was {fields[key]}.", f"| {key} | {fields[key]} |"
            )
        )
    for key in ("close_10_ema", "close_50_sma", "rsi"):
        claims.append(
            claim(
                "market." + key,
                f"The observed value was {indicators[key]}.",
                f"| {key} | {indicators[key]} |",
            )
        )
    notes = [
        {
            "note_id": f"market.n{i}",
            "kind": "provenance",
            "source_ref": ref,
            "statement": "Synthetic source description.",
            "source_quote": q,
        }
        for i, q in enumerate(
            ("# Total records: 1", "Provider: tushare", "Adjustment basis: qfq"), 1
        )
    ]
    return {"tools": tools, "claims": claims, "notes": notes}


def snapshot_claim_case():
    market = market_projection_case()
    ref = market["claims"][0]["source_ref"]
    rows = [
        ("market.trend_ema10", "The 10-day EMA is 7.90.", "| close_10_ema | 7.90 |"),
        ("market.trend_sma50", "The 50-day SMA is 7.36.", "| close_50_sma | 7.36 |"),
        ("market.trend_sma200", "The 200-day SMA is 6.85.", "| close_200_sma | 6.85 |"),
        (
            "market.volume_latest",
            "Volume was 289163320 on 2026-10-16.",
            "| Volume | 289163320.00 |",
        ),
        ("market.rsi_overbought", "RSI is above 70.", "| rsi | 74.58 |"),
    ]
    return {
        "snapshot_text": market["tools"][1]["content"].replace("289163320.0 |", "289163320.00 |"),
        "indicators_text": market["tools"][2]["content"],
        "claims": [
            {"claim_id": key, "statement": statement, "source_ref": ref, "source_quote": quote}
            for key, statement, quote in rows
        ],
    }


def disabled_tools_case():
    tools, notes = [], []
    for name, provider, args in [
        ("get_global_news", "alpha_vantage", {"curr_date": "2026-10-16"}),
        ("get_macro_indicators", "fred", {"curr_date": "2026-10-16", "indicator": "cpi"}),
        ("get_prediction_markets", "polymarket", {"topic": "synthetic topic"}),
    ]:
        content = (
            f"DATA_UNAVAILABLE: {name}; provider={provider}; reason=user_disabled. "
            "No cross-provider fallback. Report this gap; do not treat it as empty "
            "news, absence of transactions, or evidence for a factual claim."
        )
        tools.append({"name": name, "provider": provider, "args": args, "content": content})
        notes.append(
            {
                "kind": "availability",
                "note_id": f"news.{name}",
                "statement": "Provider unavailable.",
                "source_ref": f"{name}:synthetic-unbound",
                "source_quote": content,
            }
        )
    return {"disabled_tools": tools, "news_notes": notes}


def indicator_and_cash_case():
    market = market_projection_case()
    stock, snapshot, indicator = market["tools"]
    ref = market["claims"][0]["source_ref"]
    quote = "2026-10-16: 7.3602"
    indicator["args"]["indicator"] = "close_50_sma"
    indicator["content"] += "\n## close_50_sma values from 2026-10-01 to 2026-10-16:\n" + quote
    cash_quote = "Financing cash flow: -CNY 71,235,000,000"
    cash = {
        "name": "get_fundamentals",
        "args": {"ticker": "601328.SS", "curr_date": "2026-10-16"},
        "content": cash_quote,
    }
    return {
        "tools": [stock, indicator, snapshot, cash],
        "claims": [
            {
                "analyst": "market",
                "claim_id": "market.sma",
                "source_ref": ref,
                "source_quote": quote,
                "statement": "The 50-day SMA was 7.3602 on 2026-10-16.",
            },
            {
                "analyst": "fundamentals",
                "claim_id": "fundamentals.cash",
                "source_ref": "get_fundamentals:601328.SS:2026-10-16",
                "source_quote": cash_quote,
                "statement": "Financing cash flow was -CNY 71,235,000,000.",
            },
        ],
    }

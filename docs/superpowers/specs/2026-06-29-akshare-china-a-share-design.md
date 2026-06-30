# AKShare China A-Share Data Support Design

Date: 2026-06-29
Status: Approved design, pending implementation plan
Branch: codex/akshare-china-a-share-spec

## Context

TradingAgents already routes data tool calls through `tradingagents.dataflows.interface.route_to_vendor`.
The current registered vendors are mainly `yfinance`, `alpha_vantage`, `fred`, and `polymarket`.
Recent local work added `ak_pick_a_stock.py`, which proves a useful Shanghai/Shenzhen ticker mapping:

- Shanghai-style codes such as `601138` and `600895` map to Yahoo `.SS`.
- Shenzhen-style codes such as `000001` and `300750` map to Yahoo `.SZ`.

Live smoke checks showed that Yahoo Finance can return recent OHLCV and some English profile fields
for `601138.SS` and `600895.SS`, but ticker-specific Yahoo news coverage is weak or missing for
China A-shares. AKShare is installed in the local virtual environment and can return A-share
OHLCV, ticker news, business descriptions, financial abstracts, and fund-flow data. One AKShare
profile endpoint (`stock_individual_info_em`) threw a shape error in the local environment, so the
implementation must treat AKShare sub-endpoints as independently fallible.

User decisions:

- Prefer free, no-key data sources.
- Use AKShare as the primary China A-share source.
- Use Baostock only as a narrow fallback.
- Keep Yahoo/yfinance as a supplemental source.
- Do not use Tushare in this phase because it needs a token and has points/permission tiers.
- During later manual CLI smoke testing, choose DeepSeek in the model-selection step.

## Goals

1. Accept common China A-share inputs and route them through the same logic:
   `601138.SS`, `601138.SH`, and `601138` must resolve to the same Shanghai instrument.
2. Keep non-China symbols on the existing behavior unless explicitly configured otherwise.
3. Use AKShare for primary A-share price, ticker-news, business, financial-summary, and fund-flow data.
4. Use Baostock only when AKShare cannot provide historical price or basic financial fallback data.
5. Use Yahoo/yfinance for supplemental English profile fields, valuation fields, broad market
   comparability, and optional last-resort price fallback.
6. Make source attribution visible in tool outputs so the agents know which data was primary,
   fallback, or supplemental.
7. Avoid hallucination-prone empty reports by returning explicit unavailable/degraded-data
   messages when a source cannot provide a field.

## Non-Goals

- No paid or token-gated Tushare integration.
- No broad multi-provider abstraction beyond what is needed for AKShare, Baostock fallback, and
  Yahoo supplemental data.
- No major rewrite of analyst agents or the graph.
- No attempt to cover every mainland exchange variant in phase 1. Shanghai and Shenzhen A-shares
  are required. Beijing Stock Exchange support can be added later after verifying Yahoo, AKShare,
  and Baostock conventions.
- No guarantee that all optional data is present for every stock. The system should degrade clearly.

## Instrument Resolution

Add a China A-share resolver near the existing symbol normalization code. The resolver should expose
a small structured result rather than scattering string manipulation across vendors.

Proposed structure:

```python
ChinaAInstrument(
    raw_input="601138.SH",
    yahoo_symbol="601138.SS",
    akshare_code="601138",
    baostock_code="sh.601138",
    market="cn_a",
    exchange="shanghai",
)
```

Resolution rules:

- `.SH` and `.SS` are both Shanghai input aliases and normalize to Yahoo `.SS`.
- `.SZ` is Shenzhen and remains Yahoo `.SZ`.
- Bare six-digit codes are inferred by prefix:
  - Shanghai: `600`, `601`, `603`, `605`, `688`, `900`
  - Shenzhen: `000`, `001`, `002`, `003`, `300`, `301`, `200`
- Unknown six-digit prefixes should not be guessed as Shanghai or Shenzhen. They should remain
  regular symbols or return a clear unsupported-market message.
- Existing aliases for metals, crypto, forex, indexes, and non-China exchange suffixes must keep
  their current behavior.

Acceptance examples:

```text
601138.SS -> Yahoo 601138.SS, AKShare 601138, Baostock sh.601138
601138.SH -> Yahoo 601138.SS, AKShare 601138, Baostock sh.601138
601138    -> Yahoo 601138.SS, AKShare 601138, Baostock sh.601138
600895.SH -> Yahoo 600895.SS, AKShare 600895, Baostock sh.600895
000001.SZ -> Yahoo 000001.SZ, AKShare 000001, Baostock sz.000001
000001    -> Yahoo 000001.SZ, AKShare 000001, Baostock sz.000001
```

## Vendor Routing

Keep the existing `route_to_vendor(method, *args, **kwargs)` public surface. Add market-aware vendor
selection internally:

1. If a method has a ticker/symbol argument and that argument resolves to `market="cn_a"`, use the
   China A-share vendor chain from config.
2. Otherwise use the existing category/tool vendor config unchanged.
3. If a China-specific vendor receives a non-China symbol, it should raise `NoMarketDataError` or
   decline cleanly instead of returning misleading data.

Proposed default China A-share chains:

```python
"market_data_vendors": {
    "cn_a": {
        "core_stock_apis": "akshare,baostock,yfinance",
        "technical_indicators": "akshare,yfinance",
        "fundamental_data": "akshare,yfinance,baostock",
        "news_data": "akshare,yfinance",
    }
}
```

The global defaults stay unchanged:

```python
"data_vendors": {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}
```

This prevents AKShare failures or Chinese-market assumptions from touching US stocks, Hong Kong
tickers, crypto, futures, forex, or other existing instruments.

## Data Source Responsibilities

AKShare primary responsibilities:

- `get_stock_data`: `stock_zh_a_hist`, converted to the existing OHLCV CSV output shape.
- `get_indicators`: reuse stockstats calculations over AKShare OHLCV.
- `get_news`: `stock_news_em`, filtered to the requested date window.
- `get_fundamentals`: aggregate:
  - `stock_zyjs_ths` for main business and product fields.
  - `stock_financial_abstract` for financial metrics.
  - `stock_individual_fund_flow` for recent capital-flow summary.
  - Other AKShare endpoints only when their shape is verified and guarded.

Baostock fallback responsibilities:

- Historical daily OHLCV fallback when AKShare price data fails.
- Basic financial fallback where Baostock exposes the needed fields without paid credentials.
- No news responsibility.
- No broad replacement for AKShare.

Yahoo/yfinance supplemental responsibilities:

- English company name.
- Sector and industry.
- Market cap, PE, price-to-book, beta, 52-week high/low, and similar valuation/profile fields.
- Optional supplemental ticker news when present.
- Last-resort OHLCV fallback after AKShare and Baostock fail.

## Tool Output Shape

Tool outputs should explicitly mark source usage. Example:

```text
# Stock data for 601138.SS from 2026-06-01 to 2026-06-29
# Primary source: AKShare stock_zh_a_hist
# Fallback source used: none
# Supplemental source: Yahoo profile available
```

For fundamentals:

```text
# Company Fundamentals for 601138.SS
# Primary source: AKShare
# Supplemental source: Yahoo/yfinance
# Degraded fields: stock_individual_info_em unavailable (shape mismatch)
```

For optional-source failure:

```text
DATA_DEGRADED: AKShare fund-flow data unavailable (...). Continuing with price,
news, financial abstract, and Yahoo supplemental profile. Do not fabricate the
missing fund-flow values.
```

For core price failure after all configured China A-share vendors:

```text
NO_DATA_AVAILABLE: No usable market data for '601138.SH' (resolved to '601138.SS')
from AKShare, Baostock, or Yahoo. Do not estimate or fabricate values.
```

## News And Macro Handling

Ticker-specific A-share news is in scope for phase 1 through AKShare `stock_news_em`, with Yahoo
as supplemental English coverage when available.

`get_global_news` currently has no ticker argument, so it cannot reliably infer that the current
workflow is analyzing a China A-share. Phase 1 should not introduce hidden global mutable state just
to route this call. Phase 1 will keep `get_global_news` unchanged and rely on ticker-specific
AKShare news for China context. A later enhancement can add an explicit `market` argument to
`get_global_news` and update the news analyst prompt to call it with `market="cn_a"`.

## Error Handling

- Treat price data as core. If AKShare price fails, try Baostock, then Yahoo.
- Treat news, business-description, financial-abstract, fund-flow, and Yahoo profile as enrichment.
  A failure in one enrichment endpoint should degrade only that section.
- Catch and label AKShare endpoint shape errors. Do not allow a single changing endpoint schema to
  abort the full analysis.
- Preserve the existing `NoMarketDataError`, `VendorNotConfiguredError`, and `VendorRateLimitError`
  routing semantics.
- Date-filter all news and financial data that can contain future timestamps. Historical analysis
  must not see future articles or future statements.

## Configuration And Dependencies

Add optional runtime dependencies to project metadata:

- `akshare`
- `baostock`

Because the selected design depends on these sources for the China A-share path, they should be
declared in `pyproject.toml` as regular dependencies for this local fork. A future packaging pass
can move them behind an optional extra such as `pip install "tradingagents[china]"` if base-install
size becomes a concern.

Config should expose China A-share vendor chains without changing non-China defaults. Environment
variable support for nested `market_data_vendors` is a follow-up, not part of phase 1.

## Testing Plan

Unit tests with mocks:

- `601138.SS`, `601138.SH`, and `601138` resolve to the same Shanghai instrument.
- `600895.SS`, `600895.SH`, and `600895` resolve to the same Shanghai instrument.
- `000001.SZ` and `000001` resolve to the same Shenzhen instrument.
- Non-China tickers such as `AAPL`, `0700.HK`, `BTC-USD`, `XAUUSD`, and `EURUSD` keep existing
  behavior.
- A China A-share stock-data request uses `akshare,baostock,yfinance` order.
- A non-China stock-data request still uses the existing configured chain.
- AKShare price failure falls back to Baostock, then Yahoo.
- AKShare enrichment failure returns a degraded-data note without aborting the request.
- News filtering excludes articles outside the requested date window.
- Future financial columns are filtered out for historical analysis.

Manual smoke tests:

- Start with `start_tradingagents.ps1`.
- At model selection, choose DeepSeek as requested by the user.
- Run `601138.SS`, `601138.SH`, and `601138`; verify they use the same China A-share route.
- Run `600895.SH`; verify AKShare ticker news is present even when Yahoo news is empty.
- Run a non-China ticker such as `AAPL`; verify it still uses the existing yfinance path.
- Confirm final reports include visible source attribution and do not fabricate missing fields.

## Implementation Boundaries

Files likely to change during implementation:

- `tradingagents/dataflows/symbol_utils.py`
- `tradingagents/dataflows/interface.py`
- `tradingagents/dataflows/config.py`
- `tradingagents/default_config.py`
- New AKShare dataflow module(s)
- New Baostock fallback module(s)
- `tradingagents/dataflows/stockstats_utils.py` or a narrow OHLCV loader abstraction for indicators
- `tradingagents/agents/utils/agent_utils.py` for China-aware identity enrichment, if needed
- Tests under `tests/`
- `pyproject.toml`
- Optional: `.env.example` and `start_tradingagents.ps1` documentation comments

Implementation should remain surgical. Do not refactor analyst agents beyond the prompt/tool-schema
change needed for optional China market news.

## References

- AKShare stock data documentation: https://akshare.akfamily.xyz/data/stock/stock.html
- Baostock help documentation: https://www.baostock.com/helpDocsHome
- Baostock package: https://pypi.org/project/baostock/
- Tushare permission model: https://tushare.pro/document/1?doc_id=108
- Tushare points/frequency table: https://tushare.pro/document/1?doc_id=290

## Implementation Verification

Implementation must complete the focused unit tests, broader dataflow regressions,
real data smoke for `601138.SH` and `600895.SH`, and manual CLI smoke with DeepSeek.

Verification results from the implementation worktree:

- Focused regression suite:
  `tests/test_symbol_utils.py tests/test_vendor_routing.py tests/test_akshare_data.py tests/test_baostock_data.py tests/test_symbol_normalization_paths.py tests/test_china_dependencies.py`
  passed with 50 tests.
- Broader dataflow regressions:
  `tests/test_no_data_handling.py tests/test_vendor_errors.py tests/test_yfinance_stale_ohlcv_guard.py tests/test_date_boundaries.py`
  passed with 18 tests.
- Real data smoke through `route_to_vendor` returned `601138.SS` stock data from
  `AKShare stock_zh_a_hist` and `600895.SS News` from `AKShare stock_news_em`.
- CLI entrypoint import/help check passed with `python -m cli.main --help`.
- Full interactive DeepSeek CLI smoke remains a manual check after this branch is
  merged or the launcher points at this worktree, because `start_tradingagents.ps1`
  currently hardcodes the primary checkout path.

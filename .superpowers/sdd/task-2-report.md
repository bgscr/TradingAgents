# Task 2 Report: Market-Aware Vendor Routing

## Scope

Executed only Task 2 in `D:\prj\TradingAgents\TradingAgents\.worktrees\akshare-china-a-share-spec`.

## Changed Files

- `tradingagents/default_config.py`
- `tradingagents/dataflows/interface.py`
- `tests/test_vendor_routing.py`
- `tests/test_dataflows_config.py`
- `.superpowers/sdd/task-2-report.md`

## Commits

- `18436b3` `feat: route China A-shares to market vendors`

## Tests Run

### Red phase

Command:

```text
rtk .\.venv\Scripts\python.exe -m pytest tests/test_vendor_routing.py tests/test_dataflows_config.py -q
```

Output:

```text
FF..........F...                                                         [100%]
================================== FAILURES ===================================
FAILED tests/test_vendor_routing.py::VendorRoutingTests::test_china_a_market_chain_falls_back_in_order
FAILED tests/test_vendor_routing.py::VendorRoutingTests::test_china_a_symbol_uses_market_specific_vendor_chain
FAILED tests/test_dataflows_config.py::DataflowsConfigIsolationTests::test_market_data_vendors_default_contains_china_a_chain
3 failed, 13 passed in 1.38s
```

Observed failure reasons matched the brief:
- router still used category defaults for China A-share ticker-scoped calls
- `market_data_vendors` default config was missing

### Green phase

Command:

```text
rtk .\.venv\Scripts\python.exe -m pytest tests/test_vendor_routing.py tests/test_dataflows_config.py -q
```

Output:

```text
................                                                         [100%]
16 passed in 0.60s
```

## Implementation Summary

- Added default `market_data_vendors.cn_a` chains with the exact values from the brief.
- Added ticker-scoped market detection in `tradingagents/dataflows/interface.py` using `resolve_china_a_symbol`.
- Updated `get_vendor(category, method, market)` so precedence is:
  1. `tool_vendors`
  2. `market_data_vendors`
  3. `data_vendors`
- Updated `route_to_vendor()` to select the China A-share market chain for ticker-scoped calls only.
- Added targeted routing tests for China A-share selection, fallback order, non-China symbols, and tool-level override precedence.
- Added a config test to lock the default China A-share vendor chain.

## Self-Review

- Verified tool-level vendor overrides still win over market-specific chains.
- Verified non-China symbols continue using ordinary category defaults.
- Kept market detection limited to the specified ticker-scoped methods.
- Reused existing `resolve_china_a_symbol` behavior, including deliberate rejection of unknown six-digit bare or suffixed codes.
- Kept changes scoped to the Task 2 files only.

## Concerns

- None at this time.

## Review Fix Follow-Up (2026-06-29)

### Issue Addressed

- Production `tradingagents.dataflows.interface.VENDOR_METHODS` did not register the configured China A-share vendors `akshare` and `baostock`, so market-aware routing could skip directly to `yfinance`.
- The routing tests proved a synthetic dispatch table rather than the real production registration shape.

### Fix

- Added narrow temporary placeholder vendor functions in `tradingagents/dataflows/interface.py` for `akshare` and `baostock` that raise `NoMarketDataError(symbol, symbol, "<vendor> vendor not implemented yet")`.
- Registered those placeholders in the real `VENDOR_METHODS` table for:
  - `get_stock_data`
  - `get_indicators`
  - `get_fundamentals`
  - `get_news`
- Tightened `tests/test_vendor_routing.py` so route patches update the real per-method vendor table in place and assert the expected production keys already exist.
- Added a regression test that validates configured China A-share vendor names are present in the real production dispatch table for representative categories.

### Verification

```text
rtk .\.venv\Scripts\python.exe -m pytest tests/test_vendor_routing.py tests/test_dataflows_config.py -q
17 passed in 0.60s
```

### Concerns

- Placeholder vendors intentionally return `NoMarketDataError` until Tasks 3/4/5 replace them with real implementations.

## Review Fix Follow-Up 2 (2026-06-29)

### Issue Addressed

- The first review follow-up still left real China placeholder registrations incomplete for ticker-scoped methods sharing the `fundamental_data` and `news_data` market chains.
- Regression coverage only checked representative methods, so `get_balance_sheet`, `get_cashflow`, `get_income_statement`, and `get_insider_transactions` could still silently filter configured China vendors out of the real dispatch table.

### Red Phase

Command:

```text
rtk .\.venv\Scripts\python.exe -m pytest tests/test_vendor_routing.py tests/test_dataflows_config.py -q
```

Output:

```text
.........F.......
FAILED tests/test_vendor_routing.py::VendorRoutingTests::test_real_vendor_table_registers_configured_china_a_vendors
1 failed, 16 passed in 0.84s
```

Observed failure matched the review finding:
- `get_balance_sheet` was missing `akshare` and `baostock` in the real `VENDOR_METHODS` table.

### Fix

- Expanded the real-table regression to assert configured China vendor names for every ticker-scoped method using a China market-specific category chain:
  - `get_stock_data`
  - `get_indicators`
  - `get_fundamentals`
  - `get_balance_sheet`
  - `get_cashflow`
  - `get_income_statement`
  - `get_news`
  - `get_insider_transactions`
- Added minimal placeholder registrations in `tradingagents/dataflows/interface.py` for the remaining methods:
  - `get_balance_sheet`: `akshare`, `baostock`
  - `get_cashflow`: `akshare`, `baostock`
  - `get_income_statement`: `akshare`, `baostock`
  - `get_insider_transactions`: `akshare`
- Kept `get_global_news` unchanged.

### Verification

```text
rtk .\.venv\Scripts\python.exe -m pytest tests/test_vendor_routing.py tests/test_dataflows_config.py -q
17 passed in 0.58s
```

### Concerns

- Placeholder vendors still intentionally raise `NoMarketDataError(..., "<vendor> vendor not implemented yet")` until later tasks replace them with live implementations.

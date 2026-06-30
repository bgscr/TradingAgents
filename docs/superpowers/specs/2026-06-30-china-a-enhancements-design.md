# China A-Share Enhancements Design

Date: 2026-06-30
Branch: agent/china-a-enhancements
Status: Draft for user review

## Goal

Improve the China mainland A-share workflow without changing the core
TradingAgents graph shape.

This design covers:

- Fixing the analysis-date validation bug where a UTC-8 local machine rejects
  the current China trading date as "future".
- Adding a China A-share enhancement preset selection in the CLI.
- Integrating actionable China A-share data sources for fund flow, trading
  activity, announcements, industry context, and policy/news context.
- Isolating per-run logs so repeated runs for the same ticker/date do not mix
  into one `message_tool.log`.
- Keeping the implementation modular, fail-open, cached, and prompt-size
  bounded.

## Current Findings

The latest inspected run was for `600895.SS` with analysis date `2026-06-30`.

Relevant artifacts:

- Process log:
  `C:\Users\56922\.tradingagents\logs\600895.SS\2026-06-30\message_tool.log`
- Saved report:
  `D:\prj\TradingAgents\TradingAgents\reports\600895.SS_20260630_060816`

Findings:

- The CLI date validator in `cli/main.py` compares against the local machine
  date via `datetime.datetime.now().date()`. On a UTC-8 machine this rejects a
  valid Beijing-date A-share input when China has already advanced to the next
  calendar day.
- The process log is appended for each run under the same ticker/date path.
  The inspected file contained multiple runs for `600895.SS` on `2026-06-30`,
  making "latest execution" analysis ambiguous.
- Existing A-share routing is already useful:
  - Price and indicators can route through Baostock and AKShare.
  - News and fundamentals can route through AKShare and yfinance fallback.
  - Sentiment already uses China-local Eastmoney-style popularity signals.
- Reports are saved as UTF-8 markdown correctly. Some terminal views can display
  mojibake when the terminal encoding is wrong, but the saved report files are
  not corrupted.

## Non-Goals

- Do not create a new China-specific analyst node in LangGraph.
- Do not rewrite the trading graph, state schema, or debate flow.
- Do not ingest full raw announcement or article bodies into prompts.
- Do not change non-China tickers beyond preserving the current date behavior.
- Do not make any third-party data source mandatory for a successful run.

## Selected Approach

Use a modular enhancement package.

The CLI will collect a China A-share enhancement preset only when the ticker is
recognized as a mainland A-share. The selected preset will flow into the normal
run config. Data tools will read the config and add compact enhancement
snapshots to existing analyst inputs.

This keeps the change scoped while allowing future sources to be added behind
the same preset interface.

## China A-Share Detection

Show the preset selector only for mainland A-share inputs, including:

- Yahoo-style `.SS` and `.SZ` tickers.
- Common `.SH` input that normalizes to `.SS`.
- Bare six-digit mainland A-share codes that resolve through
  `resolve_china_a_symbol`.

Do not show this selector for US stocks, Hong Kong stocks, ETFs, futures,
forex, or crypto.

## CLI Presets

Add `china_a_enhancement_preset` with these values:

- `basic`: no new enhancement sources; keep current behavior.
- `flow_sentiment`: fund flow, Dragon-Tiger list, margin trading, ranking,
  heat, and keyword signals.
- `announcements`: company notices, exchange/CNINFO disclosures, earnings
  previews, dividends, buybacks, pledges, reductions, related-party
  transactions, restructuring, litigation, and major contracts.
- `industry_policy`: sector and concept membership, sector fund flow, industry
  context, policy or macro-market news summaries.
- `all`: all of the above with stricter per-category limits.

The user selected the preset-package model rather than individual checkboxes.

## Data Source Plan

Prefer existing installed libraries and the existing vendor-routing style.
AKShare is the primary source for new China-specific enrichment. Baostock stays
focused on A-share historical price/fundamental fallback where it is already
used.

Reference material:

- AKShare quick start and data dictionary:
  https://akshare.akfamily.xyz/tutorial.html
- AKShare stock data documentation:
  https://akshare-hh.readthedocs.io/en/latest/data/stock/stock.html
- AKShare GitHub project:
  https://github.com/akfamily/akshare
- Baostock project and docs:
  https://www.baostock.com/

### flow_sentiment

Candidate AKShare functions:

- `stock_individual_fund_flow`
- `stock_individual_fund_flow_rank`
- `stock_lhb_detail_em`
- `stock_lhb_stock_detail_em`
- `stock_lhb_stock_statistic_em`
- `stock_margin_detail_sse`
- `stock_margin_detail_szse`
- `stock_hot_rank_detail_em`
- `stock_hot_rank_latest_em`
- `stock_hot_keyword_em`

Output should be a compact snapshot with:

- Latest main-force net inflow and ratio.
- Recent fund-flow direction and trend.
- Whether the stock appears in Dragon-Tiger data for the lookback window.
- Margin balance changes when available.
- Heat/rank movement and top keywords.

### announcements

Candidate AKShare functions:

- `stock_individual_notice_report`
- `stock_notice_report`
- `stock_zh_a_disclosure_report_cninfo`

Output should keep only high-value events in a bounded window, defaulting to
30 to 90 days depending on data availability:

- Earnings preview or earnings report.
- Dividend and distribution actions.
- Buyback, reduction, pledge, or unlock notices.
- Restructuring, acquisition, asset sale, related-party transaction.
- Litigation, regulatory inquiry, or major contract.

### industry_policy

Candidate AKShare functions:

- `stock_board_industry_cons_em`
- `stock_board_industry_hist_em`
- `stock_board_concept_cons_em`
- `stock_sector_fund_flow_rank`
- `stock_sector_fund_flow_summary`
- China market or policy/news endpoints from an explicit allowlist. Missing
  functions in the installed AKShare version are skipped with a source-status
  line.

Output should include:

- Industry and concept memberships found for the ticker.
- Sector/concept price or fund-flow context.
- A small set of recent policy/macro/industry headlines or themes.

## New Dataflow Module

Add this module:

`tradingagents/dataflows/china_a_enhancements.py`

Primary public function:

```python
def get_china_a_enhancements(ticker: str, curr_date: str, preset: str) -> str:
    ...
```

Internal shape:

- One small collector per source family.
- A shared source result type with `source`, `status`, `as_of`, `summary`,
  `records`, and `error`.
- A formatter that produces a concise markdown snapshot.
- Cache around source calls using existing `data_cache_dir` conventions.
- No raw long-form document bodies in the returned prompt text.

## Error Handling

All enhancement sources are fail-open.

Rules:

- A single source timeout or schema mismatch does not fail the run.
- A failed source contributes a short source-status line.
- If every source in the selected preset fails, return a short "no enhancement
  data available" snapshot and continue.
- Preserve underlying ticker/date context in error messages without dumping
  stack traces into reports.

Example:

```text
Source unavailable: stock_lhb_detail_em timed out after 5s.
```

## Prompt and Report Size Control

Each preset must return bounded text:

- `flow_sentiment`: default 8 to 12 bullet facts.
- `announcements`: default 5 to 8 material notices.
- `industry_policy`: default 5 to 8 sector/policy facts.
- `all`: combine all categories but cap each category more aggressively.

Summaries should be factual and source-labeled. The LLM should reason from the
summary, not from long raw tables.

## Date Validation

Current bug:

- `get_analysis_date` in `cli/main.py` rejects dates greater than local
  `datetime.now().date()`.
- On UTC-8, mainland China can already be on the next calendar day.

Design:

- Determine whether the selected ticker is a mainland A-share before asking for
  the date.
- For A-shares, compare against `datetime.now(ZoneInfo("Asia/Shanghai")).date()`.
- For non-A-shares, preserve current behavior.
- If the A-share selected date equals the current Beijing date, allow it and
  print a clear warning that same-day or intraday data may be incomplete.
- If the A-share selected date is later than the current Beijing date, reject it
  as future.
- Use Python standard-library `zoneinfo`; add no dependency.

## Logging

Current problem:

- Logs append to `~/.tradingagents/logs/<ticker>/<date>/message_tool.log`, so
  repeated same-day runs mix.

Design:

- Generate `run_id = YYYYMMDD_HHMMSS` at run start.
- Write process log to:
  `~/.tradingagents/logs/<ticker>/<date>/runs/<run_id>/message_tool.log`
- Write first-line run metadata:
  ticker, analysis date, asset type, enhancement preset, and run id.
- Maintain:
  `~/.tradingagents/logs/<ticker>/<date>/latest_message_tool.log`
  as an overwritten copy of the newest run's `message_tool.log`.
- Keep final saved reports under the existing root `reports/<ticker>_<stamp>`
  path unless the user changes the save path.

## Integration Points

`cli/main.py`

- Ask the preset question after ticker classification and before the analysis
  run config is built.
- Only show it for China A-shares.
- Pass the preset through `get_user_selections` and `_build_run_config`.
- Use A-share-aware date validation.
- Write per-run logs.

`tradingagents/default_config.py`

- Add default:
  `china_a_enhancement_preset = "basic"`

Data tools and interface routing:

- Keep public LangChain tool signatures stable. Read the preset from active
  config instead of adding new tool parameters.
- Read active config via the existing config mechanism.
- Append enhancement snapshots to existing data returns:
  - Market/Sentiment: `flow_sentiment`.
  - News: `announcements` and `industry_policy`.
  - Fundamentals: `announcements`.

Analyst prompts:

- Only add minimal wording telling analysts to treat source-labeled China
  A-share enhancement snapshots as supplemental data and to separate short-term
  flow signals from medium-term fundamental signals.

## Testing Plan

Add focused tests for:

- UTC-8 local environment with Beijing date ahead: A-share same Beijing date is
  accepted.
- A-share date later than Beijing today is rejected.
- Non-A-share behavior stays unchanged.
- Non-A-share CLI does not show China preset selection.
- A-share CLI shows preset choices and stores the selected preset in config.
- Two repeated runs for the same ticker/date produce separate run log
  directories.
- `latest_message_tool.log` contains only the newest run.
- Each preset dispatches only its expected collector family.
- Single-source exception or timeout does not fail the aggregate snapshot.
- `all` preset enforces per-category limits.
- Existing A-share price and indicator routing tests continue to pass.

Run at least:

```powershell
rtk pytest tests/test_cli_symbol_handling.py tests/test_ticker_symbol_handling.py
rtk pytest tests/test_akshare_data.py tests/test_baostock_data.py
rtk pytest tests/test_reporting.py tests/test_vendor_routing.py
rtk pytest
```

Narrower commands may be used during implementation, but the final verification
must include the new tests plus relevant existing A-share, CLI, vendor routing,
and reporting tests.

## Rollout Notes

- Default behavior remains compatible because the default preset is `basic`.
- China A-share users get an explicit choice at runtime.
- Fail-open source handling prevents unstable public data endpoints from
  blocking a run.
- Source-labeled summaries make reports easier to audit.
- Per-run logs make future analysis of `start_tradingagents.ps1` executions
  unambiguous.

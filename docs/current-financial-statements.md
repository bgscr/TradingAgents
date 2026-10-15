# CURRENT financial statements

Three ordinary financial tools can use Tushare in CURRENT personal research for
authoritative mainland equities. AKShare's `get_fundamentals` summary is retained.
This opt-in route does not activate the qualified financial workflow or establish
strict historical PIT availability.

The non-secret local profile accepts the following settings:

```json
{
  "market_history_mode": "current",
  "data_usage_mode": "personal_research",
  "mainland_capability_routing_mode": "legacy",
  "tool_vendors": {
    "get_balance_sheet": "tushare",
    "get_income_statement": "tushare",
    "get_cashflow": "tushare"
  },
  "tushare_financial_cache_max_age_seconds": 86400,
  "tushare_financial_force_refresh": false
}
```

This example deliberately has no destination: before live use, the operator must
confirm and explicitly set `tushare_financial_https_endpoint`. Supported protocol
shapes are `https://api.tushare.pro` (POST `/`) and
`https://api.waditu.com/dataapi` (POST `/income`, `/balancesheet`, or `/cashflow`
under `/dataapi`, with `ts_type_name`). Support in code is not a claim that both
destinations have been validated with the operator's account. Never send a token
over HTTP or disable TLS verification. Requests do not follow redirects.

`TUSHARE_TOKEN` is read lazily only when acquisition is needed. Do not put it in the
profile. `tushare_account_scope_label` identifies the account for reuse; change
that non-secret label when switching accounts. Account scope and the three new
scalar settings also have corresponding `TRADINGAGENTS_TUSHARE_*` environment
overrides. The local profile accepts the destination, maximum age (integer 1 to
31,536,000 seconds), and force-refresh boolean, but not credentials or arbitrary
tool overrides.

## Reuse and request cost

The SQLite file is `current-financial-statements.sqlite3` under `data_cache_dir`.
It is separate from the market-history database. Annual and quarterly displays
share each table's response. A completed analysis checkpoint still reuses its
completed logical tool result; to refresh data, start a new analysis.

For a fully covered common announcement window, a new analysis within the age
limit makes zero financial requests. After expiry, requesting all three tables
normally makes three requests, one per table. Thus a daily analysis with the
default 24-hour interval may recheck old reports with three calls per day. This
detects corrections; it does not freeze old annual reports permanently. A longer
age, such as 604800 seconds (seven days), lowers that cost but delays detection.
No timer or background download is created: expiry is checked on demand.

When the requested window extends, only missing announcement intervals are
fetched. Fragmented stale/missing coverage can require more than one interval per
table; each interval has one attempt and stops that table's work on failure.
`tushare_financial_force_refresh=true` rechecks the common window once per table
in an analysis, regardless of annual/quarterly display. Turn it off after the
intended refresh. The existing shared Tushare ceiling (at most 40 calls/minute,
possibly lower by configuration) still applies. Rate-limit responses share a
cooldown; absent Retry-After, the local conservative fallback is 300 seconds.
These are operator policies, not provider quotas or grants of API access.

## Interpreting results

The report labels observations `CURRENT-only`, preserves provider fields, original
and normalized units, report period, announcement dates, and original retrieval
and observation times. It labels quarterly income/cash flow as cumulative values.
The six-year announcement window is not a claim of complete report-period
coverage. Invalid identities/units, insufficient core fields, conflicts, and
future disclosures cannot become usable periods. Missing values are not filled.

If acquisition or cache persistence fails after useful data exists, that data is
retained with visible degradation, stale or missing announcement intervals, and
fixed failure codes. Cache reuse does not relabel an old observation as freshly
retrieved. Historical response observations remain in the cache after refresh.

Official API field references: [income](https://tushare.pro/document/2?doc_id=33),
[balance sheet](https://tushare.pro/document/2?doc_id=36),
[cash flow](https://tushare.pro/document/2?doc_id=44).

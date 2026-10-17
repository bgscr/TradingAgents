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

CURRENT output supports provider-declared industrial/non-bank and bank schemas.
Unknown, insurance, securities and other unsupported company types are unavailable
with `current_financial_unsupported_company_type` when no supported period remains;
mixed rejected rows degrade the view. Industry is not guessed from a ticker.
CSV `value_status=reported` includes genuine zero; `missing` marks an absent core
field with an empty value. Missing optional fields can still be absent from CSV.
The existing completeness gate remains binding: all critical fields and at least
90% of declared core fields. This is not a claim that every requested period exists.

Bank output explicitly labels gross margin, current/quick ratios and inventory
turnover as not applicable. Net interest margin, nonperforming-loan ratio, capital
adequacy and provision coverage require a separate source; this three-statement
route neither supplies nor invents them. No bank-specific metric is derived here.
The presentation binding is versioned separately; the unchanged raw-response cache
scope remains reusable. Old checkpoint files remain unchanged; the changed route
binding can reject their resumption. Start a new analysis to reuse fresh raw cache
under the new presentation, rather than rewriting a historical checkpoint.

## Manual acceptance, pending endpoint decision

`scripts/accept_financial_statements.py` requires an explicit `--ticker`, destination,
non-secret account label, analysis date and fresh external output directory. Default
mode is synthetic and offline. Both processes resolve the pinned source registry;
SH/SS aliases share canonical identity. Synthetic samples cover 600895.SH, 601328.SH
and 601658.SH without changing production industry classification.

Do not run live until the operator explicitly selects the HTTPS destination with
an understood support evidence gap. The public SDK/documentation historically
use HTTP; an HTTPS address being implemented here does not establish official
ordinary REST support or account entitlement. Do not downgrade HTTP or probe a
new destination. No runnable live command is provided while this is unresolved.

Once that decision is resolved, the minimum manual acquisition is **601328.SH only**:
the user enters the token at the hidden prompt on their own machine. The agent
does not handle it. The reservation allows at most three sequential unique table
POST attempts, stops at the first failure, and never retries or redirects. Socket
timeout is 30 seconds; each child has a 180-second parent deadline, including the
prompt. A timeout or interruption is a stopped run, not authority to try again.

The prompt accepts only the Tushare token, not a model API key. Empty input,
whitespace, control characters, listed invisible Unicode ranges, quotes and common
credential wrappers (including `ts.set_token(...)` / `ts.pro_api(...)`) are rejected
before any POST. The invisible-character check covers grapheme joiners, fillers
and variation selectors; it does not infer every font's rendering behavior.
Input is never trimmed, echoed, fingerprinted or
described by length/prefix; no token-format or length guess establishes validity.
The prompt is shown once, and unavailable hidden input stops instead of falling
back to visible input. A rejection does not automatically prompt or send again.

The following fresh offline process must report zero requests and zero client
creation attempts, with different process IDs and identical six view hashes and
original timestamps. This verifies this isolated reservation's durable cache, not
an unseen daily cache, news data, all annual/quarterly periods, or a model analysis.
The script does not call a model. All-success creates `result.json`; failures retain
phase receipts and a stopped `result.json` with safe codes and partial progress.
Unknown request counts after an abnormal child exit are `null`, never assumed zero.
Raw upstream error text and tokens are not written to receipts. Keep failed runs;
do not resume them or treat a new output directory as renewed permission.

Transport-failure receipts additionally retain `provider_code` (a signed 32-bit
integer or null) and a fixed `classification_basis`. HTTP 401/403 retain their
HTTP-derived authentication/permission classifications. No numeric business-code
mapping is qualified here: nonzero business responses use
`financial_https_upstream_rejection`, with `provider_code_unmapped` or explicit
`message_heuristic_authentication`/`message_heuristic_permission` hints. Merely
mentioning a token never proves the token invalid. A rate-limit message still
enforces the existing conservative cooldown, labelled `message_heuristic_rate_limit`.
Malformed response codes are not retained. These diagnostics do not save `msg`,
request bodies or raw responses and cannot reconstruct omitted details of old runs.

Live workers close networking during imports and between sends. A Python audit
gate binds approved stdlib HTTPS events to the selected host, exact connection and
one table attempt; curl_cffi request methods are disabled. This guards trusted
diagnostic code against unplanned network paths, not arbitrary native code, all
packets, concurrent applications or provider enforcement. Only the manual CLI
installs it; ordinary production analysis keeps its existing transport policy.

After successful financial acceptance, a separate user-run model stage still needs
its own endpoint/model choice and budget: one frozen cached input, one model request,
no tools/provider fetches, no retries, a 60-second total deadline, and stop on any HTTP/limit
or parsing error. Its runner and model-specific behavior are not qualified by this
financial diagnostic. Financial or model success cannot guarantee freedom from
account limits or bans; do not combine the two budgets.

Official API field references: [income](https://tushare.pro/document/2?doc_id=33),
[balance sheet](https://tushare.pro/document/2?doc_id=36),
[cash flow](https://tushare.pro/document/2?doc_id=44).

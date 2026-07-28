# Market-data provider rate limits and acceptable-use guidance

Research date: 2026-07-25

## Scope and conclusion

This note covers the mainland daily-history chain used by TradingAgents: AKShare (and the Eastmoney history origin it calls), BaoStock, and Yahoo Finance through `yfinance`. It distinguishes provider-published rules from behavior visible only in client source code.

**No usable numeric request quota was found for any of the three production paths.** In particular, there is no primary-source basis here for claims such as “N requests per second,” “N requests per minute,” or “N requests per day.” Any numeric pacing configured by TradingAgents must therefore be described as a **local safety ceiling**, not a provider allowance or guaranteed safe rate.

| Provider path | Provider-published numeric request rate | Published or first-party guidance | Positive throttle/capacity signal |
|---|---|---|---|
| AKShare → Eastmoney `push2his` | **Not found** | AKShare advises lowering access frequency when requests time out; its statement limits supplied data to academic-research use | No AKShare-specific throttle exception or documented Eastmoney code on this path; a raw HTTP 429 would be strong evidence if captured below the library |
| BaoStock TCP API | **Not found** | The distributed client exposes a “login count limit” error, but publishes no numeric count or request rate | Server error `10001005` means the account login count reached its cap; no request-rate-specific error is defined |
| Yahoo Finance via `yfinance` | **Not found** | Yahoo says API rate limits are discretionary and prohibits excessive use; `yfinance` says the Finance API is for personal use only | HTTP 429 and, during crumb acquisition, a body containing `Too Many Requests`; `yfinance` raises `YFRateLimitError` |

“Not found” is bounded to the primary sources reviewed below. It is not proof that an operator-specific, regional, or unpublished threshold does not exist.

## Repository context

The project dependency floor is AKShare 1.18.64, BaoStock 0.8.9, and yfinance 1.4.1; the inspected environment contained AKShare 1.18.73, BaoStock 0.9.3, and yfinance 1.5.1 ([project dependencies](../../pyproject.toml#L34-L36)). The mainland price chain is AKShare, then BaoStock, then Yahoo ([ADR-0002](../adr/0002-single-provider-authoritative-market-snapshot.md)). Five-year acquisition is on demand, while automatic prewarming is limited to explicitly configured portfolios or watchlists and must share centralized budgets ([ADR-0030](../adr/0030-seed-market-history-on-demand.md)).

## AKShare and its Eastmoney upstream

### What is actually published

- AKShare's official FAQ gives only qualitative operational guidance. For `ReadTimeout`, it recommends rerunning, changing IP, and **lowering data access frequency**; it supplies no interval, concurrency, burst, or daily quota ([AKShare FAQ, lines 30–35](https://github.com/akfamily/akshare/blob/release-v1.18.73/docs/answer.md#L30-L35)). The change-IP suggestion should not be implemented as limit circumvention; the useful design signal is to reduce load and cool down.
- AKShare's official statement says its data is “just for academic research purpose,” is reference-only, and may lose interfaces because of uncontrollable factors ([AKShare statement, lines 150–157](https://github.com/akfamily/akshare/blob/release-v1.18.73/README.md#L150-L157)). The MIT license on library code does not itself grant rights to Eastmoney data.
- No numeric quota or Eastmoney-issued acceptable-use grant for the undocumented history endpoint was identified in the primary material reviewed. Absence of a published quota is not permission to discover one by load testing.

### What the first-party library source establishes

AKShare 1.18.73 attributes `stock_zh_a_hist` to Eastmoney and sends a direct GET to `https://push2his.eastmoney.com/api/qt/stock/kline/get`; it immediately parses JSON and returns an empty frame when `data.klines` is absent ([equity implementation, lines 952–995](https://github.com/akfamily/akshare/blob/release-v1.18.73/akshare/stock_feature/stock_hist_em.py#L952-L995)). The ETF and LOF history functions use the same origin and endpoint ([ETF implementation, lines 237–290](https://github.com/akfamily/akshare/blob/release-v1.18.73/akshare/fund/fund_etf_em.py#L237-L290); [LOF implementation, lines 120–160](https://github.com/akfamily/akshare/blob/release-v1.18.73/akshare/fund/fund_lof_em.py#L120-L160)).

Those functions contain no client-side pacing, quota accounting, exponential backoff, `Retry-After` handling, or throttle-specific exception. They also do not call `raise_for_status()` before JSON parsing. Consequently, observed failure may surface as a timeout/disconnection, JSON-decoding or schema error, or empty frame rather than a typed rate-limit outcome. An empty frame is ambiguous and must remain `NO_DATA`/malformed response unless independent transport evidence proves throttling.

**Design reading:** coordinate by upstream origin, not merely by Python provider name. Equity, ETF, and LOF jobs shown above all consume the same Eastmoney `push2his.eastmoney.com` capacity and therefore belong to one shared budget and cooldown.

## BaoStock

### What is actually published

BaoStock 0.9.3 is distributed by the `baostock` author with `www.baostock.com` as its homepage and describes itself as a China-market historical-data tool ([official PyPI distribution](https://pypi.org/project/baostock/0.9.3/); [0.9.3 source archive](https://files.pythonhosted.org/packages/f3/1d/e62f47577d49d5dd98ff20c2e3fccc318e2b7984a06fb9abb9b1136f046c/baostock-0.9.3.tar.gz)). The reviewed distribution and current public project metadata do not publish a requests-per-period quota or an acceptable-use policy. That absence must not be interpreted as unrestricted production or commercial permission.

### What the distributed client source establishes

The source archive's `baostock/common/contants.py` defines:

- `BSERR_LOGIN_COUNT_LIMIT = "10001005"` (“account login count reached upper limit”), but no numeric cap;
- network/connect/send/receive failures in the `10002001`–`10002008` range;
- `BSERR_ORDER_TO_UPPER_LIMIT = "10004016"`, a result/order-size cap rather than a requests-per-time quota;
- `BSERR_SYSTEM_ERROR = "10005001"`; and
- `BAOSTOCK_PER_PAGE_COUNT = 2000`, a pagination size, **not** a rate limit.

The client uses a process-global TCP socket to `public-api.baostock.com:10030`. Query and login responses carry provider-defined `error_code` and `error_msg`; there is no HTTP status or `Retry-After` header and no request-rate-specific constant in the distributed client. Generic socket/system failures are therefore not proof of throttling.

TradingAgents already serializes BaoStock sessions inside one process, but logs in and out for each wrapper session ([local wrapper, lines 24–54](../../tradingagents/dataflows/baostock_data.py#L24-L54)). That protects the client library's singleton socket only within that process. Multiple CLI processes, foreground analysis, and background maintenance can still create a login or request stampede unless coordination is shared across them.

**Design reading:** treat `10001005` as a provider-capacity outcome that opens a login cooldown and suppresses immediate relogin. Do not invent a numeric login allowance. Treat other nonzero codes according to their actual meaning, retaining the raw code and message; do not relabel every BaoStock error as rate limiting.

## Yahoo Finance and yfinance

### What is actually published

- Yahoo's Developer API Terms state that APIs **may be subject to rate limits at Yahoo's sole discretion** and prohibit request volume Yahoo considers unreasonable, excessive, or abusive. They do not give a numeric limit for the Finance chart/crumb endpoints used by yfinance ([Yahoo Developer API Terms](https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html)).
- Yahoo's general Terms prohibit collecting data from its services by automated means—including robots, spiders, scrapers, and extraction tools—without express prior permission, and restrict reuse of service data ([Yahoo Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)). A low request rate does not cure an authorization problem.
- yfinance is not Yahoo. Its own README says it is an unaffiliated research/educational tool, directs users to Yahoo's terms, and says the Yahoo Finance API is intended for **personal use only** ([yfinance 1.5.1 README, lines 21–26](https://github.com/ranaroussi/yfinance/blob/1.5.1/README.md#L21-L26)).

For a production, shared, or commercial TradingAgents deployment, the safe conclusion is to obtain permission/a licensed market-data source rather than treating throttling controls as authorization.

### What yfinance source establishes

yfinance 1.5.1 recognizes HTTP 429 and also recognizes `Too Many Requests` in crumb-response text, then raises `YFRateLimitError` ([crumb handling](https://github.com/ranaroussi/yfinance/blob/1.5.1/yfinance/data.py#L250-L267)). Its main request path can switch cookie strategy and make another request after an HTTP error; it raises `YFRateLimitError` when the resulting response is 429 ([request path, lines 448–471](https://github.com/ranaroussi/yfinance/blob/1.5.1/yfinance/data.py#L448-L471)). The exception only says “Try after a while” and carries no response status, `Retry-After`, or reset timestamp ([exception definition](https://github.com/ranaroussi/yfinance/blob/1.5.1/yfinance/exceptions.py#L51-L53)). Network-exception retries use exponential sleeps, but response throttling is not automatically retried with a provider-specified delay.

The local price wrapper does not currently translate `YFRateLimitError` into TradingAgents' `VendorRateLimitError`; the router only gives typed rate-limit treatment to the latter ([local rate-limit boundary](../../tradingagents/dataflows/interface.py#L357-L376)). A future coordinator therefore needs to normalize the yfinance exception at the source boundary and, if possible, capture raw response headers before the library discards them.

## Safe centralized-coordination implications

The following are engineering safety controls, **not published provider quotas**:

1. **One queue and cooldown per upstream service identity.** At minimum use separate identities for Eastmoney `push2his`, BaoStock's TCP service/account, and Yahoo Finance. All AKShare functions that reach the same Eastmoney origin share one budget. Coordination must span foreground analysis, on-demand seeds, incremental refresh, explicit prewarming, monthly reconciliation, retries, and all local processes.
2. **Start serialized.** A conservative initial concurrency ceiling is one in-flight operation per upstream identity. This is a local default, not evidence that one request at any particular interval is provider-approved. Keep background prewarming disabled unless an operator explicitly configures both permitted scope and a safety budget.
3. **Prioritize demand.** Foreground current analysis—including its required On-Demand History Seed—preempts incremental refresh; explicit watchlist/portfolio prewarming and Full History Reconciliation remain lower priority and pause while a provider is cooling down.
4. **Deduplicate before acquiring a lease.** Single-flight identical provider/instrument/range/Adjustment Basis requests and serve retained history whenever valid. Count physical network attempts, including library-internal cookie/login/page requests, rather than only top-level Python calls.
5. **Classify signals narrowly.**
   - Yahoo HTTP 429 or `Too Many Requests`: `RATE_LIMITED`.
   - A valid `Retry-After` header: persist and honor it; otherwise apply a conservative coordinator cooldown with exponential full jitter.
   - BaoStock `10001005`: provider login-capacity exhaustion; suppress new logins and cool down.
   - HTTP 403, authentication failures, blacklisting, generic timeouts/disconnections, invalid JSON, and empty rows: retain distinct typed outcomes. They may correlate with anti-abuse systems but are not enough by themselves to claim a rate limit.
6. **Share retry budgets and circuit state.** A retry consumes the same upstream budget as first attempts. Bound retries, use full jitter, stop background work first, and open a per-upstream circuit after repeated rate/capacity outcomes. Do not rotate IPs, accounts, cookies, or processes to evade a limit.
7. **Persist operational evidence, not folklore.** Record upstream identity, job class, physical attempt count, start/end time, status/error code, sanitized provider message, `Retry-After`, cooldown decision, and fallback. Never learn a “maximum” by deliberately probing until failure.
8. **Preserve evidence semantics.** A throttle is a Source Acquisition Outcome, never a Source Fact. Skip a provider already in cooldown and proceed through the configured fallback order sequentially; never fan out to all providers or blend partial rows. This remains consistent with [ADR-0012](../adr/0012-separate-source-failures-from-evidence-and-model-retries.md) and [ADR-0029](../adr/0029-degrade-current-analysis-but-fail-closed-replay.md).

## Practical decision

Do not encode a provider “quota table” with invented numbers. Encode configurable local ceilings and persistent cooldowns, label their provenance as `operator_policy`, and keep them below any later contractually supplied limit. If a provider publishes an account-specific limit or grants written permission, store that document/revision separately and allow the operator ceiling to become more restrictive, never silently more permissive.


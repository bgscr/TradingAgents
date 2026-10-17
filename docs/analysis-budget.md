# Optional analysis request budget

All controls default off. Ordinary analysis retains its existing provider,
retry and cache behavior. This feature does not authorize a live experiment.

| Config key | Process environment override | Default |
| --- | --- | --- |
| `analysis_budget_enabled` | `TRADINGAGENTS_ANALYSIS_BUDGET_ENABLED` | `false` |
| `analysis_max_calls` | `TRADINGAGENTS_ANALYSIS_MAX_CALLS` | `30` |
| `analysis_timeout_seconds` | `TRADINGAGENTS_ANALYSIS_TIMEOUT_SECONDS` | `600` |
| `financial_cache_only` | `TRADINGAGENTS_FINANCIAL_CACHE_ONLY` | `false` |

Use a programmatic config or process environment; no personal configuration is
modified automatically. The existing local JSON profile's key allowlist is
unchanged. Enabled budgets require DeepSeek, CURRENT mode, legacy mainland
routing and checkpointing disabled. One budgeted analysis exclusively owns its
process; overlapping budgeted/unbudgeted analysis scopes are rejected in either
start order. Unbudgeted analyses retain their ordinary concurrency behavior.
Use a dedicated process without unrelated network workloads.

## What is counted

One concurrency-safe counter is shared by model and data transport attempts.
OpenAI SDK retries, analyst structured repairs, free-text fallback calls and HTTP
redirects each require another admission. Count is reserved immediately before
the transport operation. A failed connection still consumes an attempt. Once a
limit, deadline, cache-only failure or uncovered transport stops a run, further
sends are rejected even if an intermediate handler catches the stop signal.

The initial coverage is DeepSeek's standard HTTPX clients with transport retries
disabled, AKShare's standard Requests HTTP adapter with transport retries
disabled, and the explicit CURRENT Tushare financial HTTPS client. SDK retries
remain supported and counted. For the AKShare stock-news endpoint, budgeted
invocations retain the SDK request/parser but locally bind its curl-compatible
`get/text` call to standard Requests; disabled runs keep the original SDK transport.
Shared SDK module globals are unchanged. BaoStock, Yahoo/curl elsewhere, other model clients, custom
retrying transports and uncovered sockets stop explicitly. Provider selection
and the existing shared coordinator safety ceilings/cooldowns remain binding;
this counter does not replace them. Unsupported fallback is a terminal stop,
not permission to switch providers. A complete report is not guaranteed.

The CLI activates the budget before evidence acquisition and graph streaming,
and skips its unrelated update-announcement fetch when the budget is enabled.
Programmatic `propagate()` includes deferred reflection calls. Calling a compiled
graph directly is a lower-level API: callers must establish
`analysis_budget_scope(config)` around all acquisition and execution themselves.

## Deadline semantics

The monotonic deadline is checked before admission, at supported socket I/O,
after responses, and before accepting the terminal result. Remaining time caps
supported I/O timeouts and sleeps reached through the guarded sleep functions.
Responses are buffered while their transport is admitted. Late responses are
not accepted as a successful analysis.

This is a cooperative deadline, **not a hard process wall-clock termination**.
Blocking DNS, native TLS operations, captured sleep functions, CPU work and
executor cleanup may return after the deadline. No attempt is made to kill
threads or interrupt database transactions. An existing asyncio loop's exact
internal wakeup sockets are exempt from provider accounting; creating a new loop
inside the guarded scope is not qualified. Offline async coverage uses fake
transport/coroutine and wakeup-socket tests, not a live event loop or service.

If a later experiment requires a hard wall-clock kill bound, it needs a separate
supervised-process design and validation before execution. Do not describe this
setting as that stronger guarantee.

## Read-only financial statements

`financial_cache_only=true` requires CURRENT legacy routing, checkpointing off,
and explicit `tushare` selection for all three statement tools. It conflicts with
forced refresh. It can be enabled independently of the model/data budget.

The cache is opened with SQLite `mode=ro`, `immutable=1` and `query_only`; absent
files/directories are not created. An existing WAL is rejected because immutable
reads cannot safely incorporate its uncheckpointed content. Nothing is migrated,
refreshed or written. Missing/corrupt data, stale or future observations and an
uncovered requested announcement window cause a terminal stop before client or
credential lookup. Freshness uses real UTC time on each read, even if a caller
supplies a fixed analysis date or a reconstruction clock. TTL is not extended.

Dispatcher duplicate reuse, including graph-node ledger reconstruction, rereads
the exact scope and compares the deterministic result including original
observation timestamps, announcement windows and payload digests. Changed cache
observations cannot validate and then return an older result. These CURRENT
observations do not establish strict historical point-in-time evidence or
complete report-period coverage.

## Receipts and a later experiment

CLI `run_status.json` includes `analysis_budget` with limits, admitted attempts,
model/data subtotals, terminal stop reason and financial cache-hit identities.
Programmatic callers get the final receipt in `final_state["analysis_budget"]`
and `graph.last_analysis_budget` (also available after a stopped run). Receipts
contain no prompts, credentials or response payloads. Existing coordinator and
logical-call telemetry have distinct meanings; the budget receipt is the
admission authority. Financial cache receipts retain original timestamps/hashes.

A conservative starting proposal for a separately approved shallow run is 30
combined attempts, a 600-second cooperative deadline, SDK retries set to zero,
financial cache-only, and no checkpoint. This leaves some room above the roughly
11 base logical model calls plus tool turns/repairs, but data calls consume the
same 30 and may exhaust it. Select and record both exact DeepSeek model IDs and
the actual limits before that future run. No live run is performed by the
offline tests or by setting these defaults.

Request counts are **not an exact monetary cap**: prompt/output token lengths,
reasoning, cache billing, provider prices and charges for failed attempts vary.
The current DeepSeek passthrough does not establish a whole-run token or cost
ceiling. The request budget must never be presented as one.

# BaoStock Session Serialization Design

Date: 2026-07-13

## Context

Run `20260712_220151` for `600895.SS` stopped after the market analyst emitted
three `get_indicators` calls in one response. LangGraph's default `ToolNode`
executes sibling tool calls concurrently. The China A-share vendor chain had
already fallen back from AKShare to BaoStock, so multiple calls entered the
BaoStock adapter at the same time.

BaoStock stores its connection in one process-global `default_socket`. Its
blocking receive loop has no request-level isolation, so overlapping
login/query/logout sessions can replace or close the socket another request is
using. The graph then waits indefinitely for the tool node to finish and cannot
yield a new state for the CLI to render. The user eventually closed the
PowerShell window, leaving the run status as `running` because a terminated
process cannot update its artifacts.

The same symbol completed successfully in earlier runs when the model batched
all indicators into one tool call. Model compliance is therefore useful for
performance but cannot be the correctness boundary.

## Goals

1. Prevent overlapping BaoStock sessions within one TradingAgents process.
2. Protect every current and future BaoStock caller, not only the market analyst
   path that exposed the bug.
3. Preserve concurrency for providers and tools that are safe to run in
   parallel.
4. Prove that serialization survives both normal completion and exceptions.
5. Keep the change small and compatible with the existing vendor fallback and
   indicator-cache behavior.

## Non-goals

- Do not serialize every market tool globally.
- Do not introduce a generic rate-limiter, retry framework, or provider worker
  process without a concrete provider contract that requires it.
- Do not change the China A-share vendor order or disable BaoStock fallback.
- Do not change indicator values, report semantics, prompts, or LangGraph
  topology.
- Do not attempt to repair status files after a PowerShell window is forcibly
  closed. Normal exceptions and `KeyboardInterrupt` already use the CLI's
  failure-status path.
- Do not claim protection from an unrelated standalone network outage in which
  a single vendor call itself never returns. Hard cancellation requires process
  isolation or vendor-supported timeouts and is a separate design.

## Approaches Considered

### Recommended: provider-scoped BaoStock session lock

Add one module-level reentrant lock in `baostock_data.py` and hold it across the
entire login/query/logout context. This places synchronization at the unsafe
resource boundary, covers all BaoStock entry points, and leaves unrelated tools
free to run concurrently.

The lock is process-scoped, matching BaoStock's process-global socket. A
reentrant lock avoids self-deadlock if a future BaoStock operation composes
another adapter operation on the same thread.

### Serialize the market ToolNode

Run every market tool sequentially. This avoids the observed overlap but slows
safe AKShare, Yahoo, snapshot, and local-computation work. It also leaves direct
or future non-market BaoStock callers exposed. This is rejected because the
coordination boundary is too broad and incomplete.

### Isolate vendor calls in worker processes with hard deadlines

Execute every vendor request outside the graph process and terminate workers
that exceed a deadline. This can bound arbitrary network hangs, but it adds
process lifecycle, serialization, cache, and Windows packaging concerns. It is
not justified by the observed shared-socket race and is deferred.

## Component Design

### BaoStock adapter

Define a private `_BAOSTOCK_SESSION_LOCK = threading.RLock()` beside the adapter
session helper. `_session()` acquires the lock before `bs.login()` and releases
it only after the `finally` block has attempted `bs.logout()`.

The protected critical section is intentionally the complete session:

1. acquire the BaoStock session lock
2. suppress BaoStock login output and log in
3. validate the login result
4. yield to the caller for all query and row-consumption work
5. suppress BaoStock logout output and log out in `finally`
6. release the lock

Lock acquisition must occur before login because login creates and publishes
the global socket. Holding it through logout prevents another request from
using a socket that the first request is about to close.

No public API or configuration key is added.

### Future provider concurrency controls

Provider safety stays in the dataflow adapter rather than in the market agent or
graph topology. This permits each vendor to use the control its contract needs:

- exclusive lock for process-global or non-thread-safe sessions
- pacing/rate limiter for calls-per-period quotas
- bounded retry for documented transient failures
- process isolation only when hard cancellation is required

These policies are not interchangeable. A global market-tool lock would reduce
throughput without correctly modeling provider-specific quotas. This patch uses
an explicitly named BaoStock lock so future controls can follow the same
provider-scoped placement without introducing a speculative framework now.

## Error Handling

- A login failure raises the existing `VendorNotConfiguredError` while the
  context manager releases the lock.
- A query exception still enters `_session()`'s `finally` block, attempts
  logout, and then releases the lock.
- A logout exception retains the existing behavior; lock release is guaranteed
  by the lock context manager.
- Waiting callers proceed after the active session exits, regardless of whether
  that session succeeded or failed.

## Test Design

Add focused unit tests in `tests/test_baostock_data.py` using fake BaoStock
functions and real Python threads; no network access is permitted.

1. Start one BaoStock request and hold it inside the fake query.
2. Start a second request while the first is active.
3. Assert the second request has not entered login/query before the first is
   released.
4. Release the first request, join both threads with bounded waits, and assert
   both complete with a maximum of one active session.
5. Force a query exception in one request, then run another request and assert
   it can acquire the session and complete, proving lock release on failure.

The concurrency regression test must fail on the pre-fix implementation because
the second fake session overlaps the first, then pass after the lock is added.

## Acceptance Criteria

1. At most one BaoStock login/query/logout session is active per process.
2. Concurrent callers are serialized without changing their results.
3. An exception in one session does not permanently block later sessions.
4. Existing BaoStock formatting, logout, indicator, caching, and vendor-routing
   tests remain green.
5. Existing market-tool concurrency remains unchanged for non-BaoStock work.
6. The implementation adds no generic rate-limiting abstraction or user-facing
   configuration.

## Verification Plan

- Run the new concurrent-session regression test before implementation and
  confirm it fails because sessions overlap.
- Run the new tests after implementation and confirm they pass.
- Run `tests/test_baostock_data.py`, `tests/test_vendor_routing.py`, and
  `tests/test_market_toolnode.py`.
- Run the full test suite.
- Review the task first for specification compliance and then for code quality,
  with concurrency correctness treated as the primary risk.

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
4. Bound how long a caller waits for a BaoStock session already held by another
   thread, so one stuck holder does not create an unbounded queue.
5. Prove that serialization survives normal completion, contention, timeout,
   and exceptions.
6. Keep the change small and compatible with the existing vendor fallback and
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
  the lock-owning vendor call itself never returns. The acquisition timeout
  protects waiting callers, but hard cancellation of the holder requires
  process isolation or vendor-supported network timeouts and is a separate
  design.

## Approaches Considered

### Recommended: provider-scoped BaoStock session lock

Add one module-level non-reentrant lock in `baostock_data.py` and hold it across
the complete login/query/eager-row-extraction/logout context. This places
synchronization at the unsafe resource boundary, covers all BaoStock entry
points, and leaves unrelated tools free to run concurrently.

The lock is process-scoped, matching BaoStock's process-global socket. It must
not be reentrant: a nested session on the same thread would otherwise log in
again, overwrite the outer session's global socket, and close that socket when
the inner session logs out. Nested access instead follows the same bounded
acquisition-timeout failure path as cross-thread contention.

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

Define a private `_BAOSTOCK_SESSION_LOCK = threading.Lock()` and a private
30-second acquisition-timeout constant beside the adapter session helper.
`_session()` calls `acquire(timeout=...)` before `bs.login()`. If acquisition
fails, it logs a warning and raises built-in `TimeoutError`; the existing vendor
router treats that as a vendor failure and continues to the next configured
provider. A dedicated vendor-timeout exception is not added because the current
error taxonomy creates new types only for distinct router reactions.

After successful acquisition, `_session()` releases the lock in an outer
`finally` block after the inner session cleanup has attempted `bs.logout()`.

The protected critical section is intentionally the complete session:

1. acquire the BaoStock session lock
2. suppress BaoStock login output and log in
3. validate the login result
4. execute only the adapter's query and eagerly pull all result rows into an
   in-memory list
5. suppress BaoStock logout output and log out in `finally`
6. release the lock

Lock acquisition must occur before login because login creates and publishes
the global socket. Holding it through logout prevents another request from
using a socket that the first request is about to close.

The context manager's `yield` is an internal synchronous control boundary, not
a generator exposed to an agent. The current `get_stock_data()` and
`_load_ohlcv_cached()` implementations already drain `rs.next()` completely
inside `_session()`, then perform DataFrame conversion, indicator calculation,
CSV/text formatting, and all agent/LLM work after the lock is released. The
implementation must preserve this narrow network-I/O-only contract; no new
row-fetching abstraction is required for this patch.

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
- A caller that cannot acquire the session lock within 30 seconds logs the
  contention and raises `TimeoutError`, allowing `route_to_vendor()` to try the
  next configured provider.
- A query exception still enters `_session()`'s `finally` block, attempts
  logout, and then releases the lock.
- A logout exception retains the existing behavior; lock release is guaranteed
  by the lock context manager.
- Waiting callers proceed after the active session exits, regardless of whether
  that session succeeded or failed.

## Test Design

Add focused unit tests in `tests/test_baostock_data.py` using fake BaoStock
functions and real Python threads; no network access is permitted.

1. Start one BaoStock request and hold it inside the fake query using events,
   not a multi-second sleep.
2. Start three additional callers while the first is active.
3. Assert none of the waiting callers enters login/query before the first is
   released.
4. Release the first request, join all threads with bounded waits, and assert
   all complete with a maximum of one active session and symbol-specific rows
   that show no cross-contamination or data loss. Do not assert FIFO ordering;
   Python locks do not guarantee waiter fairness.
5. Hold the lock and temporarily reduce the acquisition timeout in the test;
   assert a waiting caller raises `TimeoutError` without attempting login.
6. Attempt a nested same-thread session with a reduced timeout and assert it
   fails explicitly instead of re-entering and replacing the global socket.
7. Force a query exception in one request, then run another request and assert
   it can acquire the session and complete, proving lock release on failure.

The concurrency regression test must fail on the pre-fix implementation because
the second fake session overlaps the first, then pass after the lock is added.

## Acceptance Criteria

1. At most one BaoStock login/query/eager-row-extraction/logout session is
   active per process.
2. Concurrent callers are serialized without changing their results.
3. Nested session access cannot re-enter and replace the global socket.
4. Waiting callers fail within the configured 30-second acquisition bound when
   another session does not release the lock.
5. An exception in one session does not permanently block later sessions.
6. Existing BaoStock formatting, logout, indicator, caching, and vendor-routing
   tests remain green.
7. Existing market-tool concurrency remains unchanged for non-BaoStock work.
8. The implementation adds no generic rate-limiting abstraction or user-facing
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

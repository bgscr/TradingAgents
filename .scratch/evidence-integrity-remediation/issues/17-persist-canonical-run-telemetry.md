# Persist canonical run telemetry

Status: completed

Blocked by: 11

## Problem

The reviewed runs logged `circuit_open` results but reported zero circuit-breaker
events. Terminal metrics reconstruct counts from final Evidence State, which
does not retain every attempt. The canonical audit also omits required
stage-level model/tool, token, cost, latency, retry, and circuit telemetry.

## Settled direction

Capture each acquisition event when it occurs in one run-scoped ledger. Finalize
a closed, versioned telemetry projection before calculating the audit digest.
Both CLI and programmatic runners use the same projection; runtime metrics may
contain more operational detail but must agree on overlapping totals.

## Acceptance

- Every attempt, retry, fallback, rate limit, and circuit rejection is retained.
- Provider errors followed by `circuit_open` yield a nonzero circuit count in
  both runtime metrics and the canonical audit.
- Stage model/tool calls, tokens, cost availability, latency, and terminal route
  are present in the canonical audit.
- Unknown cost is unavailable/null, never fabricated as zero.
- Caller-supplied news capabilities remain distinct even when the tool name is
  `get_news`.
- Audit and runtime-metrics overlapping totals reconcile deterministically.
- Telemetry contains bounded metadata, not raw provider payloads or secrets.

## Implementation boundaries

- Primary modules: acquisition controller, market/news routing, graph
  propagation, `tradingagents/decision_audit.py`, `cli/runtime_artifacts.py`, and
  CLI finalization.
- Do not derive retry/circuit totals from final evidence artifacts.
- Use deterministic clocks in audit tests.

## Required tests

- Provider error then repeated circuit rejection.
- Retry with `Retry-After`.
- Multi-provider fallback.
- Capability-preservation test for instrument and sentiment news.
- Audit/runtime consistency test.
- CLI/programmatic telemetry parity.

## Comments

Follow-up to issue 11 and the observed 2026-07-22 metrics mismatch.

## Resolution

- Added a run-scoped, closed telemetry ledger that records every bounded
  acquisition event as it occurs, including retries, fallbacks, rate limits,
  and circuit-open rejections.
- Model and tool callbacks capture their graph stage when each operation starts,
  preventing an operation that finishes after a phase transition from being
  charged to the next stage.
- The canonical audit and runtime metrics now consume the same finalized
  telemetry projection for acquisition totals, stage calls, tokens, duration,
  cost availability, and terminal route. Unknown cost remains unavailable/null.
- Capability identity is preserved independently of shared tool names, and
  programmatic and CLI paths finalize equivalent telemetry.
- Circuit, retry-after, fallback, capability-preservation, stage-attribution,
  audit/runtime reconciliation, and CLI/programmatic parity regressions pass.

Verification:

- Full suite: 1,392 passed, 2 expected skips, 71 subtests passed.
- Ruff and `git diff --check` passed.

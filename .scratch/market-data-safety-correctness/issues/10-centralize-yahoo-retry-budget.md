# 10 — Put Yahoo physical retries under Provider Request Coordinator ownership

Status: ready-for-agent

Blocked by: None — can start immediately

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, and Out of Scope section are binding. [ADR-0031](../../../docs/adr/0031-coordinate-provider-requests-centrally.md) is authoritative.

## Problem being solved

One logical Yahoo request must not hide a second retry budget inside the adapter or transport. Every physical network attempt must be paced, counted, persisted, and audited by the Provider Request Coordinator using the same Upstream Service Identity, single-flight sequence, cooldown, and total attempt budget.

## What to build

Make the Provider Request Coordinator the sole owner of Yahoo physical retry attempts.

- Give the Yahoo adapter exactly one physical attempt per coordinated permit and return a typed attempt result.
- Disable transport/library retries where possible. If unavoidable, route every physical attempt through the same coordinator accounting and pacing boundary before network I/O; reject a transport path whose attempts cannot be exposed or disabled.
- Cap total physical calls at the configured coordinator budget, including the first attempt and every retry.
- Persist and audit attempt index, timestamp, typed outcome, pacing event, cooldown change, and final physical-attempt count by Yahoo's Upstream Service Identity.
- Preserve Yahoo HTTP 429/`Too Many Requests` and valid `Retry-After` as typed capacity outcomes that update shared cooldown.
- Preserve distinct meanings for disconnect, empty frame, authentication failure, malformed response, and other non-capacity failures.
- Keep identical requests single-flighted across the complete retry sequence; sequential fallback begins only after the coordinated result completes.
- Preserve one-in-flight default, Operator Safety Ceilings, priority ordering, persisted cooldown/circuit state, and all ADR-0031 prohibitions.

## Explicit non-goals

- Do not add a second retry framework, nested adapter budget, parallel fallback, request fan-out, identity/origin rotation, or throttling probe.
- Do not represent local pacing as a provider-published quota or treat throttling as a Source Fact.
- Do not reorder the Current Analysis Provider Chain or change retry policy for unrelated upstreams beyond shared coordinator correctness.
- Do not change crypto asset configuration; Ticket 09 owns that behavior.
- Do not refactor unrelated acquisition code.

## Blocking dependencies

None. This ticket blocks the Yahoo-backed crypto CLI portion of Ticket 12 final acceptance.

## External behavior and audit contract

- A fake Yahoo transport that fails three times and succeeds on the fourth makes exactly four physical calls and produces four coordinator/audit attempt records.
- Budget exhaustion makes exactly the configured maximum number of physical calls and returns a typed unavailable outcome without an extra adapter attempt.
- HTTP 429 plus valid `Retry-After` persists shared cooldown and prevents an immediate probe by another caller/process.
- Identical concurrent callers observe one single-flighted retry sequence.
- Non-capacity errors retain their typed distinctions.
- Provider fallback remains sequential and starts only after Yahoo's coordinated outcome is complete.

## Acceptance evidence

- Fake-transport call trace paired one-for-one with persisted coordinator and audit attempt events for success and exhaustion scenarios.
- Shared cooldown state and second-caller result after a deterministic 429/`Retry-After` response.
- Concurrent identical-request trace proving one single-flighted retry sequence and sequential fallback ordering.

## Regression tests

- [ ] N fake transport calls produce exactly N coordinator physical-attempt records and the same audit count.
- [ ] Three retriable failures followed by success produce exactly four attempts within budget.
- [ ] Exhaustion stops exactly at the configured cap with no hidden/nested adapter retry.
- [ ] 429/valid `Retry-After` updates persisted shared cooldown and blocks immediate cross-process probing.
- [ ] Disconnect, empty frame, authentication, malformed response, and other failures retain distinct types.
- [ ] Identical concurrent requests single-flight the entire retry sequence before capacity acquisition.
- [ ] Sequential fallback and priority behavior remain unchanged.
- [ ] One coordinated permit cannot cause more than one physical adapter/transport attempt.
- [ ] Audit attempt indices, outcomes, pacing, cooldown, and final count match transport observations.

## Compatibility or migration requirements

- Preserve existing coordinator persistence, cooldown/circuit state, Operator Safety Ceilings, and Upstream Service Identity keys; do not reset state to adopt the fix.
- Existing reports remain immutable even if their historical attempt counts are incomplete. New reports contain the complete physical-attempt event sequence.
- Any persistence migration is explicit, transactional, and backward-readable or fails typed without discarding request state.

## Acceptance criteria from the specification

- AC9 — success-after-retries physical-attempt and audit accounting.
- AC10 — exact budget exhaustion, shared cooldown, and sequential fallback.
- AC13 — focused/full-suite, lint, ADR-0031 compliance, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_provider_request_coordinator.py tests/test_vendor_errors.py tests/test_market_history_shadow.py tests/test_acquisition_controller.py tests/test_reporting.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Every Yahoo physical attempt has exactly one coordinator permit and persisted attempt event.
- [ ] Retry budgets cap physical traffic with no nested or invisible attempts.
- [ ] Single-flight, cooldown, failure typing, priority, and sequential fallback satisfy ADR-0031.
- [ ] New audit counts equal fake-transport counts under success, exhaustion, and throttling scenarios.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

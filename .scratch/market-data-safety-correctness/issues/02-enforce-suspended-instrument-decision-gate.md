# 02 — Enforce the ADR-0023 suspended-instrument Decision Gate

Status: ready-for-agent

Blocked by: 01

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, and Out of Scope section are binding.

## Problem being solved

Once authoritative evidence confirms that a Mainland Instrument remains suspended, no downstream model, signal, or memory path may turn that non-tradeability fact into Buy, Hold, or Sell output. The deterministic [ADR-0023](../../../docs/adr/0023-block-decisions-for-current-mainland-suspensions.md) terminal contract must be identical at programmatic and CLI boundaries.

## What to build

Use the status-bearing snapshot contract from Ticket 01 to enforce a deterministic Evidence Preflight/Decision Gate before analyst or model stages.

- For authoritative current suspension, finish with `terminal_outcome_kind=analysis_outcome` and exact reason `instrument_currently_suspended`.
- Render the suspension and latest genuinely traded close deterministically, including an explicit unavailable value when Ticket 01 cannot prove a traded close.
- Publish no Buy, Hold, or Sell Trading Decision, position, signal, or other directional field.
- Perform no downstream model/analyst calls, signal publication, or directional-memory write.
- Do not continue acquisition to another provider after the complete selected candidate confirms suspension.
- Fail closed if canonical evidence contradicts or loses the authoritative status delivered by Ticket 01.
- Keep the CLI and programmatic terminal contracts and decision-audit fields equivalent for the same deterministic evidence.

## Explicit non-goals

- Do not infer suspension from blank or zero-volume rows.
- Do not treat suspension as Hold, Insufficient Evidence, model confidence, or a directional premise.
- Do not change Strategy Rules, prompts, Current Analysis Provider Chain order, or model behavior for tradeable Instruments.
- Do not update or erase existing directional-memory records; only suppress the new write for the suspended run.
- Do not add unrelated graph or reporting refactors.

## Blocking dependencies

Blocked by Ticket 01, which establishes the authoritative status and latest-traded-close evidence contract consumed here. This ticket blocks Ticket 12 joint acceptance.

## External behavior and audit contract

- CLI output is a non-directional Analysis Outcome with exact reason `instrument_currently_suspended` and no directional fields.
- Decision audit records BaoStock, authoritative suspension provenance or membership identity, `current_tradeability=suspended`, and the latest genuinely traded close.
- Model-call, tool-call, signal-publication, and directional-memory counters prove zero downstream activity after deterministic preflight.
- Tradeable evidence continues through the existing analysis flow unchanged, with no extra request made solely for status or replay preparation.

## Acceptance evidence

- Captured deterministic CLI and programmatic terminal payloads showing the exact Analysis Outcome kind/reason and absence of all directional fields.
- Spy/counter output proving zero downstream model calls, signal publications, and directional-memory writes.
- Decision-audit fixture containing authoritative suspension provenance and latest genuinely traded close, with a tradeable control run for comparison.

## Regression tests

- [ ] The compiled graph and real CLI callback/runner terminate before every model node for an authoritative suspended fixture.
- [ ] The terminal result and audit contain the exact ADR-0023 kind/reason and no Buy/Hold/Sell, position, or signal fields.
- [ ] Directional-memory state is byte-for-byte unchanged by the suspended run.
- [ ] Signal/publication hooks are not invoked.
- [ ] A tradeable control fixture follows existing analysis behavior without additional provider requests.
- [ ] A status contradiction or loss fails closed rather than invoking a model or emitting Hold.
- [ ] CLI and programmatic entry points normalize to the same terminal and audit contract.

## Compatibility or migration requirements

- No database migration or historical report rewrite is required.
- Existing reports and memory remain immutable; the new gate affects only runs whose current authoritative evidence proves suspension.
- Legacy or status-unknown evidence cannot be silently upgraded to suspension and follows existing typed behavior.

## Acceptance criteria from the specification

- AC1 — complete deterministic suspended CLI/audit outcome.
- AC2 — tradeable control and provider-order compatibility.
- AC13 — focused/full-suite, lint, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_current_tradeability.py tests/test_graph_trust_boundaries.py tests/test_decision_graph_v2.py tests/test_decision_audit.py tests/test_memory_log.py tests/test_cli_run_status.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Authoritative current suspension always yields the exact ADR-0023 non-directional terminal contract.
- [ ] Tests prove zero downstream model calls, zero directional-memory writes, and zero signal publication.
- [ ] Tradeable and unknown controls preserve their existing semantics.
- [ ] CLI and programmatic audit evidence agree.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

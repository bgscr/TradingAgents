# 06 — Prove payload publication/GC concurrency and recovery deterministically

Status: ready-for-agent

Blocked by: 05

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its selected payload-GC protocol, deterministic test matrix, compatibility requirements, acceptance definitions, and Out of Scope section are binding.

Use the prototype artifacts as authoritative design evidence; do not copy their throwaway implementation into production or reopen protocol choice:

- [Prototype decision report](../payload-gc-prototype/REPORT.md)
- [Deterministic results](../payload-gc-prototype/RESULTS.md)
- [Specification amendment](../payload-gc-prototype/SPEC-AMENDMENT.md)
- [Throwaway prototype runner](../payload-gc-prototype/run_prototype.py)

## Problem being solved

Ticket 05 implements the selected protocol, but timing-sensitive tests would not prove its cross-process safety. The production invariant and recovery behavior need deterministic regression evidence at every publication, deletion, unlink, timeout, corruption, and termination boundary.

## What to build

Add a private, default-no-op phase hook at the public store publication/collection boundary and an end-to-end regression matrix using independent main/sidecar connections, separately spawned publisher and GC processes, and named event barriers.

- Expose the minimum phases required by the specification: publisher mutex acquired, file installed, payload row committed, references committed; GC mutex acquired, row deletion committed, before unlink, and after unlink before mutex release.
- Keep the hook inert in production runtime and avoid a general synchronization framework.
- Use `threading.Event`, `multiprocessing.Event`, or an equivalent deterministic barrier. Never use sleeps, polling delays, or elapsed-time races as proof.
- Exercise ordinary orphan collection; all three same-digest republication windows; publisher interruption before final reference commit; two GC workers; different-digest conservative serialization; unlink failure; separate-process termination at both safe residues; idempotent/concurrent publication; every high-level publication path; exact snapshot publication; corruption; timeout; backup/restore; and eventual cleanup/reuse.
- If the matrix reveals an implementation defect, make only the smallest correction consistent with Ticket 05 and the authoritative protocol evidence.

## Explicit non-goals

- Do not redesign or benchmark alternative protocols.
- Do not introduce sleeps, flaky timing thresholds, per-digest locks, tombstones, leases, generation fencing, or platform-specific locking.
- Do not duplicate the throwaway prototype in the production test suite.
- Do not broaden production hooks beyond the minimum inert deterministic seam.
- Do not refactor unrelated storage or test infrastructure.

## Blocking dependencies

Blocked by Ticket 05. This ticket blocks Ticket 12 joint durability acceptance.

## External behavior and audit contract

- Same-digest publishers remain excluded until GC releases the mutex, then complete idempotently with verified bytes and references.
- Different-digest publication is deliberately serialized for this fix and either proceeds after bounded release or returns the specified typed timeout without mutation.
- Process termination automatically releases the SQLite mutex and leaves only the specification's safe recoverable residues.
- GC never reports a failed unlink as collected and never removes bytes referenced by a committed row.
- Corruption stays typed and fail-closed; strict replay never treats missing referenced bytes as a cache miss or substitutes data.
- Deterministic test evidence records database row state, file state, publisher result, GC result, invariant result, and eventual cleanup for every case.

## Acceptance evidence

- A deterministic case matrix recording row state, file state, publisher result, GC result, invariant result, and eventual cleanup for every required interleaving.
- Separate-process traces at both termination boundaries proving automatic SQLite lock release and the specified safe residues.
- Repeated focused-run output with identical case outcomes and explicit confirmation that no sleep-based synchronization is present.

## Regression tests

- [ ] Pause after row deletion/before unlink; prove same-digest publication cannot mutate until release and then succeeds safely.
- [ ] Pause immediately before unlink and repeat the exclusion/republication proof.
- [ ] Pause after unlink/before mutex release; prove the intermediate `row absent, file absent` state and safe subsequent publication.
- [ ] Interrupt/terminate publication after file install and after payload-row commit but before final reference commit; prove GC exclusion and safe eventual cleanup/reuse.
- [ ] Run two GC workers with independent connections/processes; only one mutates and the other rechecks after release.
- [ ] Run GC for digest A and publication for digest B; prove conservative serialization, bounded release, timeout behavior, and correct final states.
- [ ] Inject unlink failure; prove `row absent, file present`, typed failure, and later locked cleanup or reuse.
- [ ] Terminate a separate GC process after row deletion and, separately, after unlink; prove automatic lock release and recovery.
- [ ] Prove repeated and concurrent same-digest publication is idempotent around every blocked interleaving.
- [ ] Run the matrix through Provider History Bundle, provider-frame, calendar, and exact-snapshot publication.
- [ ] Seed missing/wrong-digest/wrong-length files for committed rows; prove fail-closed corruption without repair.
- [ ] Force sidecar timeout and compare database/filesystem state byte-for-byte.
- [ ] Re-run backup/restore and eventual orphan cleanup controls.
- [ ] Run the deterministic subset repeatedly with identical outcomes and no sleep calls.

## Compatibility or migration requirements

- Tests use temporary databases/filesystems only and must not mutate developer or production runtime state.
- Preserve all Ticket 05 compatibility rules for existing databases, payloads, pins, reports, and the sidecar runtime file.
- Process tests must use supported local SQLite locking assumptions and close/terminate children deterministically on failure.

## Acceptance criteria from the specification

- AC6 — complete deterministic concurrency, interruption, timeout, corruption, and recovery matrix.
- AC11 — payload/database/report compatibility and backup/restore controls.
- AC13 — focused/full-suite, lint, deterministic behavior, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q -k "payload_mutation_mutex or payload_gc or payload_publication_concurrency or payload_corruption"
pytest -q tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_market_history_shadow.py tests/test_market_history_calendar_maintenance.py
pytest -q -k "payload_mutation_mutex or payload_gc or payload_publication_concurrency or payload_corruption"
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Every mandatory interleaving runs with deterministic events and independent connections/processes.
- [ ] No concurrency test uses sleeps, polling timing, or elapsed time as evidence.
- [ ] Every case proves the committed-row-implies-verified-file invariant and specified eventual cleanup behavior.
- [ ] Cross-process termination and timeout leave no stale owner and no unsafe mutation.
- [ ] The selected protocol remains unchanged and prototype code is not copied into production.
- [ ] Focused tests pass repeatedly; the full supported suite, Ruff, and diff validation pass.

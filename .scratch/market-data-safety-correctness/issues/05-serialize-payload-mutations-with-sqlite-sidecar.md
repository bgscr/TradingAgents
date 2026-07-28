# 05 — Serialize payload mutation with the global SQLite sidecar mutex

Status: ready-for-agent

Blocked by: 03

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its selected payload-GC protocol, cross-cutting invariants, compatibility requirements, acceptance definitions, and Out of Scope section are binding.

Authoritative protocol evidence:

- [Prototype decision report](../payload-gc-prototype/REPORT.md)
- [Deterministic results](../payload-gc-prototype/RESULTS.md)
- [Specification amendment](../payload-gc-prototype/SPEC-AMENDMENT.md)
- [Throwaway prototype runner](../payload-gc-prototype/run_prototype.py)

## Problem being solved

Payload metadata and content-addressed files have different durability boundaries. Without one exclusion scope spanning publication reference commits and GC unlink, GC can remove a canonical file after the same digest has been republished. The parent specification has resolved this design: implementation must use the selected global cross-process SQLite-sidecar payload-mutation mutex without reopening protocol choice.

## What to build

Implement the selected mutex across every high-level payload-file and payload-metadata mutator.

- Derive one sidecar lock-database path from Ticket 03's canonical Market History Database path so all processes using the store resolve the same mutex.
- Acquire the sidecar `BEGIN IMMEDIATE` mutex before any main-database transaction or canonical payload mutation. Reverse lock order is forbidden.
- Hold one uninterrupted mutex scope from canonical-file install/verification through payload-row registration, every durable reference commit, and final committed row/file digest-and-length verification.
- Make existing separate registration/reference transactions safe only when the same outer mutex spans them.
- Treat a committed payload row with a missing, wrong-digest, or wrong-length file as corruption; fail closed and never silently reinstall or repair it.
- Keep GC scans advisory. For each candidate, acquire the mutex, begin an immediate main-database transaction, recheck every durable reference, delete only a proven-orphan row, commit row deletion, unlink the canonical file, and then release the mutex.
- Apply the same mutex and locked row/reference recheck to unregistered-file sweeping.
- Return typed, no-mutation results for mutex acquisition timeout. Report collection only after unlink succeeds or the path is already absent while locked.
- Cover complete Provider History Bundle, provider-frame, calendar, and exact-snapshot publication paths; no helper may release the mutex before its high-level durable references commit.
- Keep different digests conservatively serialized in separate short per-candidate scopes.

## Explicit non-goals

- Do not redesign the protocol, use per-digest locking, add tombstones/generations/leases, or hold the main database transaction through unlink.
- Do not add a main Market History Database schema migration for F5.
- Do not replace SQLite, redesign content-addressed paths, introduce platform-specific file locks, or move GC into foreground analysis.
- Do not copy prototype implementation code into production; implement the specified invariants using repository abstractions.
- Do not refactor unrelated store, ingestion, or publication code.

## Blocking dependencies

Blocked by Ticket 03 so the canonical Market History Database path and migration envelope are stable before sidecar derivation. This ticket blocks Ticket 06 and Ticket 12.

## External behavior and audit contract

- A committed payload row always has a canonical file that verifies by digest and byte length.
- Publication is complete and idempotent or fails without durable references, leaving at most a verified collectible/reusable orphan.
- GC cannot delete a digest while a publisher can commit/re-register it; candidate state is always revalidated while locked.
- GC interruption after row deletion and before unlink safely leaves `row absent, file present`; interruption after unlink leaves `row absent, file absent`.
- Unlink failure is a typed retryable maintenance outcome and is never reported as successful collection.
- Missing/invalid bytes for a committed row surface as corruption/unavailability under existing current-analysis degradation and strict-replay fail-closed contracts.
- Sidecar timeout mutates neither database nor payload filesystem and is observable as a typed publisher or maintenance outcome.

## Acceptance evidence

- Focused publication/GC results showing the canonical sidecar path, sidecar-first lock order, verified committed row/file, and ordinary orphan cleanup.
- Typed timeout, unlink-failure, and committed-row-corruption results with before/after database and filesystem state.
- High-level publication evidence for bundle, provider-frame, calendar, and exact-snapshot paths showing the mutex spans final durable references.

## Regression tests

- [ ] Ordinary orphan collection deletes metadata before unlink and reports success only after physical removal.
- [ ] Identical publication is content-addressed and idempotent before and after GC eligibility.
- [ ] Publisher rollback/interruption leaves no durable reference and at most a collectible/reusable verified orphan.
- [ ] Bundle, provider-frame, calendar, and exact-snapshot publication each retain one mutex scope through final durable references.
- [ ] The unregistered-file sweep rechecks current metadata/references while locked rather than using a stale digest set.
- [ ] Missing/invalid canonical files for committed rows fail closed without silent repair.
- [ ] Sidecar acquisition timeout is typed and leaves byte-identical state.
- [ ] Backup/restore still verifies the main database and every referenced payload together.
- [ ] Lock acquisition order is sidecar first and main database second on every mutating path.

## Compatibility or migration requirements

- Create/open the sidecar as new runtime coordination state without changing the main database schema.
- Preserve existing snapshot IDs, pins, database rows, payload paths/digests/bytes, reports, and backup evidence. Do not bulk move, rewrite, recompress, or collect during migration/open.
- Configuration aliases must resolve to the same canonical sidecar. Two canonical databases sharing one payload root are rejected or unsupported.
- The sidecar does not replace verification or become evidence; backup/restore continues to validate the main database plus referenced files.
- Preserve local SQLite locking, WAL/full-synchronous main-database behavior, bounded busy timeout, foreign keys, and same-directory atomic payload installation assumptions from the specification.

## Acceptance criteria from the specification

- AC6 — selected-protocol implementation and typed failure portions; Ticket 06 supplies the exhaustive deterministic matrix.
- AC11 — existing database/payload/report preservation and atomic compatibility behavior.
- AC13 — focused/full-suite, lint, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_market_history_shadow.py tests/test_market_history_calendar_maintenance.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Every production payload mutator uses one canonical cross-process sidecar mutex with the required lock order.
- [ ] Publication and GC preserve the committed-row-implies-verified-file invariant and specified safe residues.
- [ ] Timeout, corruption, and unlink failures are typed and do not silently repair or misreport state.
- [ ] No main-schema migration, per-digest lock, or unapproved protocol element is introduced.
- [ ] Ticket 06 can deterministically pause every required publication/GC boundary.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

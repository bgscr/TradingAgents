# 03 — Publish `snapshot:v2` identities with exact atomic pin membership

Status: ready-for-agent

Blocked by: 01

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, and Out of Scope section are binding.

## Problem being solved

An Authoritative Market Snapshot identity must distinguish materially different status, observation, factor, calendar, and provenance membership even when OHLCV bytes are identical. Pins must publish atomically and strict replay must reconstruct only the exact committed membership without substituting newer store state.

## What to build

Introduce new `snapshot:v2:<sha256>` identities derived from the canonical membership manifest defined by the parent specification, and make exact pin publication one collision-safe transaction.

- Include canonical Instrument Identity/revision, provider dataset and Upstream Service Identity, requested/effective dates, Adjustment Basis, normalized frame digest/row count, derivation and normalization versions, ordered session/Raw Market Observation/trading-status revision tuples, canonical factor revision set, applicable calendar revision, provenance class, and every other material validation/derivation input.
- Use Ticket 01's authoritative status identity/value. Current-only live snapshots without durable revision IDs include accepted provider artifact identity plus authoritative status identity/value or an explicit `unknown` marker.
- Canonically order set/sequence fields so exact membership is stable across insertion and input permutations.
- Atomically publish parent metadata, ordered observation/status pairs, factor set, and calendar membership; all commit or all remain absent.
- Make exact republication idempotent. Treat any parent or child mismatch under the same ID as typed identity collision/corruption and leave the existing pin unchanged.
- Make strict replay fail closed for missing/inconsistent parent, membership, revision, or referenced payload data; never repair from current revisions or a live provider.
- Establish one canonical Market History Database path contract that Ticket 05 can use to derive the payload-mutation sidecar path. Do not create a competing path resolver or allow two canonical databases to share one payload root.

## Explicit non-goals

- Do not bulk re-key legacy snapshots, rewrite reports, or infer missing legacy membership.
- Do not change OPIT classification rules; Ticket 04 owns provenance correction.
- Do not implement the payload-mutation mutex or GC protocol; Ticket 05 owns it.
- Do not redesign the content-addressed payload layout, Source Artifact digest, Provider History Bundle boundary, or general storage architecture.
- Do not refactor unrelated schema or snapshot code.

## Blocking dependencies

Blocked by Ticket 01 because status-bearing v2 identity must commit to the authoritative status actually used. This ticket blocks Ticket 04 and establishes the canonical database-path/migration envelope that Ticket 05 must coordinate with.

## External behavior and audit contract

- Two snapshots with identical OHLCV but different status revision identities, Current Tradeability, factor/calendar membership, provenance, or another material input have different v2 IDs.
- Exact identical membership yields the same v2 ID regardless of row/input order and republishes idempotently.
- Conflicting membership under one ID fails atomically with a typed collision/corruption result.
- Strict replay reads only exact pinned observation/status, factor, and calendar membership.
- New audit output identifies the snapshot-ID version and exact pin-membership digest; legacy report content remains unchanged.

## Acceptance evidence

- Persisted manifest/pin inspection showing exact ordered observation/status membership, factor set, calendar revision, and deterministic v2 ID.
- Before/after database evidence proving a conflicting republish fails atomically while exact republish is idempotent.
- Legacy fixture results showing valid v1 reads and typed fail-closed behavior for incomplete/colliding membership without rewritten IDs or reports.

## Regression tests

- [ ] A status-only revision change produces a different v2 ID while identical exact membership is permutation-stable.
- [ ] Exact republish is idempotent.
- [ ] Conflicts in parent fields, observation/status pair, factor set, calendar, or provenance fail atomically without modifying the first pin.
- [ ] Deleting any pinned membership/revision/payload causes typed strict-replay unavailability/corruption with no substitution.
- [ ] Live status-bearing and explicit-unknown manifests produce deterministic distinct identities.
- [ ] Valid legacy v1 IDs/pins remain readable; colliding or incomplete legacy membership fails closed.
- [ ] Failed transactional migration leaves the pre-migration database usable and unchanged.
- [ ] Canonical database path resolution is stable across configuration aliases and is exposed for Ticket 05 without changing the payload root.

## Compatibility or migration requirements

- Readers accept legacy `snapshot:<64-hex>` and new `snapshot:v2:<64-hex>`; every new publication uses v2.
- Use an explicit, versioned, transactional, foreign-key-checked migration compatible with WAL and full-synchronous operation.
- Never bulk re-key or rewrite legacy IDs, pins, databases, payloads, or reports.
- Proven exact legacy membership may produce a separate v2 successor with explicit audit lineage; unsafe legacy state remains typed unavailable/corrupt.
- Opening an upgraded database must not trigger GC or destructive repair.

## Acceptance criteria from the specification

- AC1 — v2 ID and authoritative-status audit portion.
- AC3 — distinct identity, exact pin read, idempotence, and atomic collision failure.
- AC11 — legacy database/pin/report and atomic-migration compatibility.
- AC13 — focused/full-suite, lint, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_market_snapshot.py tests/test_market_history_reconstruction.py tests/test_market_history_store.py tests/test_evidence_chain_regressions.py tests/test_mainland_history_equivalence.py tests/test_current_tradeability.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] All new snapshots use deterministic exact-membership v2 identities.
- [ ] Pin publication is atomic, exact republication is idempotent, and conflicts fail without mutation.
- [ ] Strict replay never substitutes missing or newer membership.
- [ ] Legacy-v1 and migration behavior satisfy AC11 without rewriting historical material.
- [ ] The canonical Market History Database path contract is stable and documented for Ticket 05.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

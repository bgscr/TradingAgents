# Replay recorded evidence and cover the acceptance matrix

Status: completed

Blocked by: 14, 15, 16, 17

## Problem

Tests named after the 2026-07-19 runs construct synthetic evidence with a
different date and preselected rating. They can pass without reproducing the
persisted audited failures. The specification acceptance matrix does not have a
verifiable one-row-to-test mapping.

## Settled direction

Use sanitized, content-addressed recorded artifacts and typed fixture loaders.
Drive them through production adapters, graph gates, direction selection,
terminal contracts, audits, and reporting without live providers or models.

## Acceptance

- Recorded fixtures preserve the 2026-07-19 requested dates, Instrument
  Identity, artifacts, Source Facts, acquisition outcomes, and expected
  fail-closed behavior.
- Fixture replay uses the same trusted interfaces as production callers.
- Misleading synthetic replay names are removed or renamed.
- Every acceptance-matrix row in `spec.md` maps to a named deterministic test.
- Blocked stages prove the required zero call/write counters.
- CLI and programmatic fixture runs produce the same normalized terminal kind
  and validated contract.
- Hypothesis covers permutation, duplication, normalization, malformed
  references, optional-source loss, semantic mutation, and arbitrary ratings.

## Implementation boundaries

- Live-model output equality is not an oracle.
- Do not fetch network data in replay tests.
- Preserve raw fixture bytes separately from normalized projections.

## Required tests

- One integration test per recorded run.
- Table-driven acceptance-matrix mapping test.
- Gate call/write counter tests.
- Checkpoint migration/round-trip coverage from ticket 14.
- Property tests at canonical trusted seams.

## Comments

Follow-up to issue 12 and the 2026-07-22 specification review.

## Resolution

- Added sanitized, content-addressed 2026-07-19 recorded-run fixtures with a
  digest-pinned manifest, typed loader, canonical Evidence State, acquisition
  outcomes, and recorded run telemetry.
- Replay adapts recorded evidence and drives the production preflight, terminal
  contract, audit, and report boundaries without provider, network, or model
  calls. Missing telemetry fails fixture validation instead of being
  reconstructed from final Evidence State.
- Replay audits preserve non-empty acquisition telemetry consistent with their
  recorded acquisition outcomes, and blocked runs prove zero directional
  writes.
- Added a named acceptance-matrix fixture and deterministic mapping tests,
  recorded-run integration tests, checkpoint coverage, and property coverage
  for canonical trusted seams.

Verification:

- Recorded replay and acceptance-matrix suites passed.
- Full suite: 1,392 passed, 2 expected skips, 71 subtests passed.
- Ruff and `git diff --check` passed.

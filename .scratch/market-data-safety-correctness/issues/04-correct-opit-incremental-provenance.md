# 04 — Correct OPIT incremental-refresh provenance from exact merged membership

Status: ready-for-agent

Blocked by: 01, 03

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, and Out of Scope section are binding.

## Problem being solved

A bounded incremental refresh is a delta over retained history, not a claim to repeat every older date. Provenance must be derived from the exact merged revisions and their authoritative availability or first-observed timestamps so request shape cannot misclassify Observed Point-in-Time History as Retrospective Backfill or leak later corrections into earlier replay.

## What to build

At the shadow/history publication boundary, merge candidate revisions with the exact prior Provider History Bundle membership first, then classify the resulting bundle/snapshot from the retained and selected revisions eligible at the requested as-of cutoff.

- Preserve `observed_point_in_time` when every contributing revision qualifies, even when the refresh carries only missing dates and the 21-session Revision Refresh Window.
- Keep the aggregate `retrospective_backfill` when any contributing member is retrospective.
- Treat a genuinely unseen historical effective date as retrospective unless authoritative availability evidence proves OPIT eligibility.
- Exclude a correction first observed after the requested as-of cutoff; earlier replay selects the prior exact revision or fails closed.
- Make classification invariant to request row count, prior/candidate date-set inclusion, ingestion order, and input ordering.
- Persist and audit the exact Ticket 03 v2 membership and resulting provenance class.
- Preserve existing incremental retention, overlap, monthly reconciliation, and immutable revision behavior.

## Explicit non-goals

- Do not relabel existing aggregate provenance or overwrite old revisions in place.
- Do not change the 21-session refresh window, on-demand seed horizon, reconciliation cadence, provider order, or Strict History Provider qualification.
- Do not fetch another provider solely for replay or blend provider bundles.
- Do not alter snapshot identity rules established by Ticket 03 or refactor unrelated ingestion code.

## Blocking dependencies

Blocked by Ticket 01 for status-bearing membership and Ticket 03 for exact v2 pins/strict replay. This ticket blocks Ticket 12 joint OPIT and replay acceptance.

## External behavior and audit contract

- An OPIT seed plus an OPIT incremental delta remains `observed_point_in_time` when exact merged membership proves eligibility.
- A retrospective member prevents upgrade, and a newly seen old date remains retrospective without authoritative availability evidence.
- Replay before a correction's first-observed time excludes it or fails closed; replay at/after eligibility uses its exact revision.
- Audit identifies v2 membership, retained and refreshed revisions, as-of cutoff, and final provenance class deterministically.

## Acceptance evidence

- Deterministic before/after bundle and audit records showing an OPIT seed plus bounded delta retains OPIT using exact merged membership.
- Paired replay results immediately before and at/after a correction's first-observed cutoff.
- Retrospective-member and unseen-old-date controls showing conservative classification without relabeling legacy records.

## Regression tests

- [ ] OPIT seed plus partial OPIT overlap remains OPIT after merge.
- [ ] Omitting older retained dates from the request does not affect classification.
- [ ] A genuinely unseen historical date remains Retrospective Backfill.
- [ ] Any retrospective contributing member prevents OPIT upgrade.
- [ ] As-of cutoffs exclude later-observed revisions and admit them only when eligible.
- [ ] Input, row, and ingestion ordering permutations do not change classification or v2 identity.
- [ ] Strict replay uses exact Ticket 03 membership and fails closed when required membership is absent.

## Compatibility or migration requirements

- Do not relabel legacy bundles or reports in place. Publish a successor only when exact member timestamps/provenance prove the new classification.
- Conservative legacy provenance remains when exact proof is unavailable.
- Existing databases, revisions, payloads, pins, and reports remain readable subject to typed integrity failures.

## Acceptance criteria from the specification

- AC4 — OPIT partial refresh remains OPIT with retained plus refreshed exact membership.
- AC5 — retrospective members and as-of correction cutoffs remain fail-closed and point-in-time correct.
- AC11 — legacy provenance/database/report compatibility.
- AC13 — focused/full-suite, lint, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_market_history_shadow.py tests/test_market_history_reconstruction.py tests/test_market_history_calendar_maintenance.py tests/test_mainland_history_equivalence.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Provenance is computed only from exact merged revision membership and as-of eligibility.
- [ ] Partial OPIT refreshes retain OPIT while retrospective and late-observed controls remain conservative.
- [ ] Audit and strict replay expose the exact v2 membership used.
- [ ] No existing provenance, revision, pin, payload, or report is rewritten.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

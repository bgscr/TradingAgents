# 05 — Select duplicate revisions and overlaps deterministically

**Parent specification:** [Mainland financial reporting-period eligibility and current-only indicators](../spec.md)

**What to build:** Carry duplicate provider revisions and cross-provider
overlaps through immutable retention, deterministic family-period selection,
conflict containment, and audit-ready rendering. Equivalent evidence must not
inflate coverage, unqualified chronology must not be invented, and a critical
disagreement must affect only the exact family-period that contains it.

**Blocked by:** 01 — Add the v2 mainland financial eligibility-policy contract;
03 — Enforce current-only indicator and strict-PIT boundaries; 04 — Preserve
independent family completeness and evidence disposition.

**Status:** ready-for-agent

## Observable behavior

- Every decoded provider artifact and every local revision observation is
  retained before evaluation. Identical bytes may share a content-addressed blob
  while remaining distinct manifest observations.
- Candidate grouping includes company type, family, period, scope, currency,
  and unit. Incompatible candidates are retained/rejected and never field-merged.
- Qualified stable provider filing/revision chronology is used when available;
  strict PIT considers only sequence members whose first-publication and local
  observation are within the cutoff.
- Without qualified chronology, same-provider candidates are ordered by eligible
  `ann_date` descending, `observed_at` descending, `retrieved_at` descending,
  then local revision identity ascending. This is operational determinism, not
  a restatement claim.
- Provider winners are considered in immutable route-rank order. A lower route
  is not called merely to confirm or duplicate already complete targets.
- Compatible candidates whose shared critical and declared-core values agree
  are retained as `equivalent_unselected`; they do not increase coverage.
- Unresolved critical disagreement records a deterministic operational winner
  but marks only that family-period Conflicted and bars its Source Facts.
  Non-critical differences are retained/reported without cross-provider merge.
- `update_flag` is preserved but supplies no ordering or supersession by itself.

## Acceptance criteria

- [ ] **PE9:** Equivalent same-provider revisions and later-provider overlaps are
  all retained; one deterministic winner is selected; alternatives are
  `equivalent_unselected`; coverage is not inflated.
- [ ] **PE10:** One critical disagreement conflicts and blocks only its exact
  family-period; its operational winner is deterministic and all sibling
  selections/artifacts survive.
- [ ] **PE11:** Revisions differing only in `update_flag`, with shuffled row
  order, select identically and produce no chronology/supersession claim.
- [ ] Qualified provider chronology, when present, deterministically supersedes
  earlier eligible revisions and remains cutoff-safe for strict PIT.
- [ ] Local observation ordering is stable for current-only analysis and is
  explicitly labeled operational provenance, not provider restatement history.
- [ ] No selection path merges fields across artifacts or requests a lower
  provider solely for confirmation.
- [ ] Artifact/manifest retention is independent of model citation and includes
  selected, equivalent, superseded, rejected, and conflicted observations.

## Acceptance IDs

- **PE9, PE10, and PE11** are owned in full by this ticket.
- **PE14 (selection slice):** deterministic winners, retained alternatives,
  overlaps, and conflict dispositions carry the v2 policy identity for ticket
  06 persistence/rendering.

## Deterministic regressions

- Identical payload bytes with two revision-observation records.
- Qualified provider revision sequence before/after strict cutoff.
- No-provider-chronology ordering matrix for announcement, observed, retrieved,
  and local-identity tie-breaks.
- Route-rank overlap with equivalent normalized values and coverage-count
  negative control.
- One critical and one non-critical disagreement control in the same fixture.
- `update_flag`-only variants under shuffled response/row/process order.
- Sibling family/period invariance after one conflict.
- Exact physical attempt counts prove no confirmation call or provider fan-out.

## Checkpoint and migration compatibility

- All v2 revision/overlap records are additive and immutable. Existing provider
  rows, payloads, artifacts, manifests, reports, and checkpoints are not
  collapsed, rewritten, or reclassified.
- Content-addressed blob deduplication may not deduplicate revision-observation
  provenance.
- Old records lacking v2 selection identity remain readable under their recorded
  contract and cannot be resumed as v2; ticket 06 owns typed mismatch behavior.
- No provider revision history is backfilled from `update_flag` or local
  observation order.

## Explicit non-goals

- Repeating provider qualification to discover revision semantics.
- Treating `update_flag` as stable chronology or local identity as authoritative
  restatement history.
- Cross-provider field merge, market-row blending, confirmation requests,
  provider fan-out, or speculative prewarming.
- Threshold, Strategy Rule, Evidence Admission, Decision Gate, BaoStock
  authority, or daily provider-order changes.
- Fabricating 2026-06-30 or treating `ann_date` as `f_ann_date`.

## Verification commands

```powershell
pytest -q tests/test_financial_capability_routing_selection.py tests/test_financial_manifest_evidence_reporting.py
pytest -q tests/test_tushare_indicator_adapter.py tests/test_baostock_ratio_adapters.py
pytest -q tests/test_admitted_evidence_binding.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] PE9-PE11 pass under repeated shuffled deterministic fixtures with identical
  canonical results.
- [ ] Every revision remains retained and every equivalent/conflict disposition
  is audit-ready and family-period scoped.
- [ ] Strict cutoff, route order, and exact attempt cardinality remain correct.
- [ ] Spec Compliance and Code Quality reviews have no unresolved Critical or
  Important findings.
- [ ] No historical artifact or provider record was rewritten or deleted.

# 04 — Preserve independent family completeness and evidence disposition

**Parent specification:** [Mainland financial reporting-period eligibility and current-only indicators](../spec.md)

**What to build:** Make a corrected qualified shadow indicator request return
independent, audit-ready outcomes for every requested family across its v2
published targets. A bank's complete growth and cash-flow histories must survive
sparse siblings, a non-bank's complete families must all survive, and aggregate
Degraded/Insufficient/Conflicted status must reflect unchanged material-premise
requirements rather than a single all-families boolean.

**Blocked by:** 01 — Add the v2 mainland financial eligibility-policy contract;
02 — Construct publication-aware annual and reporting targets.

**Status:** ready-for-agent

## Observable behavior

- Every company-type/family/period is assessed independently against exactly
  100% of its declared critical fields and at least 90% of its versioned
  declared core. The denominator cannot be derived from fields populated in the
  current response.
- A family is complete only when every v2 target period for that family passes.
  Its candidates, selections, rejections, missing periods, conflicts, retained
  artifacts, and disposition are not erased by a sibling outcome.
- For the diagnosed 601328 bank fixture, growth and cash-flow families remain
  complete and selected while genuinely sparse siblings remain rejected.
- For the diagnosed 600895 non-bank fixture, every complete requested family
  remains independently selected. Missing `f_ann_date` degrades lineage, not
  breadth completeness.
- Partial family breadth is Degraded only when at least one family is complete,
  every failed family is optional under the active contract/Capability
  Profile/Strategy Rules/material premises, and every used family is complete
  and unconflicted.
- No complete family, a missing required family, a strict request with only
  current-only evidence, or overdue/unproven target construction is
  Insufficient. A critical conflict is Conflicted only for its family-period.
- Evidence Admission, Decision Assertion coverage, Strategy Rules, and the
  Decision Gate continue to decide whether surviving breadth supports a thesis.

## Acceptance criteria

- [ ] **PE7:** The 601328 bank mixture preserves qualified growth/cash-flow
  families and rejects sparse siblings; optional breadth is Degraded, while a
  premise requiring a sparse family is Insufficient.
- [ ] **PE8:** All diagnosed 600895 non-bank families remain complete and
  selected for v2 targets; overall evidence is Degraded only for current-only
  lineage.
- [ ] Critical/core gates remain exactly 100% and at least 90% for every
  family-period and company type.
- [ ] A response-populated-field denominator cannot make a sparse family pass.
- [ ] Family order, provider row order, and sibling failure order do not affect
  selections, completeness, or aggregate disposition.
- [ ] Conflicted status supplied by the family-period selection seam does not
  erase unrelated complete family-periods.
- [ ] A material required-family failure produces a non-directional Analysis
  Outcome under the unchanged Decision Gate; it is never rendered as Hold.

## Acceptance IDs

- **PE7 and PE8** are owned in full by this ticket.
- **PE3 (family-completeness aspect):** current-only degradation does not rewrite
  a complete family's field/period assessment.
- **PE10 (disposition aspect):** consumes a period-local conflict without
  spreading it to sibling families; ticket 05 owns conflict detection/selection.

## Deterministic regressions

- Fixed 601328 bank family matrix with complete growth/cash-flow and sparse
  profit/operation/balance/DuPont controls matching the diagnosis.
- Fixed 600895 non-bank matrix with every declared family complete.
- Threshold boundaries: all critical required; declared core immediately below,
  at, and above 90%; response-populated denominator negative control.
- Optional versus material-required sparse family changes only aggregate
  Degraded/Insufficient result, not family selections.
- Zero-complete-family and current-only strict-scope insufficiency controls.
- Period-local conflict input leaves unrelated family-period outputs stable.
- Shuffled family/candidate ordering produces identical manifests and rendering.

## Checkpoint and migration compatibility

- Add v2 family outcomes without rewriting v1 aggregate assessments or any
  historical report/checkpoint/manifest/artifact/provider row.
- Legacy readers remain supported under legacy contracts; no absent family
  projection is synthesized into an old record.
- Ticket 06 owns durable checkpoint/audit/report version rollout. This ticket
  must provide closed, deterministic family outcomes that can be persisted
  without recomputation or provider calls.
- Snapshots, pins, market-history state, and BaoStock status evidence are outside
  this financial-family migration and remain untouched.

## Explicit non-goals

- Lowering or redefining critical/core thresholds or dynamically declaring a
  core from observed data.
- Changing material-premise requirements, Strategy Rules, Evidence Admission,
  Decision Gate behavior, or treating Insufficient Evidence as Hold.
- Implementing revision chronology/conflict discovery (ticket 05), checkpoint
  mismatch (ticket 06), or authoritative rollout.
- Fabricating 2026-06-30, treating `ann_date` as `f_ann_date`, live provider
  qualification, BaoStock authority changes, or daily provider reordering.

## Verification commands

```powershell
pytest -q tests/test_financial_period_contracts.py tests/test_financial_capability_routing_selection.py
pytest -q tests/test_tushare_indicator_adapter.py tests/test_baostock_ratio_adapters.py tests/test_evidence_gates.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] PE7 and PE8 pass at the dispatcher/evidence seams with diagnosis-derived
  deterministic fixtures.
- [ ] Threshold boundaries are unchanged and executable tests prove family
  independence and material-premise-sensitive disposition.
- [ ] Qualified families survive every unrelated sparse/conflicted sibling case.
- [ ] Spec Compliance and Code Quality reviews have no unresolved Critical or
  Important findings.
- [ ] Historical state and all unchanged authority/decision boundaries remain
  intact.

# 03 — Enforce current-only indicator and strict-PIT boundaries

**Parent specification:** [Mainland financial reporting-period eligibility and current-only indicators](../spec.md)

**What to build:** Carry one publication-eligible indicator period from the
financial dispatcher through current-analysis rendering and Evidence Admission
with an explicit `current_only` scope when `ann_date` is available but
`f_ann_date` is absent. The same retained candidate must fail closed for strict
no-lookahead and historical replay without being relabeled, while the current
analysis remains visibly Degraded.

**Blocked by:** 01 — Add the v2 mainland financial eligibility-policy contract;
02 — Construct publication-aware annual and reporting targets.

**Status:** ready-for-agent

## Observable behavior

- A compatible, complete target candidate with
  `ann_date <= run_as_of_date` may be selected for explicit current analysis and
  carries `availability_scope=current_only` plus
  `availability_basis=ann_date_without_first_publication`.
- The selected projection binds exact announcement metadata, absent
  `f_ann_date`, family/period, artifact and local revision identities,
  observed/retrieved timestamps, and the v2 policy identity.
- Current-only indicator evidence is always visibly Degraded, even when every
  requested family/period is complete. It may enter only a current-analysis
  Source Fact contract that explicitly accepts this scope; mere acquisition or
  completeness does not admit it.
- Strict PIT additionally requires `ann_date` and `f_ann_date` no later than the
  historical cutoff and an exact provider revision observed by that cutoff.
- Missing `f_ann_date` returns typed
  `missing_first_publication_for_strict_pit`, produces Insufficient Evidence for
  a strict request, creates no historical as-of Source Fact, and cannot enter
  strict replay.
- Report/tool/audit projections available at this slice cannot copy, default,
  infer, or describe `ann_date` as `f_ann_date`.

## Acceptance criteria

- [ ] **PE3:** Eight complete published indicator periods with `ann_date` and no
  `f_ann_date` are selected in current-only scope and visibly Degraded without
  losing family completeness.
- [ ] **PE4:** The identical candidates requested for strict historical scope
  are rejected typed; no strict Source Fact, replay input, no-lookahead claim,
  or historical availability claim is produced.
- [ ] A candidate whose `ann_date` is after the run cutoff is unavailable to
  both scopes even if its fields are complete.
- [ ] A strict fixture with both dates and an observed revision inside the
  cutoff remains eligible; moving either `f_ann_date` or observation after the
  cutoff makes it ineligible.
- [ ] Current-only metadata and degradation survive deterministic contract,
  dispatcher, Evidence Admission, and model-facing rendering round-trips.
- [ ] Evidence Admission, Strategy Rules, Decision Assertions, and Decision Gate
  requirements are not weakened or reordered.

## Acceptance IDs

- **PE3 and PE4** are owned in full by this ticket.
- **PE14 (availability-scope slice):** current-only/strict metadata and typed
  rejection carry the v2 identity through the boundaries available before
  ticket 06 finalizes checkpoint/report compatibility.

## Deterministic regressions

- Current-only positive fixture with all `f_ann_date` values absent.
- Same-fixture strict rejection with zero historical Source Facts.
- Inclusive `ann_date` cutoff and future-announcement negative control.
- Strict first-publication and observed-revision boundary matrix.
- Check that serialized/reportable fields retain `f_ann_date=null`; forbidden
  substitution and relabeling controls.
- Acquired-but-unselected/current-only candidates do not become Source Facts
  merely because they exist.
- Non-directional strict insufficiency emits no Buy/Hold/Sell, signal, position,
  or directional-memory write when the missing lineage is material.

## Checkpoint and migration compatibility

- Add current-only/strict fields only to v2-bound contracts. Existing evidence,
  reports, and checkpoint projections remain readable under recorded versions.
- Do not backfill `f_ann_date`, availability scope, or degradation into an old
  artifact or Source Fact.
- Ticket 06 owns checkpoint mismatch and final report/audit version rollout;
  this ticket must expose deterministic data needed by those projections.
- Strict replay of an old/current-only artifact remains fail-closed; no in-place
  state upgrade is permitted.

## Explicit non-goals

- Changing Strategy Rules, Evidence Admission criteria, Decision Gate behavior,
  or making current-only evidence automatically decision-authorizing.
- Family breadth aggregation beyond what is needed to prove PE3/PE4.
- Revision/overlap conflict ordering, checkpoint resume policy, or authoritative
  activation.
- Treating `ann_date` as `f_ann_date`, lowering completeness thresholds,
  fabricating 2026-06-30, or performing live provider qualification.
- Changing BaoStock authority or the mainland daily provider order.

## Verification commands

```powershell
pytest -q tests/test_tushare_indicator_adapter.py tests/test_financial_capability_routing_selection.py
pytest -q tests/test_admitted_evidence_binding.py tests/test_recorded_evidence_replay.py tests/test_evidence_gates.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] PE3 and PE4 pass through dispatcher, evidence, and strict replay seams with
  deterministic fixtures only.
- [ ] Current-only evidence is usable only where explicitly allowed, remains
  Degraded and visible, and can never masquerade as strict PIT.
- [ ] Strict eligibility proves both first publication and revision observation
  by the cutoff.
- [ ] Spec Compliance and Code Quality reviews have no unresolved Critical or
  Important findings.
- [ ] Historical state and all unchanged decision boundaries are preserved.

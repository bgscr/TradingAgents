# 02 — Construct publication-aware annual and reporting targets

**Parent specification:** [Mainland financial reporting-period eligibility and current-only indicators](../spec.md)

**What to build:** Make a corrected qualified shadow request construct its
financial target horizon from compatible publication evidence available by the
run as-of date. Through the existing financial dispatcher/compiled-tool seam,
an unpublished quarter is visibly excluded, a newly announced quarter advances
the frontier exactly on `ann_date`, overdue/unproven publication fails typed,
and an authoritative recent listing produces only the explicit reduced target
set allowed by the parent specification.

**Blocked by:** 01 — Add the v2 mainland financial eligibility-policy contract.

**Status:** ready-for-agent

## Observable behavior

- The dispatcher builds an issuer publication index only from retained,
  successfully decoded, identity/scope/company-type-compatible qualified
  candidates. Sparse field coverage may prove publication but does not prove
  candidate completeness.
- Annual and reporting publication frontiers are independent. The ordinary
  targets are exactly the latest five annual ends and latest eight reporting
  ends through their proven frontiers, newest first; year-end belongs to both.
- `ann_date <= run_as_of_date` is inclusive. A later `ann_date` is
  `announced_after_cutoff`; an absent filing before or on its deadline is
  `not_yet_eligible`; both are excluded from targets and completeness
  denominators.
- An absent filing after its deadline, or an unproven hole behind a later
  publication frontier, returns typed `publication_metadata_unproven` and
  Insufficient Evidence. It is not silently removed to improve completeness.
- On 2026-08-01, an absent 2026-06-30 is `not_yet_eligible`, not missing or
  incomplete. No row is fabricated.
- An authoritative listing exception excludes only period ends before listing,
  records listing provenance/frontiers/reduced targets, and passes only when
  listing age makes five/eight impossible and all post-listing targets qualify.

## Acceptance criteria

- [ ] **PE1:** The diagnosed 2026-08-01 fixture excludes 2026-06-30 from every
  target and family denominator and records `not_yet_eligible` visibly.
- [ ] **PE2:** A 2026-06-30 row with `ann_date=2026-08-15` is excluded on
  2026-08-14 and admitted on 2026-08-15, advancing the reporting frontier once.
- [ ] **PE5:** With annual frontier 2025-12-31 and reporting frontier 2026-03-31,
  targets exactly match the five annual/eight reporting dates enumerated in the
  parent specification.
- [ ] **PE6:** A supported since-listing fixture records the exact reduced
  post-listing targets; removing one required post-listing publication makes it
  Insufficient; pre-listing rows never count.
- [ ] Incompatible candidates cannot establish an issuer publication anchor.
- [ ] A publication hole behind the frontier and an overdue unproven filing fail
  typed rather than becoming not-yet-eligible, missing family data, or an older
  substitute target.
- [ ] Target construction occurs at the existing dispatcher/tool seam and does
  not start an extra provider request solely to discover publication or replay
  evidence.

## Acceptance IDs

- **PE1, PE2, PE5, PE6** are owned in full by this ticket.
- **PE14 (target projection slice):** publication states, frontiers, targets,
  exclusions, and typed target-construction failures carry the v2 identity in
  the dispatcher manifest projection available at this stage.

## Deterministic regressions

- Fixed 2026-08-01 established-issuer fixture with Q2 absent and Q1 published.
- Boundary pair immediately before/on `ann_date` and before/on/after each filing
  deadline.
- Annual/reporting frontier independence and year-end dual-membership fixtures.
- Exact ordered five/eight target assertions, including a hole behind a later
  frontier.
- Identity, consolidation-scope, and company-type incompatibility controls.
- Recent listing: supported, missing post-listing period, unsupported provenance,
  and pre-listing-row controls.
- Shuffled candidate/provider row order produces identical target state and
  manifest identity.
- Physical request cardinality remains unchanged; no confirmation/prewarming
  request occurs.

## Checkpoint and migration compatibility

- V2 target assessments are additive and explicitly policy-bound. They do not
  reinterpret or rewrite `financial-period-selection-v1` assessments.
- Historical reports, checkpoints, manifests, artifacts, provider rows,
  snapshots, and pins remain byte-for-byte unchanged.
- Resume rejection/new-run behavior for a pre-v2 checkpoint is implemented in
  ticket 06; tests here must not silently upgrade an old assessment.
- Legacy target behavior remains readable and unchanged outside the explicit v2
  corrected shadow composition.

## Explicit non-goals

- Current-only versus strict-PIT Source Fact projection (ticket 03).
- Family-level Degraded/Insufficient/Conflicted aggregation (ticket 04).
- Duplicate revision and overlap selection (ticket 05).
- Live qualification, paid models, fabricated filings, calendar-only admission,
  or an extra foreground provider probe.
- Threshold changes, global Tushare-first routing, BaoStock status-authority
  changes, daily provider reordering, Strategy Rule, Evidence Admission, or
  Decision Gate changes.

## Verification commands

```powershell
pytest -q tests/test_financial_period_contracts.py tests/test_financial_capability_routing_selection.py
pytest -q tests/test_tushare_indicator_adapter.py tests/test_baostock_ratio_adapters.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] PE1, PE2, PE5, and PE6 pass through the highest existing financial
  selection seam with fixed clocks and no live dependencies.
- [ ] The exact five/eight construction and every publication state are visible,
  deterministic, and v2-bound.
- [ ] Not-yet-eligible data does not reduce completeness; overdue/unproven data
  fails closed; listing exceptions cannot hide provider sparsity.
- [ ] Spec Compliance and Code Quality reviews have no unresolved Critical or
  Important findings.
- [ ] Legacy and all historical state remain unchanged.

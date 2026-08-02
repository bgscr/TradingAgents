# Mainland financial reporting-period eligibility and current-only indicators

Status: ready-for-agent

## Problem Statement

The current `qualified_v1_shadow` financial target contract treats a mainland
reporting period as required once its calendar period end precedes the run
as-of date. On 2026-08-01 that rule required 2026-06-30 even though neither
qualified provider had published a row for that period. Because every indicator
family was then assessed against the same artificial eight-period target, one
not-yet-published quarter made every family fail aggregate completeness.

The latest qualified shadow diagnosis also established a separate lineage
boundary. Retained indicator periods contain `ann_date`, but every indicator
period lacks `f_ann_date`. Those periods can describe information available to
a current analysis after announcement, but they cannot prove first-publication
availability for strict no-lookahead analysis, strict historical replay, or
historical as-of Source Facts. Treating them as wholly unusable discards
qualified current evidence; treating `ann_date` as `f_ann_date` would silently
weaken the strict point-in-time boundary.

Completeness is currently over-aggregated across unrelated indicator families.
The diagnosis shows that 600895 has qualified complete non-bank families, and
that 601328 has qualified bank growth and cash-flow families while other bank
families are genuinely sparse. Qualified families must survive independently,
without lowering the 100% critical-field or 90% declared-core gates and without
allowing sparse families to masquerade as complete.

Finally, immutable duplicate revisions and cross-provider overlaps need a
deterministic selection rule that preserves every observation, does not mistake
`update_flag` for authoritative restatement chronology, and contains critical
conflicts to the affected family and period. The correction must remain shadow
and non-authoritative, preserve legacy behavior and all historical state, and
make old qualified checkpoints incompatible with the new target contract.

## Solution

Introduce the closed eligibility-policy identity
`mainland-financial-period-eligibility-v2`. Bind it into the qualified routing
plan, target construction, completeness assessment, revision selection,
checkpoint projection, audit, and report. The policy builds annual and
reporting targets from filing publication evidence available by the run as-of
date, uses the versioned mainland filing deadline only to distinguish a period
that is not yet eligible from a publication that is overdue but unproven, and
never requires a quarter merely because its end date has passed.

For current analysis, a compatible indicator candidate with `ann_date` on or
before the run as-of date may be selected under an explicit `current_only`
availability scope. Missing `f_ann_date` forces visible Degraded Evidence and
categorically bars strict-PIT use. Strict mode remains fail-closed and requires
both first-publication and observed-revision eligibility by the historical
cutoff.

Assess each declared indicator family and period independently. Every selected
family-period must retain 100% of its critical fields and at least 90% of its
versioned declared core. A complete family remains selected when a sibling
family is sparse. Partial family breadth is Degraded Evidence only when every
missing family is optional to the applicable evidence contract and proposed
material premises; otherwise it is Insufficient Evidence. Critical conflicts
produce Conflicted Evidence only for the affected family-period.

Retain every provider artifact, local revision observation, candidate, overlap,
and typed disposition. Select deterministically from eligible compatible
candidates using qualified filing chronology when available, local observation
metadata as operational ordering, provider route rank, and local revision
identity as the final stable tie-breaker. Equivalent overlaps remain retained
and unselected. A binary `update_flag` is never chronology by itself.

Keep `legacy` authoritative and `qualified_v1_shadow` non-authoritative.
`qualified_v1` remains disabled. Existing artifacts, reports, checkpoints,
manifests, and provider rows remain byte-for-byte historical records. A
qualified checkpoint that does not commit to the new eligibility-policy
identity cannot resume under the corrected contract and must fail typed before
provider or model work; a caller may then start a distinct new run.

## User Stories

1. As a mainland Equity user, I want unpublished reporting periods excluded from the target set, so that a period is not called missing before it is eligible.
2. As a current-analysis user, I want a period admitted as soon as compatible `ann_date` proves publication by my run date, so that early filings are usable before the general filing deadline.
3. As an operator, I want the same period to remain not-yet-eligible when its `ann_date` is later than the run date, so that future information cannot enter the run.
4. As an operator, I want an absent period whose filing deadline has not passed classified as `not_yet_eligible`, so that it does not reduce completeness.
5. As an operator, I want an absent period whose filing deadline has passed classified as overdue/unproven publication, so that stale provider coverage cannot silently shrink the target set.
6. As a fundamental analyst, I want exactly the latest five eligible annual periods, so that annual history has a stable, publication-aware horizon.
7. As a fundamental analyst, I want exactly the latest eight eligible reporting periods at quarterly cadence including year-end, so that indicator trends have a stable, publication-aware horizon.
8. As a recent-listing issuer user, I want authoritative listing metadata to limit eligible history explicitly, so that pre-listing periods are never fabricated or counted.
9. As an audit reader, I want the listing exception to identify its authoritative date, provenance, and reduced annual/reporting targets, so that it cannot become an implicit threshold waiver.
10. As a current-analysis user, I want announced indicators without `f_ann_date` available as explicitly labeled current-only evidence, so that qualified present-day breadth is not discarded.
11. As a point-in-time researcher, I want current-only evidence rejected from strict replay and historical as-of Source Facts, so that announcement metadata is not overstated as first-publication lineage.
12. As an audit reader, I want missing `f_ann_date` and the resulting degradation visible by family and period, so that current-only evidence cannot silently become strict PIT evidence.
13. As a Source Fact consumer, I want availability scope carried on every current-only projection, so that downstream contracts can enforce the trust boundary deterministically.
14. As a bank investor, I want growth and cash-flow families assessed independently from sparse bank families, so that qualified bank evidence survives unrelated sparsity.
15. As a non-bank investor, I want every complete 600895 indicator family preserved, so that an unpublished quarter or unrelated family cannot erase valid evidence.
16. As a model-risk owner, I want 100% critical and at least 90% declared-core completeness unchanged for every selected family-period, so that remediation does not lower evidence quality.
17. As a model-risk owner, I want the declared core versioned by company type and family, so that the passing denominator cannot be defined dynamically from populated response fields.
18. As a Decision Gate owner, I want missing optional family breadth to be Degraded only when no material premise requires it, so that Degraded Evidence remains honest and Decision-Ready only where allowed.
19. As a Decision Gate owner, I want a missing required family or requested strict lineage to be Insufficient Evidence, so that a degradation label cannot bypass a material requirement.
20. As an audit reader, I want Conflicted Evidence limited to the family-period containing contradictory critical values, so that unrelated periods and families remain reviewable and usable.
21. As an audit reader, I want every immutable provider artifact and local revision observation retained, so that deterministic selection never deletes its alternatives.
22. As an audit reader, I want identical bytes permitted to share content-addressed storage while every revision observation remains represented, so that storage deduplication does not erase provenance.
23. As an operator, I want deterministic revision ordering at a fixed as-of date, so that row order, process order, and provider response order cannot change the selected candidate.
24. As a point-in-time researcher, I want only revisions observed by the historical cutoff eligible for strict PIT, so that later corrections cannot leak into earlier replay.
25. As an audit reader, I want equivalent cross-provider overlaps retained but unselected, so that agreement remains inspectable without inflating coverage.
26. As an audit reader, I want `update_flag` preserved but excluded as standalone restatement chronology, so that a binary marker is not presented as an authoritative sequence.
27. As a legacy user, I want legacy mode and its output contracts unchanged, so that the correction is additive and reversible.
28. As a shadow-run operator, I want qualified output visibly non-authoritative, so that corrected shadow evidence cannot replace legacy-selected financial output.
29. As a checkpoint user, I want an old qualified target contract rejected before work begins, so that a resumed run cannot mix eligibility semantics.
30. As a checkpoint user, I want the option to start a new versioned run after that typed rejection, so that historical checkpoints remain immutable.
31. As an audit reader, I want the routing plan, completeness policy, selection contract, checkpoint, audit, and report to agree on one eligibility-policy version, so that lineage cannot drift across boundaries.
32. As a rollout owner, I want authoritative `qualified_v1` disabled until corrected shadow validation produces both a report and a decision audit, so that activation remains a deliberate post-evidence decision.
33. As a maintainer, I want all eligibility and completeness tests to use deterministic provider fixtures, so that acceptance does not repeat live provider qualification.
34. As a maintainer, I want parent AC1-AC13 to pass unchanged, so that this correction cannot regress market-data safety, strict replay, retry accounting, migration safety, or worktree hygiene.

## Implementation Decisions

### Authoritative diagnosis and policy identity

The latest `qualified_v1_shadow` diagnosis supplied with this specification is
authoritative evidence for the remediation. Implementation must encode, not
re-qualify, these findings:

- neither retained provider had a 2026-06-30 row as of 2026-08-01;
- requiring that period caused every eight-period indicator family to fail;
- retained indicator periods have `ann_date` and all lack `f_ann_date`;
- 600895 non-bank indicator families have qualified complete periods;
- 601328 bank growth and cash-flow families qualify per period while several
  sibling bank families are genuinely sparse;
- duplicate provider revisions and equivalent overlaps exist and must remain
  retained; and
- authoritative `qualified_v1` is blocked.

The new exact policy identity is
`mainland-financial-period-eligibility-v2`. Every non-legacy contract that
constructs, consumes, persists, resumes, audits, or renders financial targets
must carry an `eligibility_policy_version` with exactly that value. A plan or
projection missing the field is not implicitly v2.

### Reporting calendar and publication states

The v2 calendar is closed and deterministic for mainland financial reporting:

- annual period ends are 31 December;
- reporting period ends are 31 March, 30 June, 30 September, and 31 December;
- the 31 December reporting period is supplied by the annual filing and belongs
  to both the annual and reporting sequences;
- the ordinary publication deadlines are 30 April for the prior 31 December,
  30 April for 31 March, 31 August for 30 June, and 31 October for 30 September;
  and
- deadline rules are policy data under the v2 identity, not inferred ad hoc by
  adapters.

For an exact Instrument Identity, compatible consolidation scope, and resolved
company type, construct an issuer-level publication index from every retained,
successfully decoded qualified financial candidate. A sparse or otherwise
field-incomplete candidate may prove that a filing was published, but an
incompatible identity, scope, company type, malformed date, or unqualified
artifact may not. Publication proof never makes the candidate itself complete.

Each standard period has exactly one target-construction state at a run as-of
date:

1. `published_eligible`: at least one compatible retained candidate for the
   exact period has a valid `ann_date <= run_as_of_date`.
2. `announced_after_cutoff`: compatible candidates exist, but every valid
   `ann_date` is later than the run as-of date. The period is not yet eligible.
3. `not_yet_eligible`: there is no compatible publication proof and the
   ordinary filing deadline is on or after the run as-of date. The period is
   excluded from targets and every completeness denominator.
4. `publication_overdue_unproven`: there is no compatible publication proof
   and the ordinary filing deadline precedes the run as-of date. Target
   construction fails typed with `publication_metadata_unproven`; the period
   may not be silently classified as not-yet-eligible or removed to improve
   completeness.
5. `pre_listing`: authoritative listing metadata places the period end before
   listing. It is excluded only through the explicit listing rule.

Date equality is inclusive for publication (`ann_date <= run_as_of_date`) and
conservative for a missing filing on its deadline: without an announcement row,
the deadline date itself remains `not_yet_eligible` because a date-only run has
no end-of-day publication proof.

### Exact annual and reporting target construction

Target construction occurs once per run after immutable candidates needed by
the configured financial acquisition have been retained and before any family
aggregate is declared complete. It does not initiate calls to an extra provider
solely to prepare replay, discover duplicates, or prewarm evidence.

1. Validate the authoritative listing date and provenance when present. A
   listing date without authoritative provenance is not an exception input.
2. Build separate ordered standard sequences for annual ends and all reporting
   ends through the run as-of date.
3. For each sequence, find its latest `published_eligible` period. This is the
   annual or reporting publication frontier. A later period classified
   `announced_after_cutoff` or `not_yet_eligible` does not advance the frontier.
4. The ordinary annual target is the frontier and its four immediately
   preceding standard annual ends. The ordinary reporting target is the
   reporting frontier and its seven immediately preceding standard reporting
   ends, including year-end periods. Targets are stored newest first.
5. Every target member must have exact compatible publication proof with
   `ann_date <= run_as_of_date`. A hole behind a later publication frontier is
   `publication_metadata_unproven` and produces Insufficient Evidence; it is
   never silently removed or replaced by an older period.
6. If no publication frontier exists, target construction returns typed
   Insufficient Evidence rather than an empty complete history.
7. A normal established issuer is complete only with exactly five annual and
   eight reporting targets. The annual sequence and reporting sequence are
   independent; one frontier cannot fabricate the other.

For an authoritative listing exception, first remove `pre_listing` ends from
the two sequences. The exception is supported only when listing age makes five
annual or eight reporting ends impossible at the applicable publication
frontier, every post-listing target has exact publication proof, and every
target family-period passes its unchanged completeness gates. It records the
listing date, provenance, annual and reporting frontiers, reduced target sets,
and reason `authoritative_since_listing`. Pre-listing rows never count, and
provider sparsity never creates a listing exception.

For the deterministic 2026-08-01 established-issuer fixture whose latest
published annual and reporting periods are 2025-12-31 and 2026-03-31:

- annual targets are 2025-12-31, 2024-12-31, 2023-12-31, 2022-12-31, and
  2021-12-31; and
- reporting targets are 2026-03-31, 2025-12-31, 2025-09-30, 2025-06-30,
  2025-03-31, 2024-12-31, 2024-09-30, and 2024-06-30.

The absent 2026-06-30 period is `not_yet_eligible` on 2026-08-01 and appears in
the audit's excluded-period list, not in a missing-period list or completeness
denominator.

### Current-only and strict-PIT indicator evidence

Candidate eligibility is evaluated separately from target eligibility.

For `current_only` analysis, a candidate must have compatible identity,
capability, family, period, company type, scope, currency, and unit; pass its
field completeness gate; and carry `ann_date <= run_as_of_date`. The selected
projection carries:

- `availability_scope = current_only`;
- `availability_basis = ann_date_without_first_publication`;
- the exact `ann_date`, absent `f_ann_date`, provider artifact identity, local
  revision identity, observed/retrieved timestamps, family, and period; and
- `eligibility_policy_version = mainland-financial-period-eligibility-v2`.

A current-only projection may support explicitly labeled present-run financial
analysis and a current-analysis Source Fact contract that accepts that scope.
It remains Degraded Evidence even when every requested family is complete. It
cannot satisfy a contract requesting strict PIT, cannot support language that
claims knowledge at a historical cutoff, and cannot be relabeled by a report,
checkpoint, model, or downstream adapter.

Strict-PIT eligibility additionally requires a valid
`f_ann_date <= historical_as_of_date`, `ann_date <= historical_as_of_date`, and
an exact provider revision observed no later than that cutoff. The selected
revision and first-publication metadata must be bound into Source Fact lineage.
Without `f_ann_date`, strict replay and historical as-of Source Fact projection
return typed `missing_first_publication_for_strict_pit` and Insufficient
Evidence. `ann_date` is never copied, defaulted, or interpreted as
`f_ann_date`.

Reports and decision audits must show the requested analysis scope, each
period's availability scope, the missing metadata reason, the resulting
Evidence Integrity Status, and the policy version. A current-only family may be
model-visible only through its explicitly labeled current-analysis rendering;
it is never included in a strict replay projection.

### Family-level completeness and evidence disposition

The production declared-core threshold remains unchanged. For every company
type, indicator family, and target period:

- all declared critical fields must be present and valid (100%);
- at least 90% of the versioned declared-core fields must be present and valid;
- the declared core is fixed by the existing qualified company-type/family
  declaration and may not be rebuilt from populated fields; and
- all target periods for that family must pass before the family is complete.

Each family receives its own target coverage, candidates, selections,
rejections, conflicts, disposition, and retained artifacts. No aggregate
boolean may replace these family outcomes. Sparse profit, operation, balance,
or DuPont history for a bank does not remove qualified growth or cash-flow
history. Likewise, complete non-bank families remain selected independently.

Evidence disposition is deterministic:

- `Degraded Evidence`: at least one family is complete for the requested
  current analysis; every failed or absent family is optional under the
  applicable Canonical Evidence Contract, Capability Profile, Strategy Rules,
  and proposed material premises; every family used by a premise is complete;
  there is no unresolved critical conflict in a used family-period; and the
  degradation and exact breadth are visible. Missing `f_ann_date` makes an
  otherwise complete current-only result at least Degraded.
- `Insufficient Evidence`: no requested family is complete, any family required
  by the applicable evidence contract or a proposed material premise is
  incomplete, a requested strict-PIT scope has only current-only candidates, or
  publication target construction is overdue/unproven. The system produces a
  non-directional Analysis Outcome when the missing requirement is material;
  `Hold` is not a substitute.
- `Conflicted Evidence`: eligible candidates disagree on a critical normalized
  value without qualified supersession. The status attaches only to that
  family-period and facts derived from it. Other complete family-periods remain
  selected and visible, though the Decision Gate still blocks any material
  premise that needs the conflicted facts.

Partial family breadth alone does not authorize a Trading Decision. Evidence
Admission, Decision Assertion coverage, Strategy Rules, and the Decision Gate
remain unchanged and determine whether the surviving complete families are
sufficient for a proposed thesis.

### Duplicate revisions, overlaps, and deterministic selection

Every decoded provider response is retained before evaluation. Every local
revision observation has a manifest entry even when identical bytes share one
content-addressed payload blob. Rejected, superseded, equivalent, conflicted,
and selected candidates remain immutable and addressable.

Selection considers only artifacts acquired within the configured foreground
work. It must not call a lower-priority provider merely to seek confirmation or
duplicate data. For each exact company type, family, period, scope, currency,
and unit key:

1. Reject candidates that fail publication cutoff, requested scope, identity,
   metadata, or field-completeness requirements.
2. Apply a provider's qualified stable filing/revision sequence when one exists.
   For strict PIT, only sequence members whose first-publication and local
   observation time are within the cutoff participate.
3. When authoritative provider chronology is absent, order same-provider local
   revisions by eligible `ann_date` descending, then `observed_at` descending,
   then `retrieved_at` descending, then local provider revision identity in
   ascending lexical order. This is deterministic operational selection, not a
   claim about the provider's restatement chronology.
4. Compare eligible provider winners in immutable route-rank order. The first
   route candidate that passes the requested contract is the operational
   selection. Later-provider candidates returned incidentally by a bulk or gap
   request do not replace it merely because they were observed later.
5. Two compatible candidates are `equivalent_unselected` only when all shared
   normalized critical and declared-core values agree and their identity
   dimensions are compatible. Retain the later-route or lower-ranked candidate
   unselected; duplicates do not increase coverage.
6. If eligible candidates disagree on a critical normalized value and no
   qualified provider revision sequence establishes supersession, record the
   deterministic operational winner for reproducibility but mark that exact
   family-period Conflicted and bar its Source Facts. Do not erase or conflict
   sibling families or other periods.
7. Non-critical overlap differences are retained and reported; they do not
   automatically conflict the family, are never field-merged across candidates,
   and remain subject to downstream material-premise validation.

`update_flag` is retained as provider metadata but contributes neither a sort
position nor a supersession claim unless accompanied by a separately qualified
stable provider filing revision identity. Local revision identity is a stable
tie-breaker and provenance binding, not authoritative restatement chronology.

### Contracts, compatibility, and rollout

The routing plan signature must commit to the eligibility policy, mainland
deadline table, declared-core/completeness policy, candidate-selection contract,
manifest contract, checkpoint projection, audit projection, report projection,
and shadow/authority disposition. Completeness or selection input that carries
a different policy identity fails closed rather than being reinterpreted.

Compatibility rules are:

- `legacy` remains the default and authoritative financial path. It retains its
  current contract identities and does not need synthetic v2 fields.
- `qualified_v1_shadow` may execute the corrected v2 composition only as an
  explicit validation workload. Its disposition remains
  `shadow_non_authoritative`; legacy-selected output remains authoritative.
- authoritative `qualified_v1` is disabled in configuration/preflight while
  this rollout gate is closed. Shadow completion never activates it
  automatically.
- a qualified or qualified-shadow checkpoint using
  `financial-period-selection-v1`, lacking the v2 eligibility identity, or
  carrying another target/deadline/selection identity cannot resume under v2.
  Resume returns typed `financial_eligibility_policy_mismatch` before provider
  and model calls. The caller may explicitly start a new run with a new run ID;
  the old checkpoint is not upgraded.
- legacy checkpoints continue through existing legacy compatibility rules.
- old and new reports, decision audits, checkpoints, manifests, artifacts, and
  provider rows remain readable under their recorded versions. None is
  rewritten, deleted, backfilled, or relabeled.
- rollback affects only the plan of a new run and performs no data migration.

The authority gate remains closed until a corrected v2 shadow validation set
including the diagnosed non-bank 600895 and bank 601328 cases reaches a
completed Run Lifecycle Status and publishes both a complete report and a
decision audit for each run. Those outputs must show v2 targets, current-only
degradation, family-level outcomes, duplicate/conflict handling, and
`shadow_non_authoritative`. Parent AC1-AC13 must also pass unchanged. Satisfying
the gate permits a separate reviewed activation decision; it does not itself
enable `qualified_v1`.

### Highest implementation and testing seam

Use the existing run-scoped financial dispatcher and compiled financial tool
node as the single target-construction, family-selection, and manifest seam.
They already own immutable routing plans, per-family candidates, provider
attempt correlation, checkpoints, audit projection, and model-facing rendering.
The corrected policy must replace the caller-supplied calendar-derived eligible
period list at this seam rather than introduce a second financial router.

Keep provider adapters as the source-boundary owners of filing metadata and
normalized values. Keep the Provider Request Coordinator as the only physical
request seam. Keep Evidence Admission as the only route from selected evidence
to Source Facts, and keep the existing report and decision-audit projections as
the user-visible verification boundaries.

## Testing Decisions

### Test philosophy and seams

- Test observable behavior first at the financial dispatcher/compiled-tool seam
  with fixed clocks and retained provider fixtures. Assert target sets, family
  dispositions, selected revision identities, typed exclusions, and manifests,
  not private helper order.
- Test strict/current Source Fact projection at Evidence Admission, checkpoint
  compatibility at the qualified resume boundary, and shadow authority through
  the deterministic CLI/report/decision-audit seam.
- Reuse the existing financial period contract, capability routing selection,
  indicator adapter, manifest/reporting, checkpoint rollout, evidence gate, and
  parent acceptance fixtures. No acceptance test calls a live provider, uses a
  credential, reads a live qualification cache, or invokes a paid model.
- Every fixture carries fixed Instrument Identity, listing provenance, company
  type, scope, currency, filing dates, observed/retrieved timestamps, payload
  digests, and local revision identities. Randomized row order must not change
  canonical results.

### Acceptance criteria

| ID | Deterministic scenario | Required result |
| --- | --- | --- |
| PE1 | On 2026-08-01, compatible histories end at 2026-03-31 and contain no 2026-06-30 row. | 2026-06-30 is `not_yet_eligible`, excluded from targets and all family completeness denominators, and never labeled missing or incomplete. |
| PE2 | A 2026-06-30 candidate has `ann_date=2026-08-15`; run the same fixture as of 2026-08-14 and 2026-08-15. | The quarter is excluded before announcement and becomes `published_eligible` on the announcement date, advancing the reporting frontier exactly once. |
| PE3 | Eight complete indicator periods have `ann_date <= as_of` and no `f_ann_date`; current-only scope is requested. | Periods may be selected with `availability_scope=current_only`; family completeness is preserved; evidence is visibly Degraded and carries the v2 policy identity. |
| PE4 | Reuse PE3 under strict historical as-of scope. | Every no-`f_ann_date` candidate is rejected with `missing_first_publication_for_strict_pit`; no historical Source Fact or strict replay projection is produced; required evidence is Insufficient. |
| PE5 | An established issuer has publication anchors through annual 2025-12-31 and reporting 2026-03-31, plus older complete rows and an unpublished 2026-06-30. | Targets are exactly the five annual and eight reporting dates enumerated in this spec, newest first; 2026-06-30 is excluded. |
| PE6 | Authoritative listing age makes five/eight impossible and every published post-listing period is complete; then remove one post-listing publication. | The first case records an explicit supported since-listing exception; the second is Insufficient. Pre-listing rows never count. |
| PE7 | A 601328 bank fixture has complete growth and cash-flow periods and genuinely sparse sibling families. | Growth and cash-flow remain selected and complete independently; sparse families are rejected. Aggregate status is Degraded only when those families are optional, and becomes Insufficient when a material premise requires one. |
| PE8 | A 600895 non-bank fixture has all diagnosed indicator families complete for the v2 published targets. | Every family remains independently complete and selected; overall evidence is Degraded only because indicator lineage is current-only, not because of target breadth. |
| PE9 | Same-provider revisions and a later-provider overlap have compatible identity and equivalent normalized critical/core values. | Every artifact and revision observation is retained; one deterministic route/revision winner is selected; equivalent candidates are `equivalent_unselected`; duplicates do not inflate coverage. |
| PE10 | Eligible revisions disagree on one critical value in one family-period without qualified supersession. | Only that family-period is Conflicted and barred from Source Facts; its operational winner is deterministic; sibling families and periods remain selected; all artifacts remain retained. |
| PE11 | Two revisions differ only in `update_flag`, with fixed filing/observation metadata and shuffled row order. | Selection is identical across runs; `update_flag` creates no chronology or supersession claim; local revision identity is only the final deterministic tie-breaker. |
| PE12 | Resume a pre-v2 qualified checkpoint under the v2 corrected shadow plan. | Resume fails before provider/model work with `financial_eligibility_policy_mismatch`; the checkpoint/report is unchanged; an explicitly started new run receives a new ID and v2 contracts. |
| PE13 | Run the corrected composition in `qualified_v1_shadow`. | Rollout disposition, report, and decision audit all say `shadow_non_authoritative`; legacy-selected output remains authoritative; no configuration path enables `qualified_v1`. |
| PE14 | Render and checkpoint a v2 run containing eligible, not-yet-eligible, current-only, sparse, equivalent, and conflicted entries. | Routing plan, completeness, selection, checkpoint, audit, and report carry the same v2 identity and restore/render deterministically without payload bodies, secrets, or raw errors. |
| PE15 | Run parent AC1-AC13 without modifying their fixtures or expected results. | Every criterion passes unchanged and without waiver; any failure blocks the authority gate. |

Parent AC1-AC13 retain their existing authoritative definitions: AC1-AC2 current
mainland suspension/tradeability, AC3 exact snapshot pins, AC4-AC5 OPIT and
retrospective reconstruction, AC6 atomic artifact publication/GC, AC7-AC8
Crypto Instrument registry behavior, AC9-AC10 exact retries and cooldown, AC11
legacy readability/migration, AC12 clean-tree runtime state, and AC13 full
regression/lint/diff scope. This specification does not amend their inputs or
expected outcomes.

### Verification scope

Focused verification covers the existing financial period contracts,
capability routing selection, Tushare indicators, BaoStock ratio families,
financial manifest/reporting, checkpoint resume/rollout, Evidence Admission,
decision audit, and reporting suites. Joint verification then runs the parent
AC1-AC13 commands, the non-integration suite, the full supported suite, Ruff,
and whitespace/diff validation. Tests use fake coordinated transports and fixed
artifacts only.

## Out of Scope

- Lowering the 100% critical-field or at least 90% declared-core thresholds.
- Fabricating, forward-filling, or otherwise creating 2026-06-30 data.
- Treating, copying, defaulting, or describing `ann_date` as `f_ann_date`.
- Repeating live-provider qualification or making live provider calls while
  producing or accepting this specification.
- Making Tushare globally first or changing the daily mainland Current Analysis
  Provider Chain.
- Changing BaoStock authority for suspension, trading status, dated ST,
  listing, delisting, or the initial strict Provider History Bundle.
- Changing Strategy Rules, Evidence Admission requirements, the Decision Gate,
  ratings, position sizing, prompts, or predictive logic.
- Rewriting, deleting, migrating in place, or relabeling reports, checkpoints,
  manifests, immutable artifacts, provider rows, payloads, snapshots, or pins.
- Adding cross-provider market-row blending, provider fan-out, speculative
  prewarming, or foreground acquisition solely for strict replay.
- LLM-provider, model, structured-output, or model-retry redesign.
- Production-code modification as part of this specification task.

## Further Notes

- This is a focused corrective specification for the parent capability-specific
  mainland routing work. Where the parent `financial-period-selection-v1`
  calendar-derived target behavior conflicts with this document, v2 governs
  new qualified shadow runs; legacy and historical v1 records remain unchanged.
- The policy is consistent with the accepted fail-closed evidence, source-bound
  Source Fact, strict versioned contract, typed acquisition outcome, coverage,
  report rendering, OPIT/backfill, current-analysis degradation, mainland
  compatibility, BaoStock strict-history, and foreground-budget ADRs.
- Current-only is an evidence availability scope, not a predictive-confidence
  label. Degraded Evidence may remain Decision-Ready only when every material
  premise and Decision Assertion is otherwise supported under unchanged rules.
- The diagnosed shadow runs are evidence for this spec, not proof that
  authoritative rollout is safe. The corrected v2 shadow report and decision
  audit remain mandatory before any separate activation review.

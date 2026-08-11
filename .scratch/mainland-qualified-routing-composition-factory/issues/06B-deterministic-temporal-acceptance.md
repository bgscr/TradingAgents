# 06B — Run deterministic temporal acceptance

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Prove the temporal amendment end to end at the deterministic
fresh/retained acquisition, context, request, checkpoint, and replay seams. Run
the exact current-analysis and strict-PIT boundary matrices, replay every
accepted and rejected result with zero I/O, prove 32 followers share one
temporal transaction, then rerun the complete Ticket 06 QCF1–QCF18, PE1–PE15,
AC1–AC13, non-integration/full suites, Ruff, history/security/diff checks, and
two-stage review twice. No live provider or paid model participates.

**Blocked by:** 02H — Integrate temporal proof projection and acquisition; 05C
— Add temporal checkpoint and replay compatibility; 06A — Correct QCF4
deterministic acceptance; 03 — Add static registration and caller precedence;
02D — Add executable production-source wiring; 04A — Integrate managed
eligibility-context acquisition; 05B — Persist and restore indicator metadata
compatibility.

**Status:** ready-for-agent

## Observable behavior

- Fixed clocks, fake Market Session Calendar revisions, fake provider
  transports/SDKs, disposable stores, retained immutable artifacts, and fake
  models exercise fresh and retained Tushare/AKShare-Sina/BaoStock metadata
  through the one temporal evaluator and exact checkpoint/replay boundary.
- Current-analysis acceptance follows the latest-completed-session frontier,
  including the real next-day `00:48:07 Asia/Shanghai` case, weekends, holidays,
  intraday next-session retrieval, and exact next-session-close transition.
- Strict PIT remains independently fail-closed unless first-publication and
  direct pre-cutoff or exact-revision availability proof pass.
- Every accepted and rejected decision restores exactly with zero provider,
  network, model, coordinator-permit, attempt, evaluator, restore-clock, or
  restore-time calendar-frontier work.
- Thirty-two concurrent first requests share one fixed
  `eligibility_evaluated_at`, one acquisition leader, one context-level temporal
  evaluation transaction, and one immutable terminal decision/context.
- After the focused matrix passes, complete Ticket 06 acceptance is rerun under
  plan 1.3/context v2 without weakening or renumbering any prior QCF, PE, or AC.

## Acceptance criteria

- [ ] Requested-session day after close and before evaluation: current accepts
  with complete common proof; strict rejects post-cutoff observation/retrieval.
- [ ] The exact `2026-08-11 00:48:07 Asia/Shanghai` retrieval for requested
  session `2026-08-10` is current-accepted while `2026-08-10` remains latest
  completed; strict rejects unless its independent exact-cutoff predicate passes.
- [ ] Immediately before the next open session and during that session before
  close, current accepts the prior session; session open does not advance the
  frontier.
- [ ] At and immediately after the next session close, a new prior-session
  decision rejects in both modes because the later session is latest completed.
- [ ] Friday-to-Saturday and Friday-to-Sunday cases current-accept while Friday
  remains latest completed; a mainland holiday interval preserves the preceding
  open-session frontier until the next open session completes.
- [ ] Retained evidence reused after one or more later completed sessions rejects
  for a new run; exact replay of its original decision remains unchanged.
- [ ] `ann_date > requested_session` rejects both modes. Eligible `ann_date`
  with missing/later `f_ann_date` may pass current but rejects strict.
- [ ] Direct strict pre-cutoff control produces current-rejected/strict-accepted
  when `f_ann_date` is eligible and both immutable observation/retrieval
  timestamps are no later than `strict_cutoff`.
- [ ] Qualified exact-revision control accepts strict post-cutoff retrieval only
  when `exact_revision_authoritative_available_at <= strict_cutoff` binds the
  exact revision; generic filing availability and mismatched proof reject.
- [ ] Missing provider-derived `evidence_effective_as_of` or provider
  `ann_date` rejects both modes with no request/session/retrieval/route/name/
  ticker/factory/model backfill.
- [ ] Every accepted and rejected row replays its exact decision identity,
  inputs, result, eligible session, and sorted reasons with zero I/O and no
  restore-time calendar/evaluator access.
- [ ] Thirty-two concurrent followers produce evaluator count one, clock sample
  count one, one acquisition leader, and one terminal temporal decision/context;
  all physical attempt and cache reuse counts remain exact.
- [ ] After the focused temporal matrix passes, rerun complete Ticket 06
  QCF1–QCF18, PE1–PE15, and AC1–AC13 without changed IDs, weakened expectations,
  fixture waivers, or acceptance shortcuts.

## Exact contract and version requirements

- Exercise Capability Routing Plan `1.3`,
  `provider-neutral-temporal-eligibility-v1`,
  `market-session-eligibility-binding-v1`,
  `financial-metadata-temporal-eligibility-decision-v1`, and Financial
  Eligibility Acquisition Context v2.
- Exercise company-type evidence binding v3/projection v2, eligibility metadata
  binding v2, listing metadata/projection v2, company-type routing
  request/response v2, eligibility request binding/result v2, and statement/
  indicator routing requests v4/v7.
- Exercise cache checkpoint `1.2`, dispatch ledger `1.6`, statement routing
  result v2, indicator routing result v4, manifest v4, statement selection
  dispatch/checkpoint v2, and indicator selection dispatch/checkpoint v3.
- Exercise temporal operational projections: completeness row v5, aggregate v4,
  manifest/request audit v4, Financial Dispatch Audit Projection 4.0, tool
  manifest v4, and tool message envelope v4.
- Preserve plan-1.2 semantic compatibility, raw v1 artifact/candidate identities,
  financial-period eligibility v2, unchanged Canonical Evidence/Source Fact/
  Decision contracts, and the `mainland-routing-plan:v1:<sha256>` namespace.

## Migration and compatibility behavior

- All matrices use disposable new run/checkpoint namespaces. Historical reports,
  checkpoints, artifacts, attempts, manifests, audits, snapshots, pins, provider
  rows, decisions, and IDs—including the failed Ticket 07 run—remain immutable.
- Plan-1.2/context-v1 under plan 1.3 fails typed or requires explicit
  `START_NEW`; no current calendar lookup, live repair, in-place migration,
  relabeling, or reinterpretation is allowed.
- The previous Ticket 06 pass remains valid historical evidence under its
  pre-temporal contract, but it does not satisfy amended acceptance. Ticket 06
  must complete a new full rerun after this ticket passes.
- Passing 06B and the Ticket 06 rerun provides deterministic evidence only. It
  neither authorizes Ticket 07 live work nor enables/recommends `qualified_v1`.

## Explicit non-goals

- Live Tushare, AKShare, Yahoo, or BaoStock calls; paid models; credentials;
  integration tests; qualification caches; or the Ticket 07 rollout retry.
- Copying/importing temporal prototype implementation, enabling
  `qualified_v1`, mutating the failed run, or treating deterministic acceptance
  as live authorization.
- Local-date equality/ordering, grace windows, next-session-open cutoff, simple
  comparison substitution, provider-proof backfill, or duplicated evaluator.
- Route/order/fallback, completeness, financial-period eligibility, Evidence
  Admission, Decision Gate, BaoStock authority, daily routing, or shadow
  authority changes.

## Deterministic regressions

- Run the complete focused matrix above for fresh and retained company-type and
  applicable listing/lifecycle evidence in both temporal modes.
- Mutate each calendar/proof/decision/context/checkpoint material binding and
  assert the exact closed reason or typed compatibility failure.
- Replay every case after advancing the fake restore clock/calendar and install
  sentinels proving zero evaluator, provider/model, coordinator, permit, attempt,
  or current-frontier activity.
- Stress 32 simultaneous first requests at midnight and next-session-close
  boundaries and reconcile evaluator, clock, provider-attempt, single-flight,
  cache, and terminal-result counts.
- Rerun all focused suites from Tickets 01–05, 02A–02H, 04A, 05A–05C, 06A,
  then complete QCF1–QCF18, PE1–PE15, AC1–AC13, non-integration, and the full
  supported suite twice from the identical tree.
- Perform secret/raw-error, historical-byte, diff/whitespace, authority, and
  dependency-graph checks; then Stage 1 Spec Compliance review before Stage 2
  Code Quality review. Classify findings as Critical, Important, or Minor.

## Verification commands

Run focused temporal acceptance first:

```powershell
$env:RUN_LIVE_DEEPSEEK_TESTS='0'
$env:RUN_LIVE_TUSHARE_TESTS='0'
$env:RUN_LIVE_PROVIDER_TESTS='0'
pytest -q tests/test_financial_metadata_temporal_eligibility.py tests/test_financial_temporal_proof_projection.py tests/test_financial_temporal_checkpoint_compatibility.py
pytest -q tests/test_qualified_routing_composition_acceptance.py -k "temporal or QCF or qcf"
pytest -q tests/test_financial_eligibility_metadata_acquisition.py tests/test_qualified_routing_eligibility_context_lifecycle.py tests/test_qualified_routing_checkpoint_context_restore.py
```

Then run the complete amended Ticket 06 and repository assurance twice:

```powershell
pytest -q tests/test_qualified_routing_composition_acceptance.py
pytest -q tests/test_financial_period_eligibility.py tests/test_corrected_shadow_joint_acceptance.py
pytest -q -m "not integration"
1..2 | ForEach-Object { pytest -q; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
ruff check .
git diff --check
git status --short
```

No command may select integration tests, contact a live provider/model, or
permit environment credentials to opt into live work.

## Definition of done

- [ ] Every exact current-analysis, strict-PIT, weekend/holiday/frontier,
  proof-failure/control, replay, and 32-follower case passes at the highest
  deterministic seam.
- [ ] QCF1–QCF18, PE1–PE15, and AC1–AC13 pass unchanged after the temporal
  matrix, followed by non-integration and two identical full-suite passes,
  Ruff, history/security/diff checks, and no live work.
- [ ] Stage 1 Spec Compliance then Stage 2 Code Quality review passes with no
  unresolved Critical or Important finding and a durable traceability record.
- [ ] Ticket 06 remains blocked until its complete post-06B rerun is recorded;
  Ticket 07 remains blocked, the failed run is immutable, and authoritative
  `qualified_v1` remains disabled.

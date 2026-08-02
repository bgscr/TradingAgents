# 07 — Run corrected qualified shadow joint acceptance

**Parent specification:** [Mainland financial reporting-period eligibility and current-only indicators](../spec.md)

**What to build:** Prove the complete corrected v2 tracer bullet through the
real deterministic CLI/compiled-graph boundaries for the diagnosed 601328 bank
and 600895 non-bank cases. The acceptance workload must publish complete reports
and decision audits that remain shadow/non-authoritative, satisfy PE1-PE15, and
leave parent AC1-AC13 unchanged. This is evidence for a later activation review,
not authority activation.

**Blocked by:** 01 — Add the v2 mainland financial eligibility-policy contract;
02 — Construct publication-aware annual and reporting targets; 03 — Enforce
current-only indicator and strict-PIT boundaries; 04 — Preserve independent
family completeness and evidence disposition; 05 — Select duplicate revisions
and overlaps deterministically; 06 — Version persistence, reporting, and
checkpoint compatibility.

**Status:** ready-for-agent

## Observable behavior

- A fixed-clock, fake-transport, fake-model qualified shadow matrix runs through
  authoritative Instrument Identity, v2 publication/target construction,
  current-only/strict boundaries, independent families, revision selection,
  immutable artifacts, checkpoint round-trip, report, decision audit, Evidence
  Admission, Decision Gate, terminal output, and rollback.
- The 601328 bank case preserves complete growth and cash-flow families while
  exposing genuinely sparse siblings. The 600895 non-bank case preserves every
  diagnosed complete family. Neither requires or fabricates 2026-06-30.
- Every completed acceptance run publishes a complete report and decision audit
  showing the v2 identity, exact targets/exclusions, current-only degradation,
  family outcomes, duplicate/conflict outcomes, and
  `shadow_non_authoritative`.
- Strict replay fixtures reject missing `f_ann_date`; current-only evidence does
  not silently enter historical as-of Source Facts.
- Pre-v2 checkpoint mismatch is typed and mutation-free; legacy remains
  authoritative and readable; authoritative `qualified_v1` remains disabled.
- Parent AC1-AC13, the full supported suite twice, Ruff, and whitespace/diff
  validation pass without waiver.

## Acceptance criteria

- [ ] **PE1-PE14:** Every parent-spec criterion has a named deterministic test at
  the highest relevant public seam and passes with exact expected outputs.
- [ ] **PE15:** Parent AC1-AC13 pass unchanged, including suspension terminal
  behavior, exact snapshot pins, OPIT/retrospective reconstruction, artifact
  publication/GC, Crypto Instrument registry behavior, exact retry/cooldown
  accounting, legacy migration/readability, clean-tree behavior, and full
  regression scope.
- [ ] The 601328 and 600895 fixture runs each complete and publish a report and
  decision audit; both visibly remain `shadow_non_authoritative`.
- [ ] Full test suite passes twice from the same code state; Ruff and
  `git diff --check` pass.
- [ ] No acceptance scenario calls a live provider, reads live qualification
  caches, requires a credential, uses a paid model, or performs provider
  qualification.
- [ ] No report/checkpoint/manifest/artifact/provider row/snapshot/pin is
  rewritten or deleted; runtime outputs follow existing ignore rules.
- [ ] Authoritative `qualified_v1` remains disabled after all tests pass; the
  result is only an evidence package for a separate reviewed decision.

## Acceptance IDs

- **PE1-PE15** are jointly owned as the final end-to-end gate.
- **Parent AC1-AC13** retain their existing authoritative definitions and
  expected results without amendments or waivers.

## Deterministic regressions

- 2026-08-01 unpublished-quarter exclusion and announcement-date admission.
- Exact five-annual/eight-reporting construction and authoritative listing
  exception.
- Current-only positive and strict-PIT rejection matrices.
- 601328 bank sparse/qualified family mixture and 600895 complete non-bank
  families.
- Equivalent revisions, `update_flag` negative chronology, critical conflict,
  shuffled-order, and exact-attempt-cardinality fixtures.
- Pre-v2 checkpoint mismatch, v2 round-trip, legacy readability, report/audit
  secret-safety, shadow authority, and rollback fixtures.
- Parent AC1-AC13 fixtures unchanged.
- Run the full suite twice from the identical tree to prove reproducibility.

## Checkpoint and migration compatibility

- Acceptance uses new disposable v2 run/checkpoint state and immutable fixed
  artifacts. It does not mutate the diagnosed shadow artifacts or any historical
  record.
- Pre-v2 qualified resume must fail with
  `financial_eligibility_policy_mismatch`; an explicit new run uses a distinct
  ID. Legacy readers and rollback remain unchanged.
- Validate byte/reference preservation for representative legacy reports,
  checkpoints, manifests, artifacts, provider rows, snapshots, pins, and
  market-history payloads.
- Generated acceptance state must be ignored/local according to existing rules;
  source/configuration negative controls remain visible in Git status.

## Explicit non-goals

- Live provider qualification, live Tushare/BaoStock/AKShare/Yahoo calls,
  credentials, paid models, or reinterpretation of diagnosis artifacts.
- Authoritative `qualified_v1` activation or a recommendation to activate it
  automatically after passing.
- Threshold reduction, fabricated 2026-06-30 data, or `ann_date`/`f_ann_date`
  substitution.
- Global Tushare-first routing, BaoStock status-authority changes, mainland daily
  provider reordering, Strategy Rule changes, Evidence Admission changes, or
  Decision Gate changes.
- Unrelated refactoring, dependency upgrades, provider fan-out, hidden retries,
  speculative prewarming, or historical-state rewrites/deletions.

## Verification commands

Run focused financial v2 verification:

```powershell
pytest -q tests/test_financial_period_contracts.py tests/test_financial_capability_routing_selection.py tests/test_tushare_indicator_adapter.py tests/test_baostock_ratio_adapters.py
pytest -q tests/test_financial_manifest_evidence_reporting.py tests/test_checkpoint_resume.py tests/test_qualified_routing_rollout.py tests/test_decision_audit.py tests/test_reporting.py tests/test_admitted_evidence_binding.py tests/test_recorded_evidence_replay.py tests/test_evidence_gates.py
```

Run unchanged parent market/evidence regressions:

```powershell
pytest -q tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py tests/test_market_snapshot.py tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_market_history_shadow.py tests/test_current_tradeability.py
pytest -q -m "not integration"
```

Run the complete suite twice and final static/scope checks:

```powershell
1..2 | ForEach-Object { pytest -q; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
ruff check .
git diff --check
git status --short
```

## Definition of done

- [ ] A traceability record maps PE1-PE15 and parent AC1-AC13 to named tests and
  passing outcomes.
- [ ] Both diagnosis-derived instrument fixtures complete with reports and
  decision audits that show v2 semantics and shadow non-authority.
- [ ] Focused suites, non-integration suite, full suite twice, Ruff, whitespace,
  secret/payload, and scope checks all pass.
- [ ] **Stage 1 — Spec Compliance Review:** confirms exact eligibility rules,
  current-only/strict boundary, unchanged thresholds, family independence,
  revision selection, compatibility, shadow authority, acceptance IDs, and all
  non-goals. Any functional gap is resolved before Stage 2.
- [ ] **Stage 2 — Code Quality Review:** confirms boundary separation,
  deterministic tests, typed/sanitized failures, immutable retention, exact
  attempts, maintainability, and no unrelated changes. All Critical and
  Important findings are resolved.
- [ ] No historical state was rewritten/deleted, no live qualification occurred,
  and authoritative `qualified_v1` remains disabled.

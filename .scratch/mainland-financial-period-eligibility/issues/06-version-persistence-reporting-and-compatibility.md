# 06 — Version persistence, reporting, and checkpoint compatibility

**Parent specification:** [Mainland financial reporting-period eligibility and current-only indicators](../spec.md)

**What to build:** Make one corrected v2 qualified shadow run persist and render
the same eligibility policy, targets, family outcomes, availability scope,
revision selections, and authority disposition across routing plan, checkpoint,
manifest, audit, and report. A pre-v2 qualified checkpoint must fail typed
without mutation, legacy history must remain readable, and no path may enable
authoritative `qualified_v1`.

**Blocked by:** 01 — Add the v2 mainland financial eligibility-policy contract;
02 — Construct publication-aware annual and reporting targets; 03 — Enforce
current-only indicator and strict-PIT boundaries; 04 — Preserve independent
family completeness and evidence disposition; 05 — Select duplicate revisions
and overlaps deterministically.

**Status:** ready-for-agent

## Observable behavior

- Routing-plan projection, completeness policy, selection contract, checkpoint,
  manifest, audit, tool/report projection, and rendered report all carry the
  exact same `mainland-financial-period-eligibility-v2` identity.
- A checkpoint round-trip restores eligible/excluded periods, frontiers,
  five/eight targets, family dispositions, current-only/strict scope, artifact
  and revision bindings, overlaps/conflicts, physical attempts, rendering
  identity, and `shadow_non_authoritative` without a provider call.
- Resuming a qualified/qualified-shadow checkpoint that is pre-v2, lacks the
  policy, or has a target/deadline/selection mismatch returns typed
  `financial_eligibility_policy_mismatch` before provider/model work. It does not
  upgrade or rewrite the checkpoint.
- A caller may explicitly start a distinct new v2 run with a new run ID after
  mismatch. No state is copied into the old run.
- Legacy checkpoints/reports remain readable through existing legacy rules.
  Old and new records retain their recorded meaning and are never relabeled.
- `qualified_v1_shadow` remains `shadow_non_authoritative`; legacy-selected
  output remains authoritative. Authoritative `qualified_v1` is disabled and
  shadow completion cannot activate it.
- Reports and decision audits visibly distinguish not-yet-eligible, current-only,
  sparse, equivalent, selected, rejected, and conflicted entries without
  payload bodies, credentials, token digests, or raw exceptions.

## Acceptance criteria

- [ ] **PE12:** A pre-v2 qualified checkpoint fails typed before all external
  work, remains byte-for-byte unchanged, and an explicit new run has a distinct
  ID and v2 plan.
- [ ] **PE13:** Corrected qualified shadow output is non-authoritative in rollout
  projection, report, decision audit, and terminal state; no configuration path
  enables authoritative `qualified_v1`.
- [ ] **PE14:** A mixed v2 fixture round-trips and renders identically across all
  named persistence/reporting contracts with one policy identity.
- [ ] Mismatch detection covers missing policy, old period-selection contract,
  changed deadline table, changed selection contract, tampered signature, and
  shadow/authority mode mismatch.
- [ ] Checkpoint restoration performs zero provider/model calls and preserves
  physical attempt cardinality and immutable artifact references.
- [ ] Legacy fixture readability remains unchanged, including existing reports,
  checkpoints, manifests, provider rows, snapshots, and pins.
- [ ] Rollback changes only a new run's plan; it deletes, rewrites, or migrates no
  historical state.

## Acceptance IDs

- **PE12, PE13, and PE14** are owned in full by this ticket.
- **PE1-PE11:** their finalized outputs must survive the persistence/reporting
  projections unchanged; this ticket does not redefine their behavior.

## Deterministic regressions

- Full v2 checkpoint save/restore with no network/model work.
- Mismatch matrix for every committed policy/contract identity and tampered
  signature.
- Explicit new-run-after-mismatch identity and no-rewrite assertions.
- Mixed report/audit fixture containing every required publication,
  availability, family, revision, overlap, and conflict disposition.
- Secret/raw-payload/raw-error absence scans for checkpoint, manifest, audit,
  report, CLI, and terminal projections.
- Legacy qualified/legacy readable fixtures and rollback byte-preservation
  controls.
- Shadow versus authoritative mode negative tests at configuration, preflight,
  checkpoint, execution, report, and decision-audit boundaries.

## Checkpoint and migration compatibility

- Compatibility is additive and version-selective. There is no in-place schema
  reinterpretation from `financial-period-selection-v1` to v2.
- `financial_eligibility_policy_mismatch` is a safe typed outcome before provider
  or model work. Automatic mutation, backfill, and partial resume are forbidden.
- Historical reports, checkpoints, manifests, artifacts, provider rows,
  snapshots, pins, payloads, and market-history databases remain readable and
  unchanged.
- Identical artifacts may be referenced by a new run, but a new run cannot
  rewrite the previous run's manifest or authority disposition.
- Default/rollback mode remains legacy; authoritative activation requires a
  separate future decision after ticket 07 and is not implemented here.

## Explicit non-goals

- Enabling authoritative `qualified_v1` or treating shadow completion as
  activation.
- Rewriting, upgrading in place, deleting, or relabeling any historical record.
- Rerunning live provider qualification, changing provider authority/order, or
  adding foreground calls.
- Lowering thresholds, fabricating 2026-06-30, treating `ann_date` as
  `f_ann_date`, or changing Strategy Rules, Evidence Admission, or Decision Gate.
- General checkpoint/report framework refactoring beyond the smallest v2
  projections and typed compatibility path.

## Verification commands

```powershell
pytest -q tests/test_checkpoint_resume.py tests/test_qualified_routing_rollout.py
pytest -q tests/test_financial_manifest_evidence_reporting.py tests/test_decision_audit.py tests/test_reporting.py
pytest -q tests/test_recorded_evidence_replay.py tests/test_admitted_evidence_binding.py
pytest -q -m "not integration"
ruff check .
git diff --check
git status --short
```

## Definition of done

- [ ] PE12-PE14 pass through real checkpoint, CLI/report, and decision-audit
  boundaries with fixed artifacts.
- [ ] Every v2 projection agrees on identity and semantics without recomputation
  or external calls.
- [ ] Legacy history is readable, incompatible resume fails typed, and no record
  is rewritten.
- [ ] Authoritative qualified routing remains disabled and shadow output remains
  visibly non-authoritative everywhere.
- [ ] Spec Compliance and Code Quality reviews have no unresolved Critical or
  Important findings.

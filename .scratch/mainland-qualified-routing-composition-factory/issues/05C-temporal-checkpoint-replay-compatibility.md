# 05C — Add temporal checkpoint and replay compatibility

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Persist the exact plan-1.3/context-v2 temporal eligibility
inputs and decisions through the existing Provider Subrequest Cache checkpoint,
financial dispatch ledger, manifests, and safe operational projections. Exact
restore reproduces accepted and rejected decisions and their identities with
zero provider/model I/O and without consulting the restore-time clock or today's
calendar frontier; incompatible old state fails typed or requires explicit
`START_NEW` and is never rewritten.

**Blocked by:** 02G — Add temporal eligibility contracts and evaluator; 02H —
Integrate temporal proof projection and acquisition; 05A — Bind and restore
eligibility context through checkpoints; 05B — Persist and restore indicator
metadata compatibility.

**Status:** ready-for-agent

## Observable behavior

- A fresh plan-1.3 qualified shadow run persists context v2, cache checkpoint
  1.2, dispatch ledger 1.6, manifest v4, and downstream projections that all
  bind the same temporal policy/calendar identities, requested session/mode,
  fixed evaluation instant, proof, decision results/reasons, and exact IDs.
- Both accepted and rejected company-type and listing/lifecycle decisions retain
  their provider-derived effective/publication proof, immutable observation and
  retrieval timestamps, and exact artifact/revision binding.
- Strict-mode state additionally retains `strict_cutoff`, `f_ann_date`, and any
  qualified `exact_revision_authoritative_available_at`; current mode retains
  the pinned latest-completed-session decision and exact frontier inputs.
- Closing and restoring an exact checkpoint reproduces context, cache,
  dispatcher, attempts, candidates, temporal decisions, semantic compatibility,
  manifests, safe audit/tool projections, and IDs exactly with zero provider,
  network, coordinator-permit, model, or new-attempt work.
- Restore uses only recorded deterministic inputs/result. It does not sample a
  new evaluation instant, query `latest_completed_session`, call today's
  calendar frontier, invoke the temporal evaluator against new state, reacquire
  metadata, or repair a checkpoint.

## Acceptance criteria

- [ ] Context v2 and checkpoint state bind canonical run/asset/Instrument
  Identity, plan 1.3, requested session/mode, fixed
  `eligibility_evaluated_at`, pinned calendar revision/binding, requested and
  next-session close facts, and derived `latest_completed_session`.
- [ ] Each prerequisite artifact binds `evidence_effective_as_of` plus its
  permitted derivation identifier, `ann_date`, applicable `f_ann_date`,
  immutable `observed_at`/`retrieved_at`, artifact/row/revision identity, and
  the exact temporal decision identity/result/reasons.
- [ ] Strict state binds `strict_cutoff` and qualified exact-revision proof when
  used; generic filing availability cannot be persisted as exact-revision
  authority.
- [ ] Cache checkpoint 1.2, ledger 1.6, statement/indicator manifest v4,
  routing results, selection dispatch/checkpoint entries, audit/report rows, and
  tool projections reject any contradictory temporal identity, result, reason,
  mode, proof, calendar, plan, context, cache, or artifact binding before
  callable restore.
- [ ] Exact replay covers every accepted and rejected deterministic temporal
  case and yields byte-identical safe decisions and stable IDs with zero I/O,
  permits, attempts, evaluator calls, clock reads, calendar-frontier lookups,
  repairs, or historical writes.
- [ ] Plan-1.2/context-v1, old/absent temporal policy, unknown reason/version,
  malformed proof, or mismatched plan-1.3 checkpoint fails with a typed
  incompatibility or requires explicit `START_NEW` before work.
- [ ] `START_NEW` allocates a new run ID and follows fresh acquisition. It does
  not reuse a failed run namespace, upgrade a checkpoint, or reinterpret
  historical evidence.
- [ ] Operational projections carry safe decision identity/result/reasons only;
  raw payloads/errors, normalized provider values, credentials, or mutable
  provider state do not cross the existing trusted boundary.

## Exact contract and version requirements

- Capability Routing Plan payload `1.3`; Financial Eligibility Acquisition
  Context `financial-eligibility-acquisition-context-v2` with identity namespace
  `financial-eligibility-acquisition-context:v2:`.
- Temporal policy `provider-neutral-temporal-eligibility-v1`, calendar binding
  `market-session-eligibility-binding-v1`, and decision projection
  `financial-metadata-temporal-eligibility-decision-v1` with their exact v1
  identity namespaces.
- Provider Subrequest Cache checkpoint `1.2` and financial dispatch ledger
  `1.6`.
- `financial-statement-routing-result-v2` and
  `financial-indicator-routing-result-v4`; both statement and indicator new
  writes use `financial-manifest-v4` / `financial-manifest:v4`.
- `financial-statement-selection-dispatch-v2`,
  `financial-statement-selection-checkpoint-v2`,
  `financial-indicator-selection-dispatch-v3`, and
  `financial-indicator-selection-checkpoint-v3`.
- `financial-completeness-row-v5`,
  `financial-aggregate-completeness-v4`, `financial-manifest-audit-v4`,
  `financial-manifest-request-audit-v4`, Financial Dispatch Audit Projection
  `4.0`, `financial-tool-manifest-v4`, and
  `financial-tool-message-envelope-v4` wherever temporal decisions surface.
- Preserve the exact plan-1.2 indicator semantic bindings and unchanged raw
  Financial Period Candidate/Identity namespaces. `mainland-financial-report-
  projection-v2`, Canonical Evidence, Source Fact, Decision Report, and Decision
  Audit remain unchanged and never recompute temporal eligibility.

## Migration and compatibility behavior

- Historical artifacts, candidates, manifests, routing results, checkpoints,
  ledgers, attempts, reports, audits, tool projections, run IDs, and decision IDs
  remain readable and byte-for-byte untouched under their recorded contracts.
- A historical plan-1.2/context-v1 run continues under its pinned contract only.
  A plan-1.3 reader never applies the new calendar frontier or reason taxonomy
  retroactively and never upgrades state in place.
- The failed historical Ticket 07 run remains immutable and retains its recorded
  local-date-contract interpretation. New temporal contracts cannot relabel its
  evidence or reuse its run/checkpoint namespace.
- No second checkpoint, sidecar, report/audit authority, or ID namespace is
  introduced; the existing checkpoint-aware dispatcher boundary remains owner.

## Explicit non-goals

- Fresh provider proof projection/acquisition owned by 02H, a new cache or
  checkpoint owner, restore-time acquisition/repair, or current-calendar
  reinterpretation.
- Historical migration/rewrite/relabeling, implicit checkpoint upgrade,
  defaulted temporal proof, or reuse of a failed run identity.
- Route/order/fallback, completeness, financial-period eligibility, Evidence
  Admission, Decision Gate, BaoStock/daily-route authority, or shadow-authority
  changes.
- Live providers, paid models, integration tests, Ticket 07, prototype code
  copy/import, or enabling authoritative `qualified_v1`.

## Deterministic regressions

- Produce plan-1.3 checkpoints for every accepted/rejected 06B temporal row;
  close, restore, and assert exact decision/identity/projection equality and zero
  external work or restore-time calendar/evaluator access.
- Independently mutate or omit every temporal version, calendar revision/binding,
  requested session/mode, evaluation instant, frontier, proof timestamp,
  artifact identity, result, reason, context/cache/plan binding, manifest, and
  ledger version; assert typed pre-exposure failure.
- Exercise plan-1.2/context-v1 and absent/unknown temporal state under plan 1.3
  through typed incompatibility and explicit `START_NEW` with sentinels proving
  no repair, provider/model I/O, or historical write.
- Open representative historical semantic/temporal versions—including the
  failed Ticket 07 state—and verify recorded readers without byte changes or
  current-registry/calendar reinterpretation.
- Regress semantic compatibility, completeness, financial-period eligibility,
  attempts/cache, reports/audits, Evidence Admission, BaoStock authority, daily
  routing, and `shadow_non_authoritative` disposition.

## Verification commands

Run deterministic checkpoint, replay, and compatibility tests only:

```powershell
pytest -q tests/test_financial_temporal_checkpoint_compatibility.py tests/test_qualified_routing_checkpoint_context_restore.py tests/test_qualified_routing_factory_checkpoint_restore.py
pytest -q tests/test_provider_subrequest_cache.py tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py tests/test_checkpoint_resume.py
pytest -q tests/test_financial_version_persistence.py tests/test_financial_manifest_evidence_reporting.py tests/test_recorded_evidence_replay.py
pytest -q tests/test_reporting.py tests/test_decision_audit.py tests/test_admitted_evidence_binding.py tests/test_evidence_gates.py
ruff check .
git diff --check
git status --short
```

No command may select integration tests or contact a provider or model.

## Definition of done

- [ ] Every exact plan/context/cache/ledger/manifest/downstream temporal version
  is emitted and cross-bound without changing preserved semantic/raw versions.
- [ ] Every accepted and rejected temporal case restores exactly with zero I/O,
  permits, attempts, evaluator/clock/frontier calls, repair, or rewrite.
- [ ] Old/absent/corrupt/mismatched temporal state fails typed or requires
  explicit `START_NEW`; historical readers and the failed Ticket 07 state remain
  unchanged.
- [ ] Spec Compliance then Code Quality review finds no unresolved Critical or
  Important issue; no provider/model I/O occurred, no prototype code was copied,
  and authoritative `qualified_v1` remains disabled.

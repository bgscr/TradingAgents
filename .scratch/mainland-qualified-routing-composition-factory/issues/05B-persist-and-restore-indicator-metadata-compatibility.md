# 05B — Persist and restore indicator metadata compatibility

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Persist the exact indicator semantics and compatibility
decision made by the plan-1.2 qualified dispatcher, then restore it without
reassessment or external work. New indicator manifests, selections, routing
results, checkpoint entries, ledger, audit/report projections, and tool
envelopes all agree on the pinned assessment identities/states and typed
reasons. Historical readers keep interpreting old material under its recorded
contracts, while an old or compatibility-absent checkpoint cannot silently
resume as plan 1.2.

**Blocked by:** 02E — Financial-indicator semantic metadata contracts; 02F —
Truthful provider indicator normalization and compatibility
evaluation; 05A — Bind and restore eligibility context through checkpoints.

**Status:** completed

## Observable behavior

- A fresh plan-1.2 qualified shadow run publishes an indicator manifest,
  provider-candidate wrapper, period selection/rejection, routing result,
  dispatch/checkpoint entry, ledger, operational audit/report projection, and
  tool message that all pin the same field-semantics version, metadata-
  compatibility version, assessment identities/states, raw-marker-safe
  diagnostics, and canonical typed incompatibility reasons.
- Selected and unresolved families agree exactly across manifest v3,
  financial-period selection v3, and indicator routing result v3. A retained
  complete-but-incompatible candidate remains auditable and cannot appear as
  selected evidence.
- Closing and restoring an exact checkpoint reproduces context, plan,
  dispatcher/cache, artifacts, candidates, completeness, compatibility,
  rejections, selections, attempts, manifests, audits, tool projections, and IDs
  exactly with zero provider/network/model I/O, zero coordinator permits, and
  zero new attempts.
- A plan-1.2 restore from an old or compatibility-absent checkpoint fails with a
  typed compatibility outcome or requires the explicit existing `START_NEW`
  choice. `START_NEW` allocates a new run identity and follows the fresh path.
- Restore never consults the current registry to repair historical state, never
  reacquires provider metadata, and never upgrades a checkpoint in place.

## Acceptance criteria

- [ ] Every new indicator projection carries the plan identity and exact pinned
  `financial-indicator-field-semantics-v1` and
  `financial-indicator-metadata-compatibility-v1` versions plus required
  assessment identity/state and safe typed-reason bindings.
- [ ] Indicator manifests retain compatible and incompatible candidates,
  unchanged completeness outcomes, raw-marker-safe diagnostics, and typed
  incompatibility reasons without leaking normalized provider values, raw
  payloads/errors, or credentials into operational audit projections.
- [ ] Indicator provider-candidate wrappers, accepted/current-only selections,
  and typed rejections bind metadata-assessment identities while raw candidate
  and Financial Period identities remain v1.
- [ ] Manifest v3 selected/unresolved outcomes, period selection v3, and routing
  result v3 are mutually consistent and reject contradictions before
  publication or callable restore.
- [ ] Dispatch ledger 1.5 carries statement manifest v2, indicator manifest v3,
  semantics/compatibility versions, all assessment identities/states, and the
  existing context/cache/plan bindings.
- [ ] Operational report/audit and tool projections carry only safe exact
  indicator bindings. Statement projections stay on their existing v2
  contracts; canonical Evidence, Source Fact, Decision Report, and Decision
  Audit contracts do not change.
- [ ] Exact restore validates every plan/context/cache/manifest/selection/
  assessment binding before exposure and performs zero I/O, permits, attempts,
  repairs, writes, or semantic reassessment.
- [ ] Missing, old/absent, unknown-version, corrupt, contradictory, or mismatched
  compatibility state under plan 1.2 fails typed and secret-free or requires
  explicit `START_NEW`; no default or current-registry reinterpretation is
  allowed.
- [ ] The unchanged 100% critical / at-least-90% declared-core completeness
  gates, financial-period eligibility v2, current-only/strict-PIT boundaries,
  Capability Routing Plan authority, BaoStock authority, Evidence Admission,
  daily AKShare → BaoStock → Yahoo, and `shadow_non_authoritative` disposition
  remain intact.

## Exact contract and version requirements

- Financial Period Candidate/Identity stay at contract `1.0`,
  `financial-period-candidate:v1`, and `financial-period:v1`; raw candidate
  identities remain v1.
- Add `financial-provider-candidate-v2` and
  `financial-period-selection:v3` bindings, with selection contract
  `financial-period-selection-v3`.
- Add `financial-manifest-v3` and identity namespace `financial-manifest:v3` for
  indicators. Statement manifests remain `financial-manifest-v2`.
- Add `financial-indicator-routing-result-v3`.
- Add `financial-indicator-selection-dispatch-v2` and
  `financial-indicator-selection-checkpoint-v2`, each binding routing-result v3
  and manifest-v3 identities.
- Add exact dispatch checkpoint ledger version `1.5`.
- Add `financial-completeness-row-v4`,
  `financial-aggregate-completeness-v3`, `financial-manifest-audit-v3`,
  `financial-manifest-request-audit-v3`, and Financial Dispatch Audit Projection
  `3.0` for indicators. Statement operational projections remain unchanged.
- Add `financial-tool-manifest-v3` and
  `financial-tool-message-envelope-v3` for indicators. Statement tool
  projections remain v2.
- Bind all new writes to Capability Routing Plan payload 1.2 and the exact
  Ticket 02E semantics/compatibility identities. No additional version,
  namespace, sidecar, or migration is permitted.

## Deterministic regressions

- Produce a plan-1.2 checkpoint from a fresh built-in run containing compatible,
  complete-incompatible, partial, current-only, and typed-rejected indicator
  candidates; close, restore, and assert exact projection/identity equality and
  zero I/O/permit/attempt behavior.
- Mutate or omit each semantics version, compatibility version, assessment
  identity/state, typed reason, manifest identity, selection, routing result,
  plan/context/cache binding, and ledger version independently; assert typed
  pre-exposure failure.
- Assert selected/unresolved contradictions among manifest v3, selection v3,
  provider-candidate v2, routing result v3, dispatch/checkpoint v2, audit 3.0,
  and tool envelope v3 are rejected deterministically.
- Open representative historical candidate, selection, manifest, routing,
  dispatch, checkpoint, ledger, report/audit, and tool versions and verify their
  recorded readers without rewriting or applying the current registry.
- Exercise old/absent compatibility checkpoints under plan 1.2 through typed
  failure and explicit `START_NEW`; install provider/model/coordinator/store
  sentinels to prove restore does no repair or external work.
- Regress Evidence Admission, report/decision audit, historical replay,
  completeness, financial-period eligibility, current-only/strict-PIT, BaoStock
  authority, daily routing, and shadow authority.

## Historical compatibility

- Historical artifacts, candidates, selections, manifests, routing results,
  dispatch/checkpoint entries, ledgers, audits, reports, tool projections, and
  IDs remain readable, byte-for-byte untouched, and interpreted only under
  their recorded contracts.
- The new registry is never applied retroactively. Stored v1 Tushare/BaoStock
  artifacts/results and raw candidate/period identities are never relabeled.
- No in-place migration, semantic reinterpretation, ID renumbering, checkpoint
  repair, or historical write-back occurs. `START_NEW` always allocates a new
  run identity.

## Explicit non-goals

- Provider normalization/selection logic owned by Ticket 02F, route changes,
  provider-specific router behavior, completeness or financial-period policy
  changes, or Evidence Admission rewrite.
- New cache, checkpoint sidecar, persistence/report authority, competing ID
  namespace, historical migration, current-registry repair, or live restore.
- Changes to statement projection versions, canonical Evidence/Source Fact/
  Decision contracts, BaoStock authority, daily routing, or shadow authority.
- Live providers, paid models, real credentials, integration tests, copying
  either prototype implementation, or enabling authoritative `qualified_v1`.

## Verification commands

Run deterministic persistence, checkpoint, report, and replay tests only:

```powershell
pytest -q tests/test_financial_indicator_compatibility_persistence.py tests/test_financial_version_persistence.py tests/test_financial_manifest_evidence_reporting.py
pytest -q tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py tests/test_qualified_routing_checkpoint_context_restore.py tests/test_qualified_routing_factory_checkpoint_restore.py
pytest -q tests/test_checkpoint_resume.py tests/test_recorded_evidence_replay.py tests/test_reporting.py tests/test_decision_audit.py
pytest -q tests/test_admitted_evidence_binding.py tests/test_evidence_gates.py tests/test_financial_period_eligibility.py
ruff check .
git diff --check
git status --short
```

No command may select integration tests or contact a live provider or model.

## Definition of done

- [ ] Every exact forward contract/version is emitted and cross-bound as
  specified, with statement and unchanged raw identity versions preserved.
- [ ] Exact plan-1.2 restore reproduces all compatibility state with zero I/O,
  permits, attempts, repairs, or writes.
- [ ] Old/absent and corrupt/mismatched compatibility checkpoints fail typed or
  require explicit `START_NEW`, and historical readers pass without rewrite or
  reinterpretation.
- [ ] Tests prove audit/report/tool safety, selection consistency, unchanged
  completeness/PIT/routing/Evidence Admission/authority boundaries, and no
  hidden historical migration.
- [ ] Spec Compliance and Code Quality review find no unresolved Critical or
  Important issue; no live traffic occurred, no prototype code was copied, and
  authoritative `qualified_v1` remains disabled.

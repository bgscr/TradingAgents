# 05 — Restore checkpoint and cache state through the factory

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Bind the managed production composition at the existing
checkpoint-aware financial tool/dispatcher seam so a compatible
\`qualified_v1_shadow\` run restores its exact terminal Provider Subrequest
Cache and shadow dispatcher state before adapters become callable. Repeating a
completed request must return the identical artifacts, attempts, candidates,
selections, and IDs with zero provider, coordinator-permit, network, or model
I/O; incompatible or corrupt state must fail typed without live repair.

**Blocked by:** 01 — Add the production composition contract and immutable
blueprint.

**Status:** completed

## Observable behavior

- Run binding waits until the authoritative Instrument Identity, canonical run
  ID, active plan, financial eligibility binding, authoritative/shadow ledgers,
  artifact references, and checkpoint compatibility are known.
- Resume validation covers asset configuration, plan signature, eligibility
  policy, run scope, provider-chain/topology binding, artifacts, and Provider
  Subrequest Cache checkpoint before constructing callable adapters.
- Terminal cache entries restore from existing immutable artifact references
  first; the existing shadow financial dispatch ledger then restores candidates,
  selection progress, results, attempts, and dispatcher state.
- For \`qualified_v1_shadow\`, the authoritative dispatcher remains legacy and
  receives no qualified router. A distinct shadow dispatcher receives the bound
  qualified router, request factories, and the single restored cache.
- An exact repeated request returns entirely from restored local state with zero
  provider transports, coordinator permits, network calls, new physical-attempt
  events, or model calls.
- Missing, malformed, incompatible, or corrupt projections fail through existing
  typed boundaries before external work; no provider call repairs state and no
  subset is opportunistically reused.

## Acceptance criteria

- [ ] The existing shadow financial dispatch ledger remains the sole owner of
  qualified selection progress and Provider Subrequest Cache checkpoint data;
  no sidecar checkpoint or competing schema is introduced.
- [ ] Restoration validates all identity/topology/artifact/cache bindings before
  any adapter can be called and rejects mismatched asset, plan, eligibility,
  run, provider route, artifact, attempt, or cache state deterministically.
- [ ] Available cache entries require valid immutable artifacts and consistent
  existing attempt/sequence/subrequest identities; terminal unavailable entries
  and reuse counts restore exactly under their existing contracts.
- [ ] Interrupted/non-terminal entries are never restored or serialized as
  available. Checkpoint publication remains limited to the existing safe
  terminal boundary; the existing unsafe-checkpoint failure is unchanged.
- [ ] Exact-resume tests assert identical checkpoint projection, artifact
  references, outcomes, candidates, selections, attempts, IDs, and rendered
  shadow state before and after reconstruction.
- [ ] Corrupt/missing artifacts, malformed cache entries, plan/eligibility
  mismatches, and provider-topology changes fail typed with zero transport,
  coordinator-permit, attempt, model, checkpoint rewrite, or artifact mutation.
- [ ] A fresh run after an incompatible qualified checkpoint uses a new run ID
  according to existing rules and does not rewrite or partially salvage the old
  checkpoint.

## Deterministic regressions

- Complete a fake qualified shadow request, checkpoint at the safe terminal
  boundary, close all resources, reconstruct exclusively through the built-in
  factory, repeat the request, and assert exact state plus all external-I/O
  counters at zero.
- Restore shared bulk entries used by multiple periods/families/graph calls and
  assert deterministic reuse without a new physical attempt.
- Parameterize asset signature, plan signature, eligibility binding, run scope,
  provider topology, cache key, attempt identity, artifact digest/reference,
  missing artifact, malformed payload, and non-terminal entry corruption.
- Preserve pre-v2/mismatched qualified start-new-or-fail-typed behavior and
  existing unsafe-checkpoint behavior.
- Prove authoritative legacy dispatch, legacy checkpoint resume, report/audit
  rendering, and \`shadow_non_authoritative\` remain unchanged.

## Compatibility and migration behavior

- Reuse the existing checkpoint-aware financial tool/dispatcher seam,
  ProviderSubrequestCache checkpoint contract, immutable artifact store,
  dispatcher ledgers, schemas, and ID constructors.
- Legacy checkpoints remain readable by the legacy path. Pre-v2 or incompatible
  qualified checkpoints follow current start-new/fail-typed rules; none receives
  an in-place upgrade.
- Historical checkpoints, artifacts, attempts, manifests, reports, audits,
  provider rows, snapshots, pins, and IDs are never rewritten, deleted,
  relabeled, or renumbered.
- Local checkpoint/artifact reads are the only restoration boundary. Existing
  authority, routes, eligibility, completeness, and evidence semantics do not
  change.

## Explicit non-goals

- A new cache, checkpoint sidecar, persistence authority, artifact schema,
  router, provider registry, ID namespace, or migration framework.
- Live repair, historical checkpoint rewrite, partial salvage of incompatible
  state, deletion of corrupt artifacts, or external provider/model calls during
  restore.
- Changing provider topology, completeness, financial eligibility, Evidence
  Admission, Decision Gate, BaoStock authority, daily routing, or shadow
  disposition.
- Live providers, paid models, qualification runs, or speculative replay work.

## Verification commands

\`\`\`powershell
pytest -q tests/test_qualified_routing_factory_checkpoint_restore.py
pytest -q tests/test_provider_subrequest_cache.py tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py
pytest -q tests/test_checkpoint_resume.py tests/test_financial_version_persistence.py tests/test_qualified_routing_rollout.py
ruff check .
git diff --check
git status --short
\`\`\`

Use retained fake immutable artifacts and disposable existing stores only.
Network/provider/model constructors must be guarded by zero-call spies during
every restoration scenario.

## Definition of done

- [ ] QCF12 passes at the built-in factory → graph → checkpoint-aware dispatcher
  seam with exact state equality and zero provider/model I/O on repeat.
- [ ] Every corrupt or incompatible control fails typed before callable adapters
  and leaves the historical checkpoint/artifacts byte-for-byte unchanged.
- [ ] Legacy and pre-v2 compatibility behavior passes without schema or fixture
  changes.
- [ ] Focused tests, Ruff, whitespace, secret/payload, and scope checks pass.

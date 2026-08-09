# 05A — Bind and restore eligibility context through checkpoints

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Version the existing qualified checkpoint projection with
the Financial Eligibility Acquisition Context contract version, deterministic
identity, and safe projection. On restore, validate its exact run/asset/plan/
eligibility/provider-topology binding and atomically bind the context before the
Provider Subrequest Cache, request factories, or dispatcher become callable.
An exact compatible resume performs zero provider/model I/O; missing, corrupt,
or mismatched context fails typed without live reconstruction or historical
rewrite.

**Blocked by:** 02A — Add provider-neutral eligibility acquisition context
contracts; 02B — Acquire fresh company type and listing provenance; 02C —
Integrate pure financial request factories; 04A — Integrate managed eligibility-
context acquisition; 05 — Restore checkpoint and cache state through the
factory.

**Status:** completed

## Observable behavior

- A fresh successful qualified shadow checkpoint retains the context contract
  version, identity, and complete safe projection alongside the existing plan,
  eligibility, provider topology, dispatcher/cache, artifact, candidate,
  selection, attempt, and ID bindings.
- Restore validates exact context/run/Run Asset Configuration/Instrument
  Identity/as-of/availability/plan/eligibility/company-type evidence/listing
  provenance/provider-topology compatibility before exposing any cache,
  request factory, dispatcher, adapter, or router call.
- An exact compatible resume binds the restored immutable context first,
  restores the existing Provider Subrequest Cache and dispatcher state, and
  repeats a terminal financial request with identical outputs and zero provider,
  network, model, coordinator-permit, or new physical-attempt work.
- Missing context, unknown context version, corrupt safe projection, identity
  mismatch, cross-run/asset/plan/eligibility/provider-topology mismatch, and
  context/cache contradiction fail through typed safe restoration outcomes.
- Restore never invokes live company-type or listing acquisition, never repairs
  a missing context from current providers, and never rewrites the historical
  checkpoint or its referenced artifacts.

## Acceptance criteria

- [ ] The qualified checkpoint version explicitly persists the context version,
  identity, and every safe projection field needed for exact reconstruction and
  validation, with no raw payload/error/credential or mutable runtime state.
- [ ] Context validation and atomic host binding complete before restored cache,
  factory, dispatcher, adapter, or router exposure.
- [ ] A checkpoint produced from a fresh built-in acquisition resumes the exact
  request with identical context, artifacts, attempts, candidates, selections,
  outcomes, and IDs and with zero provider/network/model I/O, coordinator
  permits, or new attempts.
- [ ] Missing, corrupt, unknown-version, cross-bound, and context/cache/provider-
  topology contradiction fixtures fail typed and secret-free before callable
  runtime exposure.
- [ ] No restore failure invokes the metadata acquirer or any live source, and
  no success/failure path rewrites historical checkpoint bytes or referenced
  immutable material.
- [ ] Legacy/pre-context checkpoints retain their specified read/start-new/
  typed-incompatibility behavior and are never silently upgraded.

## Deterministic regressions

- Create a checkpoint from a deterministic fresh built-in acquisition and
  terminal qualified request, close all resources, restore, repeat the request,
  and assert exact zero-I/O/zero-permit/zero-new-attempt behavior.
- Mutate each context identity/projection and run/asset/plan/eligibility/
  provider-topology/cache binding independently and assert the exact typed
  pre-exposure failure.
- Remove the context, change its version, corrupt canonical serialization, strip
  listing/evidence provenance, and inject secret/raw-error canaries.
- Install sentinels on metadata acquisition, provider/model transports,
  coordinator permits, cache writes, artifact writes, and checkpoint writes to
  prove restore never live-reconstructs or rewrites history.
- Verify context is bound before cache/dispatcher/factory access under ordinary,
  concurrent, interrupted, and cleanup-failure restore paths.

## Compatibility and migration behavior

- Checkpoint evolution is explicit and additive for new qualified runs.
  Historical checkpoints, reports, artifacts, attempts, provider rows,
  selections, and IDs remain byte-for-byte untouched.
- Older checkpoint versions follow existing compatibility policy; missing new
  context is never inferred, synthesized, fetched live, or written back.
- Existing cache/artifact/dispatcher identity constructors and restoration
  authority remain in place; no sidecar, parallel persistence format, or new ID
  namespace is introduced.

## Explicit non-goals

- Live repair, live metadata reacquisition during restore, checkpoint rewrite,
  historical migration, synthesized default context, or partial compatibility
  based on ticker/name/provider availability.
- New cache, persistence authority, checkpoint sidecar, ID namespace, router,
  provider-order table, fallback, completeness logic, or mutable context.
- Provider/source implementation, ordinary fresh acquisition policy changes,
  live providers, real credentials, paid models, integration tests, or the real
  rollout gate.
- Copying/importing fresh-run prototype implementation or tests.
- Enabling or recommending authoritative `qualified_v1`.

## Verification commands

Run deterministic checkpoint, cache, dispatcher, and graph suites only:

```powershell
pytest -q tests/test_qualified_routing_checkpoint_context_restore.py tests/test_qualified_routing_factory_checkpoint_restore.py tests/test_checkpoint_resume.py
pytest -q tests/test_provider_subrequest_cache.py tests/test_financial_version_persistence.py tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py
pytest -q tests/test_recorded_evidence_replay.py tests/test_financial_manifest_evidence_reporting.py
ruff check .
git diff --check
git status --short
```

No verification command may select integration tests or contact a provider or
model.

## Definition of done

- [ ] A fresh-produced checkpoint restores exact context and financial runtime
  state with identical outputs and zero provider/model/coordinator work.
- [ ] Every missing/corrupt/mismatched fixture fails typed before exposure and
  proves no live reconstruction or historical rewrite occurred.
- [ ] Spec Compliance review confirms exact binding topology, restore ordering,
  compatibility, fail-closed behavior, and non-goals.
- [ ] Code Quality review confirms safe serialization, typed validation,
  restoration durability, maintainability, and no Critical or Important
  findings.
- [ ] No live traffic occurred, no prototype code was copied, and authoritative
  `qualified_v1` remains disabled.

# 02B — Acquire fresh company type and listing provenance

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Implement a provider-neutral eligibility metadata acquirer
that can originate one complete Financial Eligibility Acquisition Context for a
fresh qualified shadow run. It must use the same immutable Capability Routing
Plan and router as ordinary acquisition, resolve Financial Company Type exactly
once through the existing resolver, obtain authoritative listing provenance
through the existing BaoStock lifecycle capability's provider-neutral
projection, and reuse exact compatible retained metadata before physical work.

**Blocked by:** 02A — Add provider-neutral eligibility acquisition context
contracts; 02 — Assemble run-owned production provider adapters.

**Status:** ready-for-agent

## Observable behavior

- Given only authoritative Instrument Identity, run/asset bindings, as-of date,
  requested availability, purpose, eligibility binding, and the immutable plan,
  the acquirer can return a complete immutable context without seeded runtime or
  checkpoint state.
- Company-type evidence traverses only the plan-declared prerequisite route,
  sequentially, through the one injected router, Provider Subrequest Cache, and
  Provider Request Coordinator. The existing versioned
  `resolve_financial_company_type()` is invoked exactly once per acquisition.
- Tushare and AKShare-Sina contribute only qualified artifact-bound declarations
  or whitelisted classifier inputs; Yahoo contributes only its allowed current
  classifier projection and gains no strict PIT or qualified-declaration
  authority.
- Listing date and Financial Listing Provenance arrive through the existing
  `issuer_lifecycle -> baostock` route and a provider-neutral lifecycle
  projection. BaoStock-specific candidates do not escape the adapter boundary.
- Exact compatible retained company-type and lifecycle projections are checked
  first and reused without physical work; incompatible retained metadata is
  rejected or reacquired only through the allowed current-work plan routes.
- Unknown/unresolved, ambiguous, contradictory, invalid, mismatched, or missing-
  provenance outcomes fail typed and secret-free. No ordinary financial request
  factory, dispatcher, partial context, or callable runtime is exposed by this
  acquisition slice before success.

## Acceptance criteria

- [ ] A fresh deterministic run with no seeded context/checkpoint acquires safe
  company-type evidence, invokes the existing resolver exactly once, obtains
  authoritative BaoStock listing provenance, and returns one fully validated
  context.
- [ ] The acquirer consumes the same immutable plan/router/cache/coordinator
  authorities supplied by the managed composition and creates no bootstrap
  router, route table, fallback loop, cache, or provider client.
- [ ] Exact retained compatible projections cause zero transport calls and zero
  new physical-attempt events while producing the same context identity.
- [ ] Provider work remains sequential, single-flight/cache aware, and exactly
  attempt-accounted; no hidden retries, fan-out, duplicate bulk call, or
  speculative prewarming occurs.
- [ ] Resolver invocation count is exactly one for success and every terminal
  safe resolution outcome that reaches resolution.
- [ ] Unknown, ambiguous, contradictory, invalid, mismatched, missing company-
  type evidence, and missing listing provenance all fail with the specified
  typed acquisition result and no partial callable runtime.

## Deterministic regressions

- Use fake clocks, fake sleepers, fake provider facades/transports, disposable
  existing coordinator/cache stores, and immutable artifact fixtures only.
- Cover Tushare-qualified success, AKShare-Sina sequential salvage, Yahoo
  classifier-only terminal evidence, and exact no-lower-provider behavior.
- Cover retained metadata hits, incompatible/expired/wrong-plan/wrong-asset
  projections, and exact physical-attempt cardinality for each path.
- Cover BaoStock lifecycle success, absent listing date, stripped provenance,
  wrong asset binding, provider-specific candidate leakage, interruption, and
  raw-error/secret canaries.
- Assert no ordinary financial request factory or dispatcher is called during
  acquisition and no provider/model I/O occurs for retained-compatible reuse.

## Compatibility and migration behavior

- Existing retained artifacts are reused only through exact safe projections;
  raw or historically incompatible material is neither rewritten nor promoted.
- Historical reports, checkpoints, artifacts, provider attempts, selections,
  IDs, lifecycle authority, and plan versions remain byte-for-byte untouched.
- Existing resolver policy and BaoStock listing authority are reused exactly;
  this ticket adds acquisition orchestration, not new classification or listing
  rules.

## Explicit non-goals

- Ordinary statement/indicator request construction, factory/dispatcher
  exposure, graph-host single assignment, checkpoint persistence/restoration,
  or any live rollout.
- A second router, route registry, provider order, duplicate company-type
  resolver, alternate listing authority, request-factory inference, or model-
  authored classification.
- Live providers, real credentials, paid models, integration tests, prewarming,
  replay-only acquisition, parallel provider fan-out, or hidden retry.
- Copying/importing the fresh-run prototype implementation or its tests into
  production.
- Enabling or recommending authoritative `qualified_v1`.

## Verification commands

Run deterministic acquisition and existing adapter/coordination suites only:

```powershell
pytest -q tests/test_financial_eligibility_metadata_acquisition.py tests/test_financial_eligibility_acquisition_context.py
pytest -q tests/test_baostock_capability_adapters.py tests/test_provider_subrequest_cache.py tests/test_provider_request_coordinator.py
pytest -q tests/test_production_qualified_provider_composition.py tests/test_mainland_physical_attempt_coordination.py
ruff check .
git diff --check
git status --short
```

No verification command may select integration tests or contact a provider or
model.

## Definition of done

- [ ] Fresh and retained-compatible paths pass with exact resolver, router,
  cache, coordinator, provider-attempt, listing-provenance, and context-identity
  evidence.
- [ ] Every typed failure proves no partial context, factory, dispatcher, or
  callable runtime escaped and all diagnostics are payload-free.
- [ ] Spec Compliance review confirms plan authority, one resolver call,
  BaoStock lifecycle provenance, reuse rules, compatibility, and non-goals.
- [ ] Code Quality review confirms deterministic concurrency/attempt behavior,
  typed boundaries, maintainability, and no Critical or Important findings.
- [ ] No live traffic occurred, no prototype code was copied, and authoritative
  `qualified_v1` remains disabled.

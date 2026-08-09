# 02C — Integrate pure financial request factories

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Version the statement and indicator routing-request contracts
so a complete Financial Eligibility Acquisition Context can be projected into
ordinary routing without losing company-type resolution/evidence, listing
provenance, eligibility, availability, or as-of bindings. Make both request
factories pure two-input functions:
`(FinancialToolRequest, Financial Eligibility Acquisition Context) -> typed
routing request`.

**Blocked by:** 02A — Add provider-neutral eligibility acquisition context
contracts.

**Status:** completed

## Observable behavior

- Statement and indicator routing requests retain the complete Financial
  Company Type resolution, including resolution method/version, canonical
  inputs, candidates, ambiguity/contradiction state, and safe artifact/provider
  evidence binding rather than only a resolved enum.
- Both request types retain authoritative listing date and Financial Listing
  Provenance, financial eligibility binding, Financial Availability Scope,
  acquisition purpose, run as-of date, plan signature, run/asset/context
  identities, consolidation scope, and the context's canonical acquisition
  periods.
- Each factory accepts only a FinancialToolRequest and immutable context,
  validates exact run/asset/as-of/scope/plan/eligibility/context binding, and
  deterministically projects a typed routing request.
- Repeated calls with identical inputs return canonically identical requests and
  do not mutate either input or any shared state.
- A mismatch fails typed before a routing request exists. No provider, router,
  cache, coordinator, retained-artifact, clock, model, fallback, completeness,
  resolver, or lifecycle dependency is reachable from either factory.

## Acceptance criteria

- [ ] Versioned statement and indicator request contracts preserve every
  required context binding through canonical serialization and identity.
- [ ] Both request factories have the exact two-input pure conceptual interface
  and require no optional provider/runtime/state parameter or hidden global.
- [ ] Factories validate binding equality and project the context's already
  computed periods; they never resolve company type, infer listing data, rebuild
  eligibility periods, choose providers, route, retry, fall back, or apply
  completeness policy.
- [ ] Every run/asset/identity/as-of/scope/plan/eligibility/company-type/
  evidence/listing/context mismatch returns a typed safe failure and no partial
  routing request.
- [ ] Valid bank and non-bank fixtures produce complete statement and indicator
  requests with identical evidence/listing bindings and zero side effects.
- [ ] Tests prove zero provider/model/coordinator/cache/router calls and input
  immutability for success and failure paths.

## Deterministic regressions

- Use fixed FinancialToolRequest and immutable context fixtures for statement
  and indicator requests across bank and non-bank resolutions.
- Snapshot canonical request identities/projections and independently mutate
  every binding-bearing field to prove fail-closed validation.
- Inject sentinels for provider clients, router, cache, coordinator, resolver,
  fallback, completeness, clocks, and model seams and prove none is touched.
- Exercise repeated and concurrent pure calls to prove deterministic output and
  no context/request mutation.
- Verify safe evidence and listing provenance survive request serialization,
  routing selection, and existing deterministic dispatcher projections.

## Compatibility and migration behavior

- Request contracts change through explicit versioning. Historical requests,
  checkpoints, reports, artifacts, attempts, selections, and IDs are not
  rewritten or upgraded in place.
- Old request versions retain their existing read or typed incompatibility
  behavior. Missing new bindings are never inferred from ticker/name, provider
  output, model text, or live reconstruction.
- Existing provider routing, completeness, eligibility, Evidence Admission,
  Decision Gate, and shadow authority remain unchanged.

## Explicit non-goals

- Provider calls, metadata acquisition, retained-metadata reuse, routing,
  fallback, completeness evaluation, retries, company-type resolution, listing
  inference, graph-host lifecycle, dispatcher exposure, or checkpoint restore.
- Adding provider clients, router/cache/coordinator handles, mutable context, a
  second route registry, or process-global state to request factories.
- Copying/importing fresh-run prototype code or treating it as an implementation
  shortcut.
- Live providers, real credentials, paid models, integration tests, or enabling
  authoritative `qualified_v1`.

## Verification commands

Run deterministic request-contract and dispatcher suites only:

```powershell
pytest -q tests/test_financial_request_factory_context_binding.py tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py
pytest -q tests/test_financial_capability_routing_selection.py tests/test_financial_version_persistence.py
ruff check .
git diff --check
git status --short
```

No verification command may select integration tests or contact a provider or
model.

## Definition of done

- [ ] Statement and indicator requests retain all amended-spec bindings and
  pass deterministic identity/serialization/mismatch coverage.
- [ ] Purity tests prove the exact two-input interface, zero I/O/state access,
  input immutability, and deterministic repeated/concurrent results.
- [ ] Spec Compliance review confirms complete binding retention, exact
  prohibitions, version compatibility, and non-goals.
- [ ] Code Quality review confirms clear typed validation, maintainability, and
  no Critical or Important findings.
- [ ] No live traffic occurred, no prototype code was copied, and authoritative
  `qualified_v1` remains disabled.

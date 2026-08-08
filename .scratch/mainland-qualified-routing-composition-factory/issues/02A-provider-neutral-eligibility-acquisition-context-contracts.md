# 02A — Add provider-neutral eligibility acquisition context contracts

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Add the provider-neutral contracts that let a qualified
fresh run describe, validate, identify, and safely persist all metadata needed
before ordinary financial request construction. Version the one immutable
Capability Routing Plan with `financial_company_type_evidence`; add the closed,
immutable Financial Eligibility Acquisition Context, its safe company-type
evidence binding, deterministic identity/projection, and closed typed acquisition
failures. This slice is contract-only and performs no provider work.

**Blocked by:** 01 — Add the production composition contract and immutable
blueprint.

**Status:** ready-for-agent

## Observable behavior

- The next Capability Routing Plan version declares
  `financial_company_type_evidence` with the qualified shadow route `tushare ->
  akshare_sina -> yfinance`; its signature commits to the capability and route,
  and the plan remains the only provider-order authority.
- A closed immutable Financial Eligibility Acquisition Context binds its
  contract version/identity, canonical run ID, Run Asset Configuration
  signature, Instrument Identity, run as-of date, Financial Availability Scope,
  `foreground_current_analysis` purpose, eligibility-policy binding, routing
  plan signature, complete Financial Company Type resolution and safe evidence,
  BaoStock listing date/provenance, currency, consolidation scope, and canonical
  annual/reporting acquisition-period supersets.
- A safe company-type evidence binding retains accepted artifact/provider
  identities and canonical normalized resolver inputs without raw rows, raw
  provider errors, credentials, model prose, or operational mutable state.
- Context identity and its safe checkpoint projection change for every material
  field and remain stable across deterministic serialization round trips.
- Construction validates all run/asset/identity/currency/as-of/scope/plan/
  eligibility/resolution/listing/period cross-bindings atomically. No partial
  bind or field-by-field mutation API exists.
- Closed typed failures distinguish unavailable company-type evidence,
  unknown/unresolved, ambiguous, contradictory, unavailable listing provenance,
  binding mismatch, and interrupted acquisition without carrying a partial
  successful context or unsafe payload.

## Acceptance criteria

- [ ] The Capability Routing Plan has one new version whose prerequisite
  company-type-evidence route and signature are deterministic and cannot be
  reordered by a factory or source registration catalog.
- [ ] The Financial Eligibility Acquisition Context is closed and immutable,
  contains every field required by the amended specification, and rejects
  provider clients/functions, cache/router state, fallback/completeness policy,
  credentials, raw payloads/errors, and provider-specific candidates.
- [ ] Successful contexts reject `UNKNOWN`, ambiguity, unacceptable
  contradiction, missing qualified evidence binding, missing/invalid BaoStock
  listing provenance, and any cross-binding mismatch.
- [ ] Context identity and safe projection commit to every material field and
  are stable under canonical serialization; changing any material field changes
  identity.
- [ ] Typed acquisition failures are exhaustive for the required failure
  classes, safe to display/persist, and never contain a partial context.
- [ ] Contract construction and validation cause zero provider, model, graph,
  store, artifact, report, checkpoint, or coordinator work.

## Deterministic regressions

- Use fixed run/asset/identity/plan/eligibility/as-of/listing/evidence fixtures
  to cover one valid bank context and one valid non-bank context.
- Mutate every identity-bearing field independently and prove exact typed
  rejection or deterministic identity change as appropriate.
- Cover unknown, ambiguity, contradiction, unsafe evidence, missing artifact
  binding, missing listing provenance, invalid periods, broadened availability,
  wrong currency/venue/kind, and interrupted-acquisition fixtures.
- Round-trip the safe projection and verify immutability, canonical ordering,
  stable identity, secret/payload canaries, and rejection of extra fields.
- Prove the prior plan version retains its existing deterministic behavior and
  is never silently upgraded or assigned the new prerequisite route.

## Compatibility and migration behavior

- The change is additive through explicit contract and plan versioning.
  Historical plans, reports, checkpoints, artifacts, attempts, IDs, and safe
  projections are not rewritten, relabeled, or upgraded in place.
- Prior plan/checkpoint versions retain their existing read, start-new, or typed
  incompatibility behavior. They do not receive a synthesized context or route.
- Existing financial eligibility, completeness, Evidence Admission, Decision
  Gate, BaoStock authority, and daily mainland routing remain unchanged.

## Explicit non-goals

- Provider adapters, provider/source implementations, provider calls, live I/O,
  retained-metadata lookup, resolver invocation, graph lifecycle integration,
  request-factory integration, or checkpoint restoration.
- A second route registry, bootstrap router, provider-order table, fallback,
  completeness logic, hidden retry, provider fan-out, or process-global context.
- Inferring company type or listing date from symbols, names, financial rows,
  model text, provider installation, or incidental adapter behavior.
- Copying or importing fresh-run prototype code or treating prototype tests as
  production completion.
- Enabling or recommending authoritative `qualified_v1`.

## Verification commands

Run deterministic contract and routing-plan suites only:

```powershell
pytest -q tests/test_financial_eligibility_acquisition_context.py tests/test_mainland_capability_routing.py tests/test_qualified_routing_composition_contract.py
pytest -q tests/test_financial_version_persistence.py tests/test_financial_capability_routing_selection.py
ruff check .
git diff --check
git status --short
```

No verification command may select integration tests or contact a provider or
model.

## Definition of done

- [ ] Every observable behavior and acceptance criterion is covered by named,
  deterministic fixtures and passing tests.
- [ ] The contract/plan diff contains no provider implementation or I/O path and
  no production code copied from the fresh-run prototype.
- [ ] Spec Compliance review confirms exact fields, identities, cross-bindings,
  failures, plan authority, compatibility, and non-goals.
- [ ] Code Quality review confirms immutability, typed/safe validation,
  deterministic serialization, maintainability, and no Critical or Important
  findings.
- [ ] Authoritative `qualified_v1` remains disabled and no live provider/model
  work occurred.

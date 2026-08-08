# 02D — Add executable production-source wiring

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Replace exactly the six confirmed plan-declared production
source registrations that currently terminate as `NOT_CONFIGURED`, and add the
safe company-type evidence source projections required by the amended plan.
Bind only existing production adapters/facades through the existing source-
registration seam, one Provider Subrequest Cache, and one Provider Request
Coordinator. The immutable Capability Routing Plan remains the only provider-
order authority.

**Blocked by:** 02A — Add provider-neutral eligibility acquisition context
contracts; 02 — Assemble run-owned production provider adapters.

**Status:** ready-for-agent

## Observable behavior

- The Yahoo `financial_statements` registration executes annual/quarterly
  balance-sheet, income-statement, and cash-flow access through the run-owned
  Yahoo facade instead of returning terminal `NOT_CONFIGURED`.
- AKShare `financial_indicators`, Yahoo `current_financial_profile`, AKShare
  `adjustment_factors`, Yahoo-derived `derived_adjustment_factors`, and AKShare
  `issuer_name_events` likewise execute through their existing run-owned
  production facade/adapter boundaries.
- Yahoo-derived factors remain explicitly `derived=True` and `degraded=True`;
  Yahoo current profile remains current/classifier-only; issuer names remain
  descriptive; BaoStock alone retains Current Tradeability and lifecycle
  authority.
- Tushare, AKShare-Sina, and Yahoo expose only the amended spec's safe artifact-
  bound/whitelisted company-type evidence projections. Yahoo cannot claim a
  qualified declaration or strict PIT authority it does not possess.
- Every actual transport call passes through the existing Provider Subrequest
  Cache and Provider Request Coordinator with the existing Upstream Service
  Identity, account/endpoint scope, Safety Ceiling, cooldown, retry, and exact
  physical-attempt semantics.
- Source registrations are an unordered executable-binding catalog for pairs
  already present in the plan. Registration order has no routing meaning, and
  no second route registry is created.

## Acceptance criteria

- [ ] Exactly these six terminal registrations are replaced: Yahoo
  `financial_statements`, AKShare `financial_indicators`, Yahoo
  `current_financial_profile`, AKShare `adjustment_factors`, Yahoo-derived
  `derived_adjustment_factors`, and AKShare `issuer_name_events`.
- [ ] No `NOT_CONFIGURED`/`PhysicalAttemptNotMade` terminal stub remains for
  those six plan-declared production pairs, and no unrelated registration is
  added, removed, or reordered as policy.
- [ ] Bindings use only existing production adapters/facades, the existing
  source-to-bound-source seam, one run-owned cache, and one coordinator; no
  direct/nested legacy provider path opens another coordination boundary.
- [ ] `akshare` shares the `akshare_sina` physical identity,
  `yfinance_derived` shares `yfinance`, and
  `baostock_qualified_families` continues to share `baostock`; exact attempts,
  cooldowns, and cache keys follow those existing identities.
- [ ] Safe Tushare/AKShare-Sina/Yahoo company-type projections contain only
  qualified declarations or whitelisted canonical classifier inputs allowed by
  the spec, with artifact binding and secret/raw-payload rejection.
- [ ] Deterministic fixtures prove sequential plan traversal, exact attempt
  cardinality, cache/single-flight/bulk reuse, typed provider failures, and no
  hidden retry or fan-out for every replacement.

## Deterministic regressions

- Exercise success, partial, empty, malformed, retryable, terminal, cooldown-
  skip, cache-hit, restored-hit, and concurrent identical-call fixtures for each
  of the six bindings.
- Verify Yahoo annual/quarterly statement normalization, AKShare indicator
  projection, Yahoo current-only profile, AKShare qfq-factor projection, Yahoo
  derived-factor math/labels, and AKShare name-event authority separately.
- Assert factor/name endpoint scopes, keys, artifacts, attempts, and authority
  stay distinct while sharing the one run cache.
- Exercise company-type projection allowlists, artifact binding, provider-
  declaration limits, raw-frame/SDK-object rejection, and secret/raw-error
  canaries.
- Re-run BaoStock bundle/status, Tushare/AKShare-Sina adapter, coordinator/cache,
  daily-market route, and exact-attempt regressions to prove authority and route
  invariance.

## Compatibility and migration behavior

- Existing source keys and provider identities are filled at their current
  registration seam; no second registry, new route plan, new cache namespace,
  or historical migration is introduced.
- Existing artifacts, checkpoints, attempts, reports, IDs, provider rows,
  selections, and plan versions remain readable and are never rewritten.
- Existing Tushare, AKShare-Sina, Yahoo, and BaoStock adapter/facade behavior is
  reused. Daily mainland routing and strict BaoStock Provider History Bundle
  semantics remain unchanged.

## Explicit non-goals

- New provider clients/transports, direct SDK calls outside existing facades,
  wrapping legacy helpers that open nested coordination, or a second source/
  route registry.
- Provider-order changes, fallback/completeness policy, alternate Current
  Tradeability/listing authority, hidden retry, provider fan-out, prewarming,
  account rotation, or raw payload exposure.
- Graph-host context ownership, request-factory integration, checkpoint context
  restoration, or live rollout.
- Copying/importing the fresh-run prototype implementation or tests.
- Live providers, real credentials, paid models, integration tests, or enabling
  authoritative `qualified_v1`.

## Verification commands

Run deterministic source-binding and coordination suites only:

```powershell
pytest -q tests/test_production_qualified_provider_composition.py tests/test_qualified_production_source_bindings.py
pytest -q tests/test_tushare_statement_adapters.py tests/test_tushare_indicator_adapter.py tests/test_akshare_sina_statement_adapters.py tests/test_baostock_capability_adapters.py tests/test_baostock_ratio_adapters.py
pytest -q tests/test_provider_subrequest_cache.py tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py tests/test_yahoo_retry_coordination.py
ruff check .
git diff --check
git status --short
```

No verification command may select integration tests or contact a provider or
model.

## Definition of done

- [ ] All six plan-declared pairs execute through deterministic existing-facade
  fixtures with exact typed results, attempts, cache, and authority evidence.
- [ ] Safe company-type projections satisfy allowlist/artifact-binding rules and
  leak no raw payload, provider object, exception text, or credential material.
- [ ] Spec Compliance review confirms the exact six replacements, provider-
  identity sharing, one plan/router/cache/coordinator authority, compatibility,
  and non-goals.
- [ ] Code Quality review confirms typed normalization/error handling,
  deterministic coordination, maintainability, and no Critical or Important
  findings.
- [ ] No live traffic occurred, no prototype code was copied, and authoritative
  `qualified_v1` remains disabled.

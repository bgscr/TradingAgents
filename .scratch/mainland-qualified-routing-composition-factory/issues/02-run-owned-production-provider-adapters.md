# 02 — Assemble run-owned production provider adapters

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Make a valid production blueprint bind to a directly
exercisable, run-owned qualified router whose source functions wrap the existing
Tushare, AKShare-Sina, Yahoo, and BaoStock adapter contracts. Through the
existing router/dispatcher seam, deterministic fake transports must demonstrate
complete higher-provider success, narrow sequential salvage, degraded terminal
fallbacks, factor/name separation, BaoStock authority, exact physical-attempt
accounting, and shared-cache/bulk reuse. Provider order comes only from the
immutable Capability Routing Plan.

**Blocked by:** 01 — Add the production composition contract and immutable
blueprint.

**Status:** ready-for-agent

## Observable behavior

- One bound composition supplies the existing plan-named sources for Tushare
  statements, financial indicators, adjustment factors, and issuer name events;
  AKShare-Sina statements and already-qualified AKShare sources; Yahoo
  degraded/current-only fallbacks; and BaoStock raw/factor/status/lifecycle and
  qualified ratio-family capabilities.
- The existing router iterates the plan's exact routes. Adapters expose source
  functions but contain no second route/provider-order table, hidden fallback,
  provider probing, parallel fan-out, or inference from installed dependencies.
- Usable Tushare periods/families survive partial gaps; only unresolved work
  proceeds sequentially to AKShare-Sina, other plan sources, BaoStock ratios, or
  Yahoo as applicable.
- Yahoo remains coordinated, terminal, degraded/current-only or candidate-free
  according to existing contracts. Yahoo-derived factors remain \`derived\` and
  \`degraded\`; they never become native factors or strict PIT evidence.
- BaoStock alone can establish Current Tradeability. Its strict Provider History
  Bundle stays atomic across raw observations, status, and Adjustment Factor
  Revisions; ratio families are requested only for unresolved qualified
  families/periods.
- Factor and issuer-name routes share the run cache but retain distinct endpoint
  scopes, subrequest keys, artifacts, outcomes, attempts, and authority. Names
  remain descriptive and create no suspension, ST, lifecycle, tradeability,
  Evidence Admission, or Decision Gate fact.

## Acceptance criteria

- [ ] Every adapter converts provider results through existing provider-response,
  manifest, selection, completeness, and capability-response contracts; no SDK
  object or raw frame crosses the adapter boundary.
- [ ] Tushare uses a per-run API client without a module-global token setter;
  AKShare-Sina uses a run-owned facade; Yahoo uses the existing coordinated
  request-local transport path; BaoStock receives a per-run transport boundary.
- [ ] Every real fake-transport invocation enters the single run Provider
  Subrequest Cache and Provider Request Coordinator and emits exactly one
  physical-attempt event. Cache hits, skips, normalization, selection, restored
  values, and derived facts emit zero attempts.
- [ ] Adapter code performs no uncoordinated retry or fallback. Configured
  retries add exactly the configured attempt count, and the next provider starts
  only after a terminal non-usable result from the preceding plan source.
- [ ] Identical concurrent subrequests single-flight to one physical sequence;
  bulk statement/indicator/factor/name/status/lifecycle/ratio payloads are
  acquired once and reused for every covered target or graph call.
- [ ] Materially distinct subrequest keys do not collide, and all attempt,
  sequence, subrequest, artifact, and reuse identities use existing constructors
  and schemas.
- [ ] No qualified adapter is registered as a daily mainland market-snapshot
  source and no adapter starts prewarming or future-replay work.

## Deterministic regressions

- Tushare complete statement/indicator success retains every eligible target,
  calls no lower provider, and records exactly the endpoint invocations.
- Tushare unavailable and partial-gap matrices retain successful bulk artifacts
  while routing only unresolved periods/families.
- AKShare-Sina partial statement salvage prevents Yahoo calls for resolved
  targets; remaining Yahoo-only gaps retain exact degraded/current-only
  semantics.
- BaoStock tradeable/suspended, incomplete strict-bundle, narrow ratio-family,
  terminal failure, and logout-ready transport fixtures preserve its existing
  authority.
- Joint factor/name fixtures prove distinct scopes and artifacts, Tushare and
  BaoStock factor outcomes, Yahoo-derived terminal fallback, descriptive-only
  names, and no cross-provider bundle blending.
- Complete, retryable, terminal, cooldown-skip, fallback, concurrency, and bulk
  reuse matrices assert exact physical-attempt cardinality and deterministic
  reuse counts.

## Compatibility and migration behavior

- Existing adapters, coordinator, Provider Subrequest Cache, artifact store,
  router, dispatcher contracts, schemas, and ID constructors are composed rather
  than replaced.
- Existing provider route order, capability enablement, completeness,
  eligibility, revision selection, BaoStock authority, Evidence Admission,
  Decision Gate, and daily AKShare → BaoStock → Yahoo route remain unchanged.
- Legacy mode and ordinary non-validation paths do not construct or call these
  adapters. Historical artifacts, attempts, reports, checkpoints, manifests,
  audits, snapshots, pins, and provider rows remain byte-for-byte untouched.
- This ticket adds no migration, checkpoint sidecar, provider registry,
  process-global client, or persistence authority.

## Explicit non-goals

- Static host selection/override precedence, full lifecycle cleanup, checkpoint
  restoration, or final joint acceptance; Tickets 03–06 own those slices.
- Changing any route or provider order, adding a fallback table, broadening
  provider source coverage, relaxing completeness/eligibility, or changing
  strict-history bundle semantics.
- Giving Tushare names or non-BaoStock sources tradeability authority.
- Live-provider traffic, paid models, provider qualification, global SDK state,
  hidden retries, parallel fan-out, account rotation, or speculative prewarming.

## Verification commands

\`\`\`powershell
pytest -q tests/test_production_qualified_provider_composition.py
pytest -q tests/test_tushare_statement_adapters.py tests/test_tushare_indicator_adapter.py tests/test_tushare_factor_name_adapters.py
pytest -q tests/test_akshare_sina_statement_adapters.py tests/test_baostock_capability_adapters.py tests/test_baostock_ratio_adapters.py tests/test_provider_subrequest_cache.py
pytest -q tests/test_financial_capability_routing_selection.py tests/test_mainland_physical_attempt_coordination.py tests/test_yahoo_retry_coordination.py
ruff check .
git diff --check
\`\`\`

All provider objects must be fakes or deterministic fixtures. Do not select
integration tests, load live qualification caches, or read real credentials.

## Definition of done

- [ ] A direct production composition run demonstrates all provider families,
  sequential fallback, authority boundaries, exact attempts, single-flight, and
  bulk reuse through the existing router/dispatcher seam.
- [ ] Tests prove the plan is the only execution-order authority and no adapter
  performs uncoordinated retry, fallback, or provider I/O.
- [ ] Compatibility suites for all existing adapters and coordinator/cache
  behavior pass without fixture or expectation changes.
- [ ] Focused tests, Ruff, whitespace, secret/payload, and scope checks pass with
  zero live-provider or paid-model activity.


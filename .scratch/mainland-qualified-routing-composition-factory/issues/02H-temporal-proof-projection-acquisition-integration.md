# 02H — Integrate temporal proof projection and acquisition

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Make fresh and retained company-type and listing/lifecycle
metadata project only the exact provider-origin effective/publication proof
qualified by the specification, then route every eligibility decision through
the Ticket 02G evaluator. One fixed evaluation instant and pinned calendar
binding are owned by the existing acquisition single-flight, so all followers,
retained evidence, fresh evidence, context construction, and request binding
observe the same terminal decision without changing provider routes or owners.

**Blocked by:** 02G — Add temporal eligibility contracts and evaluator; 02B —
Acquire fresh company type and listing provenance; 02D — Add executable
production-source wiring.

**Status:** ready-for-agent

## Observable behavior

- Tushare statement-derived company-type evidence projects
  `evidence_effective_as_of` only from the exact selected row's provider
  `end_date`/bound `period_end`, and projects `ann_date`/`f_ann_date` only from
  the same immutable candidate/revision.
- AKShare-Sina statement-derived company-type evidence projects the exact
  report-period/end-date and only explicit same-row provider publication fields;
  an endpoint that cannot supply required proof returns typed temporal-proof
  unavailable.
- Yahoo current profile remains a retained diagnostic/classifier artifact but
  is unavailable as context-v2 temporal proof because the current profile has no
  qualified effective or announcement derivation.
- BaoStock listing/lifecycle evidence projects `ipoDate` as authoritative
  `listing_date`/effective proof only. It does not alias listing, observation, or
  retrieval time to `ann_date`; without qualified provider-origin publication
  proof, the projection fails closed.
- Exact retained and fresh metadata use the same evaluator. Every former
  retrieval-local-date equality authority in acquisition, compatibility,
  post-route validation, and retained lifecycle checks is removed as authority.
- The graph/run host fixes `eligibility_evaluated_at` and pins the calendar
  binding before retained-state evaluation or physical work. Thirty-two
  concurrent followers join one acquisition and one context-level evaluation
  transaction; no follower samples a clock or invokes the evaluator.

## Acceptance criteria

- [ ] Every proof retains its provider field identity plus exact artifact, row,
  candidate, and revision binding; `observed_at` and `retrieved_at` remain
  timezone-aware, immutable, and byte-for-byte unchanged.
- [ ] Tushare uses semantics identifier `tushare_statement_period_end` and
  rejects missing `end_date`, `ann_date`, or artifact/revision binding. Missing
  `f_ann_date` is current-only at most and rejects strict PIT.
- [ ] AKShare-Sina uses `akshare_sina_statement_period_end`; observation or
  retrieval timestamps never fill missing effective/publication fields.
- [ ] Yahoo profile supplies no passing temporal proof under v1. Provider order
  remains `tushare -> akshare_sina -> yfinance`; unavailability advances only
  through the existing plan-owned sequential route.
- [ ] BaoStock uses `baostock_lifecycle_listing_date` for effective proof, keeps
  `outDate` distinct, and accepts publication/exact-revision proof only when the
  provider-origin value binds the same lifecycle row/artifact.
- [ ] Request/session dates, retrieval dates, routes, factory defaults,
  Instrument Identity, symbol/name, and model output never backfill
  `evidence_effective_as_of`, `ann_date`, `f_ann_date`, observation, retrieval,
  or exact-revision availability.
- [ ] Context v2 binds both prerequisite decision identities/results/reasons,
  the latest-completed-session decision, temporal mode, requested session,
  fixed evaluation instant, and strict proof where applicable before request
  factories or the dispatcher become callable.
- [ ] Statement and indicator request factories copy the persisted context-v2
  temporal identities only; they do not recompute eligibility, query a calendar,
  call a provider, or mutate proof.
- [ ] A rejected or unavailable proof yields the closed typed acquisition
  failure, leaves the context slot unbound, exposes no partial callable runtime,
  and preserves managed interruption/cleanup and original-failure precedence.

## Exact contract and version requirements

- Consume `provider-neutral-temporal-eligibility-v1`,
  `market-session-eligibility-binding-v1`, and
  `financial-metadata-temporal-eligibility-decision-v1` without provider-specific
  policy branches.
- Advance `financial-company-type-evidence-binding-v2` to
  `financial-company-type-evidence-binding-v3` and
  `financial-company-type-evidence-projection-v1` to
  `financial-company-type-evidence-projection-v2`.
- Advance `financial-eligibility-metadata-binding-v1` to
  `financial-eligibility-metadata-binding-v2`.
- Advance `financial-listing-metadata-v1` and
  `financial-listing-metadata-projection-v1` to
  `financial-listing-metadata-v2` and
  `financial-listing-metadata-projection-v2`.
- Advance `financial-company-type-evidence-routing-request-v1`/provider response
  v1 to `financial-company-type-evidence-routing-request-v2` and
  `financial-company-type-evidence-provider-response-v2`.
- Advance Financial Eligibility Acquisition Context to
  `financial-eligibility-acquisition-context-v2` with identity namespace
  `financial-eligibility-acquisition-context:v2:`; advance
  `financial-eligibility-request-binding-v1` and acquisition result v1 to
  `financial-eligibility-request-binding-v2` and
  `financial-eligibility-acquisition-result-v2`.
- Advance statement/indicator routing requests to
  `financial-statement-routing-request-v4` and
  `financial-indicator-routing-request-v7`. All new writes bind Capability
  Routing Plan 1.3 and preserve plan-1.2 semantic-compatibility bindings.

## Migration and compatibility behavior

- Stored v1/v2 metadata, contexts, requests, provider artifacts, timestamps,
  and decisions retain their recorded readers and identities. No object is
  relabeled or rewritten to add temporal proof.
- A plan-1.3/context-v2 run cannot reuse retained evidence merely because an
  older run accepted it. The evidence is evaluated with the new run's fixed
  instant and frontier; exact replay remains a separate 05C path.
- Missing forward proof fails typed. `START_NEW` allocates a new run identity
  and performs fresh acquisition; it never repairs or upgrades historical state.
- Provider routes, the one router, Provider Subrequest Cache/Coordinator,
  graph/run-host context ownership, single-flight ownership, and BaoStock
  lifecycle authority remain unchanged.

## Explicit non-goals

- New providers, changed provider order/fallback, provider fan-out, hidden
  retries, another router/evaluator/calendar authority, or direct provider calls
  from factories/dispatchers.
- Request/session or retrieval-date backfill, local-date equality/ordering,
  grace windows, next-session-open cutoffs, or inferred publication proof.
- Checkpoint/report/audit persistence owned by 05C, broad Evidence Admission or
  Decision Gate changes, historical rewrite, or prototype code import/copy.
- Live providers, paid models, integration tests, Ticket 07, or enabling
  authoritative `qualified_v1`.

## Deterministic regressions

- For each provider/evidence class, test the one permitted derivation and every
  forbidden substitute, including missing fields, wrong row/revision, timestamp
  rewriting, and request/retrieval-date backfill.
- Exercise retained and fresh Tushare, AKShare-Sina, Yahoo, and BaoStock
  projections through the same evaluator and assert identical decisions for
  identical immutable inputs.
- Test current and strict modes at session-close/frontier boundaries, including
  missing/later `f_ann_date` and exact-revision binding controls.
- Issue 32 concurrent first requests with clock/evaluator/provider counters and
  assert one fixed instant, one context-level evaluator transaction, one
  acquisition leader, one terminal decision/context, and no follower I/O.
- Inject failure at projection, evaluation, context validation/bind, and request
  exposure boundaries; assert typed failure, unbound context, no partial runtime,
  and exact managed cleanup.
- Regress plan routes, provider attempt accounting, cache single-flight, pure
  request factories, BaoStock authority, and historical readers.

## Verification commands

Run deterministic provider-projection and acquisition tests only:

```powershell
pytest -q tests/test_financial_metadata_temporal_eligibility.py tests/test_financial_temporal_proof_projection.py
pytest -q tests/test_financial_eligibility_metadata_acquisition.py tests/test_financial_eligibility_acquisition_context.py
pytest -q tests/test_financial_request_factory_context_binding.py tests/test_qualified_routing_eligibility_context_lifecycle.py
pytest -q tests/test_tushare_statement_adapters.py tests/test_akshare_sina_statement_adapters.py tests/test_baostock_capability_adapters.py tests/test_qualified_production_source_bindings.py
pytest -q tests/test_provider_subrequest_cache.py tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py
ruff check .
git diff --check
git status --short
```

No command may select integration tests or contact a provider or model.

## Definition of done

- [ ] Every qualified provider class projects only its exact allowed
  effective/publication proof, and unavailable proof fails typed without
  backfill.
- [ ] All retained/fresh and context/request paths use the single evaluator with
  one run-owned fixed instant and pinned calendar binding.
- [ ] The 32-follower test proves one temporal transaction and one immutable
  terminal result with exact provider attempt accounting.
- [ ] Forward versions and historical readers pass with no route/ownership,
  authority, migration, or replay drift.
- [ ] Spec Compliance then Code Quality review finds no unresolved Critical or
  Important issue; no provider/model I/O occurred, no prototype code was copied,
  and authoritative `qualified_v1` remains disabled.
